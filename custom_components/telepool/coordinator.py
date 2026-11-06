"""Polling, persistent telemetry and power-aware availability."""

import logging
import math
from dataclasses import asdict
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .availability import Availability, PowerPolicy
from .client import AuthenticationError, Snapshot, Station, TelePoolClient, TelePoolError
from .const import DEFAULT_POLL_INTERVAL, DEFAULT_STALE_AFTER, DEFAULT_STARTUP_GRACE, DOMAIN, get_control_mode
from .native_control import NativeControlManager

_LOGGER = logging.getLogger(__name__)


def _restore_snapshot(stored, station_id: str) -> Snapshot:
    """Reject foreign or malformed cache rather than publishing invented state."""
    if not isinstance(stored, dict) or stored.get("station_id") != station_id:
        raise ValueError("Invalid station cache")
    restored = dict(stored)
    for field in ("observed_at", "received_at"):
        value = restored.get(field)
        parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
        if value is not None and (parsed is None or parsed.tzinfo is None):
            raise ValueError("Invalid cached timestamp")
        restored[field] = parsed
    if restored["observed_at"] is None:
        raise ValueError("Missing cached sample time")
    for field in ("values", "modes"):
        mapping = restored.get(field)
        if not isinstance(mapping, dict) or not all(isinstance(key, str) for key in mapping):
            raise ValueError("Invalid cached readings")
        for value in mapping.values():
            if field == "values" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError("Invalid cached number")
            if field == "modes" and not isinstance(value, int):
                raise ValueError("Invalid cached mode")
    for field in ("stale", "error"):
        if not isinstance(restored.get(field), bool):
            raise ValueError("Invalid cached flag")
    for field in ("rssi", "quality"):
        value = restored.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("Invalid cached diagnostic")
    return Snapshot(**restored)


class TelePoolCoordinator(DataUpdateCoordinator[Snapshot | None]):
    """Retain original sample times across failures and HA restarts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: TelePoolClient, station: Station):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=entry.options.get("poll_interval", DEFAULT_POLL_INTERVAL)),
        )
        self.entry = entry
        self.client = client
        self.station = station
        self.snapshot: Snapshot | None = None
        self.cloud_ok = False
        self.cloud_error: str | None = None
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.policy = PowerPolicy(
            entry.options.get("stale_after", DEFAULT_STALE_AFTER),
            entry.options.get("startup_grace", DEFAULT_STARTUP_GRACE),
        )
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.telemetry", private=True, atomic_writes=True)
        self._native = NativeControlManager(self)
        entry.async_on_unload(self._native.close)

    @property
    def native_controls(self):
        return self._native.controls

    @property
    def native_available(self) -> bool:
        return self._native.available

    @property
    def native_held(self) -> bool:
        return self._native.held

    @property
    def native_pending_target(self):
        return self._native.pending_target

    @property
    def native_pending_error(self):
        return self._native.pending_error

    @property
    def native_source_time(self):
        return self._native.source_time

    async def async_set_native_temperature(self, value: float) -> None:
        await self._native.set_temperature(value)

    async def async_set_native_enabled(self, enabled: bool) -> None:
        await self._native.set_enabled(enabled)

    @property
    def power_entity_id(self) -> str | None:
        if get_control_mode(self.entry.options, "power") != "external":
            return None
        registry_id = self.entry.options.get("power_registry_id")
        if registry_id:
            entity = er.async_get(self.hass).async_get(registry_id)
            return entity.entity_id if entity else None
        return self.entry.options.get("power_source") or None

    @property
    def availability(self) -> Availability:
        sample = self.snapshot
        return self.policy.evaluate(
            dt_util.utcnow(),
            sample.observed_at if sample else None,
            server_stale=sample.stale if sample else True,
            cloud_ok=self.cloud_ok,
            has_values=bool(sample and any(v is not None for v in sample.values.values())),
        )

    async def async_initialize(self) -> None:
        """Restore cache before the first network attempt."""
        try:
            if stored := await self._store.async_load():
                self.snapshot = _restore_snapshot(stored, self.station.id)
        except ValueError, TypeError, KeyError, HomeAssistantError, OSError:
            _LOGGER.warning("Ignoring invalid TelePool telemetry cache")
        self._read_power()
        await self._native.initialize()
        if self.power_entity_id or self.entry.options.get("power_source"):
            # Listening to all switch events also handles a source's registry rename.
            self.entry.async_on_unload(self.hass.bus.async_listen("state_changed", self._power_changed))
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._tick, timedelta(seconds=30)))
        await self.async_refresh()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._read_power()
        self.async_update_listeners()

    @callback
    def _read_power(self) -> None:
        entity_id = self.power_entity_id
        state = self.hass.states.get(entity_id) if entity_id else None
        registered = er.async_get(self.hass).async_get(entity_id) if entity_id else None
        trusted = bool(
            state
            and not state.attributes.get("restored")
            and (registered is None or (not registered.disabled and registered.platform != DOMAIN))
        )
        power = state.state if trusted and state.state in ("on", "off") else None
        if self.policy.set_power(power, dt_util.utcnow(), self.snapshot.observed_at if self.snapshot else None):
            self.entry.async_on_unload(
                async_call_later(self.hass, self.policy.startup_grace.total_seconds(), self._boot_finished)
            )

    @callback
    def _power_changed(self, event) -> None:
        if event.data.get("entity_id") != self.power_entity_id:
            return
        previous = self.policy.power
        self._read_power()
        self.async_update_listeners()
        if previous != self.policy.power and self.policy.power == "on":
            self.hass.async_create_task(self.async_request_refresh())

    async def _boot_finished(self, _now) -> None:
        await self.async_request_refresh()
        self.async_update_listeners()

    async def _async_update_data(self) -> Snapshot | None:
        if self.policy.power == "off" and self.snapshot:
            return self.snapshot
        self.last_attempt = dt_util.utcnow()
        try:
            sample = await self.client.async_fetch(self.station)
        except AuthenticationError:
            self.cloud_ok = False
            self.cloud_error = "authentication"
            self.entry.async_start_reauth(self.hass)
        except TelePoolError:
            self.cloud_ok = False
            self.cloud_error = "connection_or_protocol"
        else:
            if sample.observed_at is None or not any(value is not None for value in sample.values.values()):
                self.cloud_ok = False
                self.cloud_error = "no_data"
                return self.snapshot
            self.snapshot = sample
            self.cloud_ok = True
            self.cloud_error = None
            self.last_success = dt_util.utcnow()
            stored = asdict(sample)
            for field in ("observed_at", "received_at"):
                stored[field] = stored[field].isoformat() if stored[field] else None
            await self._store.async_save(stored)
        await self._native.refresh()
        return self.snapshot
