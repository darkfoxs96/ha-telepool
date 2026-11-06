"""Native Q3 control state and a durable, power-aware temperature intention."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import asdict
from typing import Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .client import (
    AuthenticationError,
    CommandNotSent,
    ProtocolError,
    Q3Controls,
    SettingsRejected,
    TelePoolError,
    WriteOutcomeUnknown,
)
from .client import (
    ConnectionError as TelePoolConnectionError,
)
from .const import POOL_MAX_TEMP_CELSIUS, get_control_mode
from .storage import StrictStore

_LOGGER = logging.getLogger(__name__)
_SAFE_ERROR_TYPES = {
    AuthenticationError: "AuthenticationError",
    CommandNotSent: "CommandNotSent",
    TelePoolConnectionError: "ConnectionError",
    ProtocolError: "ProtocolError",
    SettingsRejected: "SettingsRejected",
    TelePoolError: "TelePoolError",
    WriteOutcomeUnknown: "WriteOutcomeUnknown",
}
_SETTINGS_REJECTION_REASONS = {
    "http_400": "TelePool rejected the settings form (HTTP 400)",
    "http_403": "TelePool rejected the settings form (HTTP 403)",
    "access_denied": "TelePool denied access to the settings submission",
    "validation_error": "TelePool reported settings validation errors",
}
_SAFE_VALIDATION_FIELDS = frozenset(
    {
        "SetPointTemp",
        "SetDeltaTemp",
        "MenuEnableFu",
        "SysPriorityTemp",
        "SysPoolType",
        "SysPoolTent",
        "SysPoolVolume",
        "SysFlow",
        "SysFlowWait",
        "SysStp",
        "SysStpWait",
        "ClockGmt",
        "ModePh",
        "ModeRx",
        "ModeFcl",
        "ModeFu",
        "ModeTemp",
        "DosModePh",
        "DosPriorityPh",
        "SysZozEnableTemp",
        "SysZozTemp",
        "SysDcTemp",
        "SysTypeTemp",
        "SenCorrectTemp",
        "PumpPerfLeft",
        "PumpModeLeft",
        "PumpCanTypeLeft",
        "PumpCanWaitLeft",
        "PumpPerfRight",
        "PumpModeRight",
        "PumpCanTypeRight",
        "PumpCanWaitRight",
    }
    | {
        f"{field}{channel}"
        for field in (
            "SetPoint",
            "SetDelta",
            "DosType",
            "DosEnable",
            "DosDayVolume",
            "DosVolume",
            "DosWait",
            "DosGain",
            "DosProtectEnable",
            "DosProtect",
            "DosProtectWait",
            "SenCorrect",
            "SenMin",
            "SenMax",
        )
        for channel in ("Ph", "Rx")
    }
    | {f"Hour{hour}" for hour in range(24)}
)
# Audited literal messages only. A new client exception/message must be
# explicitly reviewed here before its detail is exposed to the UI or logs.
# Never stringify arbitrary future exceptions or include exception tracebacks:
# their args/causes may eventually contain URLs, HTML, credentials, or tokens.
_SAFE_CLIENT_MESSAGES = frozenset(
    {
        "Could not connect to TelePool",
        "TelePool ChartQ3 contains an invalid point",
        "TelePool ChartQ3 has an unexpected format",
        "TelePool FU menu control requires a boolean",
        "TelePool FU menu setting has unknown choices",
        "TelePool FU menu setting is unavailable",
        "TelePool FaceData contains an invalid entry",
        "TelePool FaceData has no station identifier",
        "TelePool FaceData is not an array",
        "TelePool command was not sent because its dispatch conditions changed",
        "TelePool command was not sent because its dispatch guard could not approve it",
        "TelePool dashboard contains an invalid device",
        "TelePool dashboard contains an invalid station identifier",
        "TelePool dashboard contains invalid Q3 metadata",
        "TelePool dashboard device list has an unexpected format",
        "TelePool dashboard device list was not found",
        "TelePool dashboard has invalid account routing context",
        "TelePool dashboard is missing Q3 metadata",
        "TelePool dashboard is missing a Q3 display",
        "TelePool dashboard is missing account routing context",
        "TelePool dashboard is missing the Q3 pool association",
        "TelePool denied access to the settings submission",
        "TelePool denied access to this operation",
        "TelePool did not accept the supplied credentials",
        "TelePool did not provide a request verification token",
        "TelePool did not provide a valid settings form",
        "TelePool did not return a discovered Q3's screen mask",
        "TelePool did not return the requested Q3's status",
        "TelePool is temporarily unavailable",
        "TelePool provided an empty request verification token",
        "TelePool redirected a form to an unexpected endpoint",
        "TelePool redirected outside its HTTPS origin",
        "TelePool redirected to an unsupported endpoint",
        "TelePool rejected the settings form",
        "TelePool request verification token is unavailable",
        "TelePool response exceeded the supported size",
        "TelePool returned a redirect without a destination",
        "TelePool returned an unexpected HTTP status",
        "TelePool returned an unrecognized settings destination",
        "TelePool returned an unrecognized settings response",
        "TelePool returned an unsupported Q3 screen mask",
        "TelePool returned invalid JSON",
        "TelePool returned invalid response text",
        "TelePool returned too many redirects",
        "TelePool session changed after settings preflight",
        "TelePool session could not be restored",
        "TelePool session expired before the settings submission",
        "TelePool setting is not a unique successful control",
        "TelePool settings are missing unique binding context",
        "TelePool settings contain ambiguous duplicate controls",
        "TelePool settings contain an invalid capability flag",
        "TelePool settings contain an unknown successful control",
        "TelePool settings contain an unsupported file control",
        "TelePool settings contain an unsupported sensitive control",
        "TelePool settings contain duplicate critical controls",
        "TelePool settings contain duplicate successful critical values",
        "TelePool settings form has unexpected routing context",
        "TelePool settings form is incomplete",
        "TelePool settings have invalid station or ordinary-command context",
        "TelePool settings have unsupported binding context",
        "TelePool settings may be queued; the requested value is not confirmed yet",
        "TelePool settings response exceeded the supported size",
        "TelePool settings response redirected outside its HTTPS origin",
        "TelePool settings submission outcome is unknown; do not resend automatically",
        "TelePool settings unexpectedly expose sensitive network configuration",
        "TelePool settings use an unsupported submission format",
        "TelePool settings were submitted but readback could not be confirmed",
        "TelePool station has invalid control routing context",
        "TelePool telemetry belongs to a different station",
        "TelePool temperature must match the current form bounds and step",
        "TelePool temperature setting is unavailable",
        "Unsupported TelePool setting",
    }
)


def _safe_client_cause(error: TelePoolError) -> str:
    """Return bounded, reviewed diagnostics without invoking arbitrary str()."""
    error_type = type(error)
    label = _SAFE_ERROR_TYPES.get(error_type, "TelePoolError")
    reason = None
    if error_type is SettingsRejected:
        reason = _SETTINGS_REJECTION_REASONS.get(error.reason) if type(error.reason) is str else None
        if reason and error.reason == "validation_error":
            fields = getattr(error, "invalid_fields", ())
            if isinstance(fields, (tuple, list)):
                # The client already bounds these fields. Defend this display
                # boundary again without reading arbitrary payload attributes,
                # model error messages, field values, or routing/token fields.
                names = sorted({name for name in fields[:32] if type(name) is str and name in _SAFE_VALIDATION_FIELDS})[
                    :8
                ]
                if names:
                    reason += f" (fields: {', '.join(names)})"
    elif error_type in _SAFE_ERROR_TYPES and len(error.args) == 1:
        message = error.args[0]
        if isinstance(message, str) and message in _SAFE_CLIENT_MESSAGES:
            reason = message
    return f"{label}: {reason}" if reason else f"{label}: no safe diagnostic detail"


def _log_client_failure(operation: str, error: TelePoolError) -> str:
    reason = _safe_client_cause(error)
    _LOGGER.warning("Native Q3 %s failed (%s)", operation, reason)
    return reason


def _number(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


class NativeControlManager:
    """Keep native commands separate from external HA proxy commands.

    Persist a sending marker BEFORE POST. A crash or ambiguous response never
    causes an automatic replay: a later read can confirm the requested value,
    otherwise a new explicit user request is required.
    """

    def __init__(self, coordinator) -> None:
        self.owner = coordinator
        self.controls: Q3Controls | None = None
        self.pending_target: float | None = None
        self.pending_error: str | None = None
        self.source_time: str | None = None
        self._delivery: str | None = None
        self._read_ok = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        generation = coordinator.entry.options.get("native_cache_generation", "")
        generation = (
            generation if isinstance(generation, str) and re.fullmatch(r"[a-f0-9]{32}", generation) else "initial"
        )
        self._store = StrictStore(
            coordinator.hass,
            1,
            f"telepool.{coordinator.station.id}.{generation}.native_controls",
            private=True,
            atomic_writes=True,
        )

    @property
    def enabled(self) -> bool:
        return any(get_control_mode(self.owner.entry.options, kind) == "native_q3" for kind in ("thermostat", "power"))

    @property
    def available(self) -> bool:
        return bool(not self._closed and self._read_ok and self.controls and self.owner.availability.fresh)

    @property
    def held(self) -> bool:
        return bool(
            not self._closed
            and self.controls
            and self.owner.availability.display_available
            and self.owner.availability.status in ("paused", "starting")
        )

    def close(self) -> None:
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()

    def _track(self) -> asyncio.Task:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        return task

    async def initialize(self) -> None:
        if not self.enabled:
            return
        try:
            record = await self._store.async_load()
            if not isinstance(record, dict) or set(record) != {
                "controls",
                "source_time",
                "pending",
                "delivery",
                "error",
            }:
                return
            values = record["controls"]
            if not isinstance(values, dict) or values.get("station_id") != self.owner.station.id:
                return
            controls = Q3Controls(**values)
            for value in (
                controls.target_temperature,
                controls.min_temperature,
                controls.max_temperature,
                controls.temperature_step,
            ):
                if value is not None and not _number(value):
                    return
            if controls.menu_enable_fu is not None and not isinstance(controls.menu_enable_fu, bool):
                return
            if not isinstance(controls.can_set_temperature, bool) or not isinstance(controls.can_set_enabled, bool):
                return
            timestamp = dt_util.parse_datetime(record["source_time"])
            if timestamp is None or timestamp.tzinfo is None:
                return
            pending = record["pending"]
            if pending is not None and not _number(pending):
                return
            delivery = record["delivery"]
            if delivery not in (None, "queued", "sending", "unknown", "blocked"):
                return
            if (pending is None) != (delivery is None):
                return
            error = record["error"]
            if error is not None and error not in ("write_outcome_unknown", "write_failed", "target_out_of_range"):
                return
            if (
                (delivery in (None, "queued", "sending") and error is not None)
                or (delivery == "unknown" and error != "write_outcome_unknown")
                or (delivery == "blocked" and error not in ("write_failed", "target_out_of_range"))
            ):
                return
            self.controls = controls
            self.source_time = timestamp.isoformat()
            if get_control_mode(self.owner.entry.options, "thermostat") == "native_q3":
                self.pending_target = pending
                self._delivery = "unknown" if delivery == "sending" else delivery
                self.pending_error = "write_outcome_unknown" if delivery == "sending" else error
        except HomeAssistantError, OSError, TypeError, ValueError, KeyError, OverflowError:
            # Restoring cache must not authenticate or dispatch anything.
            return

    async def _save(self, pending: float | None, delivery: str | None, error: str | None) -> None:
        if self.controls is None or self._closed:
            raise HomeAssistantError("Native Q3 control state is unavailable")
        observed = self.owner.snapshot.observed_at if self.owner.snapshot else None
        timestamp = observed.isoformat() if observed else self.source_time
        if timestamp is None:
            raise HomeAssistantError("No Q3 measurement timestamp is available for the native cache")
        await self._store.async_save(
            {
                "controls": asdict(self.controls),
                "source_time": timestamp,
                "pending": pending,
                "delivery": delivery,
                "error": error,
            }
        )
        self.source_time = timestamp
        self.pending_target, self._delivery, self.pending_error = pending, delivery, error

    def _validate_target(self, value: Any) -> float:
        controls = self.controls
        if controls is None or not controls.can_set_temperature:
            raise ServiceValidationError("Q3 temperature control is not available in the current form")
        if not _number(value) or not all(
            _number(bound) for bound in (controls.min_temperature, controls.max_temperature, controls.temperature_step)
        ):
            raise ServiceValidationError("Q3 temperature bounds or target are invalid")
        minimum, maximum, step = (
            controls.min_temperature,
            min(controls.max_temperature, POOL_MAX_TEMP_CELSIUS),
            controls.temperature_step,
        )
        if minimum > maximum or step <= 0 or not minimum <= value <= maximum:
            raise ServiceValidationError("Temperature is outside the supported Q3 range")
        steps = (value - minimum) / step
        if not math.isclose(steps, round(steps), abs_tol=1e-6):
            raise ServiceValidationError("Temperature does not match the Q3 target step")
        return float(value)

    def _can_send_temperature(self, *, require_power_on: bool = False) -> bool:
        if (
            self._closed
            or not self.available
            or get_control_mode(self.owner.entry.options, "thermostat") != "native_q3"
        ):
            return False
        if require_power_on or get_control_mode(self.owner.entry.options, "power") == "external":
            return self.owner.policy.power == "on"
        return True

    async def refresh(self) -> None:
        if not self.enabled or self._closed:
            return
        task = self._track()
        try:
            async with self._lock:
                try:
                    controls = await self.owner.client.async_get_controls(self.owner.station)
                except TelePoolError:
                    self._read_ok = False
                    return
                if self._closed:
                    return
                self.controls = controls
                self._read_ok = True
                try:
                    if (
                        self.pending_target is not None
                        and controls.target_temperature is not None
                        and math.isclose(controls.target_temperature, self.pending_target, abs_tol=1e-6)
                    ):
                        await self._save(None, None, None)
                    else:
                        await self._save(self.pending_target, self._delivery, self.pending_error)
                        if (
                            self.pending_target is not None
                            and self._delivery == "queued"
                            and self.available
                            and self.owner.policy.power == "on"
                        ):
                            try:
                                self._validate_target(self.pending_target)
                            except ServiceValidationError:
                                await self._save(self.pending_target, "blocked", "target_out_of_range")
                            else:
                                await self._deliver(require_power_on=True)
                except HomeAssistantError, OSError:
                    # Live readings remain usable even if the private cache is not writable.
                    pass
        finally:
            self._tasks.discard(task)

    async def set_temperature(self, value: float) -> None:
        if get_control_mode(self.owner.entry.options, "thermostat") != "native_q3" or self._closed:
            raise ServiceValidationError("Native Q3 thermostat is not selected")
        task = self._track()
        try:
            async with self._lock:
                target = self._validate_target(value)
                paused = self.owner.policy.power == "off" and self.held
                if not paused and not self._can_send_temperature():
                    raise HomeAssistantError("Q3 is not connected; temperature cannot be sent")
                await self._save(target, "queued", None)
                if not paused:
                    await self._deliver()
        finally:
            self._tasks.discard(task)
            if not self._closed:
                self.owner.async_update_listeners()

    async def _deliver(self, *, require_power_on: bool = False) -> None:
        if self._closed or self.pending_target is None:
            return
        if not self._can_send_temperature(require_power_on=require_power_on):
            await self._not_sent(self.pending_target, require_power_on=require_power_on)
            return
        target = self._validate_target(self.pending_target)
        await self._save(target, "sending", None)
        try:
            if not self._can_send_temperature(require_power_on=require_power_on):
                await self._not_sent(target, require_power_on=require_power_on)
                return
            self.controls = await self.owner.client.async_set_temperature(
                self.owner.station,
                target,
                dispatch_guard=lambda: self._can_send_temperature(require_power_on=require_power_on),
            )
        except CommandNotSent:
            await self._not_sent(target, require_power_on=require_power_on)
            return
        except WriteOutcomeUnknown as error:
            reason = _log_client_failure("temperature command", error)
            await self._save(target, "unknown", "write_outcome_unknown")
            raise HomeAssistantError(
                f"Q3 command outcome is unknown; it will not be automatically repeated ({reason})"
            ) from None
        except TelePoolError as error:
            reason = _log_client_failure("temperature command", error)
            await self._save(target, "blocked", "write_failed")
            raise HomeAssistantError(f"Q3 did not accept the temperature command ({reason})") from None
        if not self._closed:
            await self._save(None, None, None)

    async def _not_sent(self, target: float, *, require_power_on: bool) -> None:
        if require_power_on or (self.owner.policy.power == "off" and self.held):
            await self._save(target, "queued", None)
            return
        await self._save(target, "blocked", "write_failed")
        raise HomeAssistantError("Q3 control conditions changed before sending; no command was sent") from None

    async def set_enabled(self, enabled: bool) -> None:
        if get_control_mode(self.owner.entry.options, "power") != "native_q3" or not isinstance(enabled, bool):
            raise ServiceValidationError("Native Q3 FU menu control is not selected")
        task = self._track()
        try:
            async with self._lock:
                if not self.available or not self.controls or not self.controls.can_set_enabled:
                    raise HomeAssistantError("Q3 FU menu control is unavailable")
                try:
                    self.controls = await self.owner.client.async_set_enabled(
                        self.owner.station,
                        enabled,
                        dispatch_guard=lambda: (
                            self.available and get_control_mode(self.owner.entry.options, "power") == "native_q3"
                        ),
                    )
                except CommandNotSent:
                    raise HomeAssistantError(
                        "Q3 control conditions changed before sending; no command was sent"
                    ) from None
                except WriteOutcomeUnknown as error:
                    reason = _log_client_failure("FU menu setting", error)
                    self._read_ok = False
                    raise HomeAssistantError(
                        f"Q3 command outcome is unknown; refresh before another command ({reason})"
                    ) from None
                except TelePoolError as error:
                    reason = _log_client_failure("FU menu setting", error)
                    self._read_ok = False
                    raise HomeAssistantError(f"Q3 did not accept the FU menu setting ({reason})") from None
                try:
                    await self._save(self.pending_target, self._delivery, self.pending_error)
                except HomeAssistantError, OSError:
                    pass
        finally:
            self._tasks.discard(task)
            if not self._closed:
                self.owner.async_update_listeners()
