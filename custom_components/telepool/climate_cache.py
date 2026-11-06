"""Private, source-bound cache for an external climate proxy.

Only curated thermostat state and a pending temperature target can be saved.
No credentials, arbitrary source attributes, or arbitrary service calls belong
here. A successful async_save means the write finished; failed writes propagate
so callers cannot acknowledge a pending command as durably queued.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .storage import StrictStore

_STORAGE_VERSION = 1
_DATA_FIELDS = frozenset(("source_identity", "source_time", "state", "attributes", "pending"))
_REQUIRED_FIELDS = _DATA_FIELDS - {"pending"}
_NUMBER_ATTRIBUTES = frozenset(
    (
        "current_temperature",
        "temperature",
        "target_temp_low",
        "target_temp_high",
        "min_temp",
        "max_temp",
        "target_temp_step",
    )
)
_OTHER_ATTRIBUTES = frozenset(("temperature_unit", "hvac_modes", "hvac_action", "supported_features"))
_TARGET_FIELDS = frozenset(("temperature", "target_temp_low", "target_temp_high"))
_UNITS = frozenset(item.value for item in UnitOfTemperature)
_MODES = frozenset(item.value for item in HVACMode)
_ACTIONS = frozenset(item.value for item in HVACAction)


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_unit(value: object) -> str:
    if not isinstance(value, str) or value not in _UNITS:
        raise ValueError("Invalid thermostat cache temperature unit")
    return str(value)


def _validate_attributes(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - (_NUMBER_ATTRIBUTES | _OTHER_ATTRIBUTES):
        raise ValueError("Invalid thermostat cache attributes")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _NUMBER_ATTRIBUTES:
            if item is not None and not _finite_number(item):
                raise ValueError("Invalid thermostat cache numeric attribute")
            result[key] = item
        elif key == "temperature_unit":
            result[key] = None if item is None else _validate_unit(item)
        elif key == "hvac_modes":
            if not isinstance(item, (list, tuple)) or any(
                not isinstance(mode, str) or mode not in _MODES for mode in item
            ):
                raise ValueError("Invalid thermostat cache HVAC modes")
            result[key] = [str(mode) for mode in item]
        elif key == "hvac_action":
            if item is not None and (not isinstance(item, str) or item not in _ACTIONS):
                raise ValueError("Invalid thermostat cache HVAC action")
            result[key] = None if item is None else str(item)
        elif key == "supported_features":
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError("Invalid thermostat cache supported features")
            result[key] = int(item)
    return result


def _validate_pending(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"unit", "targets"}:
        raise ValueError("Invalid thermostat cache pending target")
    unit = _validate_unit(value["unit"])
    targets = value["targets"]
    if (
        not isinstance(targets, Mapping)
        or not targets
        or set(targets) - _TARGET_FIELDS
        or any(not _finite_number(target) for target in targets.values())
        or set(targets) not in ({"temperature"}, {"target_temp_low", "target_temp_high"})
        or ("target_temp_low" in targets and targets["target_temp_low"] > targets["target_temp_high"])
    ):
        raise ValueError("Invalid thermostat cache pending temperature values")
    return {"unit": unit, "targets": dict(targets)}


class ClimateCache:
    """Persist a cache bound to one Q3 station and one external source identity.

    The caller supplies a backend-prefixed registry UUID or entity_id as the
    source identity. Its digest isolates source changes without putting an
    arbitrary entity string into a storage filename. Unknown keys are rejected,
    not copied into persistent storage.
    """

    def __init__(self, hass: HomeAssistant, station_id: str, source_identity: str) -> None:
        if not isinstance(station_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", station_id):
            raise ValueError("Invalid thermostat cache station identifier")
        if not isinstance(source_identity, str) or not source_identity or len(source_identity) > 2048:
            raise ValueError("Invalid thermostat cache source identity")
        self._source_identity = source_identity
        digest = hashlib.sha256(source_identity.encode("utf-8")).hexdigest()
        self._store: StrictStore[dict[str, Any]] = StrictStore(
            hass,
            _STORAGE_VERSION,
            f"telepool.{station_id}.{digest}.climate",
            private=True,
            atomic_writes=True,
        )
        self._lock = asyncio.Lock()

    def _validate(self, data: object) -> dict[str, Any]:
        if not isinstance(data, Mapping) or set(data) - _DATA_FIELDS or not _REQUIRED_FIELDS <= set(data):
            raise ValueError("Invalid thermostat cache data")
        if data["source_identity"] != self._source_identity:
            raise ValueError("Thermostat cache source identity does not match")
        source_time = data["source_time"]
        if not isinstance(source_time, str):
            raise ValueError("Invalid thermostat cache source time")
        try:
            timestamp = datetime.fromisoformat(source_time)
        except ValueError:
            raise ValueError("Invalid thermostat cache source time") from None
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Thermostat cache source time must include a timezone")
        state = data["state"]
        if not isinstance(state, str) or state not in _MODES:
            raise ValueError("Invalid thermostat cache HVAC state")
        return {
            "source_identity": self._source_identity,
            "source_time": timestamp.isoformat(),
            "state": str(state),
            "attributes": _validate_attributes(data["attributes"]),
            "pending": _validate_pending(data.get("pending")),
        }

    async def async_load(self) -> dict[str, Any] | None:
        """Return validated cached state, or None for missing/invalid/unreadable data."""
        async with self._lock:
            try:
                data = await self._store.async_load()
                return None if data is None else self._validate(data)
            except HomeAssistantError, OSError, ValueError, TypeError, KeyError, NotImplementedError:
                return None

    async def async_save(self, data: Mapping[str, Any]) -> None:
        """Validate a detached copy and await its write; never suppress save errors."""
        validated = self._validate(data)
        async with self._lock:
            await self._store.async_save(validated)
