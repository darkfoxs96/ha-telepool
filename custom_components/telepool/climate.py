"""External thermostat proxy with an optional explicit shared-power pause."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Any

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .climate_cache import ClimateCache
from .const import (
    CONF_MAX_TARGET_TEMPERATURE,
    DEFAULT_MAX_TARGET_TEMPERATURE,
    DEFAULT_STARTUP_GRACE,
    DOMAIN,
    get_control_mode,
    get_max_target_temperature,
)
from .proxy import StationReference, TelePoolSourceEntity

_LOGGER = logging.getLogger(__name__)
_TARGET_KEYS = (ATTR_TEMPERATURE, ATTR_TARGET_TEMP_LOW, ATTR_TARGET_TEMP_HIGH)

_SUPPORTED_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
)


class _TargetValidationError(ServiceValidationError):
    """Expose a stable pending status as well as a readable service error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Create exactly the explicitly selected thermostat backend."""
    mode = get_control_mode(entry.options, "thermostat")
    if mode == "native_q3":
        from .native_climate import TelePoolNativeClimate

        async_add_entities([TelePoolNativeClimate(entry.runtime_data.coordinator)])
    elif mode == "external" and (source := entry.options.get("climate_source")):
        async_add_entities(
            [
                TelePoolClimate(
                    entry.runtime_data.station,
                    source,
                    entry.options.get("climate_registry_id"),
                    power_linked=entry.options.get("climate_power_linked", False),
                    power_source=entry.options.get("power_source"),
                    power_registry_id=entry.options.get("power_registry_id"),
                    startup_grace=entry.options.get("startup_grace", DEFAULT_STARTUP_GRACE),
                    max_target_temperature=get_max_target_temperature(entry.options),
                )
            ]
        )


class TelePoolClimate(TelePoolSourceEntity, ClimateEntity):
    """Reflect an existing thermostat without adding another regulator."""

    _attr_translation_key = "climate"
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self,
        station: StationReference,
        source_entity_id: str,
        registry_id: str | None = None,
        *,
        power_linked: bool = False,
        power_source: str | None = None,
        power_registry_id: str | None = None,
        startup_grace: int = DEFAULT_STARTUP_GRACE,
        max_target_temperature: float = DEFAULT_MAX_TARGET_TEMPERATURE,
    ) -> None:
        """Initialize the stable logical thermostat."""
        super().__init__(station, source_entity_id, registry_id, CLIMATE_DOMAIN)
        self._attr_unique_id = f"{station.id}_climate"
        self._station_id = station.id
        self._power_linked = power_linked
        self._track_source_reports = power_linked
        self._configured_power_id = power_source
        self._power_registry_id = power_registry_id
        self._power_entity_id: str | None = None
        self._unsubscribe_power = None
        self._power: str | None = None
        self._power_on_at: datetime | None = None
        self._source_recovered = False
        self._startup_grace = startup_grace
        self._max_target_temperature = get_max_target_temperature({CONF_MAX_TARGET_TEMPERATURE: max_target_temperature})
        self._cancel_grace = None
        self._cache: dict[str, Any] | None = None
        self._cache_store: ClimateCache | None = None
        self._cache_identity: str | None = None
        self._cache_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._flush_inflight = False
        self._removed = False
        self._source_tasks: set[asyncio.Task] = set()
        self._pending_error: str | None = None

    @property
    def _source_identity(self) -> str:
        return f"external_climate:{self._source_registry_id or self._configured_source_id}"

    @property
    def _source_ready(self) -> bool:
        if not super().available:
            return False
        if not self._power_linked:
            return True
        if self._power == "off":
            return False
        if self._power == "on" and self._power_on_at is not None:
            state = self.source_state
            return self._source_recovered or bool(state and state.last_reported >= self._power_on_at)
        return True

    @property
    def _held(self) -> bool:
        if not self._power_linked or not self._cache or self._cache_identity != self._source_identity:
            return False
        if self._power == "off":
            return True
        return bool(
            self._power == "on"
            and not self._source_ready
            and not self._source_recovered
            and self._power_on_at is not None
            and (dt_util.utcnow() - self._power_on_at).total_seconds() < self._startup_grace
        )

    @property
    def available(self) -> bool:
        """A confirmed shared power pause may retain a validated snapshot."""
        return (self._source_ready or self._held) and self._source_min_temp <= self.max_temp

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes = super().extra_state_attributes
        if not self._power_linked:
            return attributes
        return {
            **attributes,
            "held": self._held,
            "data_fresh": self._source_ready,
            "source_time": self._cache.get("source_time") if self._cache else None,
            "pending_target": self._cache.get("pending") if self._cache else None,
            "pending_error": self._pending_target_error,
            "reported_target": (
                {
                    "unit": self._cache["attributes"].get("temperature_unit"),
                    "targets": {
                        key: self._cache["attributes"][key] for key in _TARGET_KEYS if key in self._cache["attributes"]
                    },
                }
                if self._cache and self._cache.get("pending")
                else None
            ),
            "power_link_status": (
                "paused"
                if self._power == "off"
                else "working"
                if self._source_ready
                else "starting"
                if self._held
                else "unavailable"
            ),
        }

    @property
    def _pending_target_error(self) -> str | None:
        if not self._cache or not (pending := self._cache.get("pending")):
            return None
        if self._held or self._source_ready:
            try:
                self._validate_targets(pending["targets"], pending["unit"])
            except _TargetValidationError as error:
                return error.code
        return self._pending_error

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if not self._power_linked:
            return
        self._cache_identity = self._source_identity
        self._cache_store = ClimateCache(self.hass, self._station_id, self._cache_identity)
        self._cache = await self._cache_store.async_load()
        self._bind_power()
        self.async_on_remove(self._remove_power_tracking)
        self._read_power()
        if self._source_ready:
            await self._async_capture(self.source_state)
            # Restored pending state is sent only when this source is demonstrably live.
            self._schedule_source_work()

    @callback
    def _remove_power_tracking(self) -> None:
        self._removed = True
        for task in self._source_tasks:
            task.cancel()
        if self._unsubscribe_power:
            self._unsubscribe_power()
            self._unsubscribe_power = None
        if self._cancel_grace:
            self._cancel_grace()
            self._cancel_grace = None

    async def async_will_remove_from_hass(self) -> None:
        self._remove_power_tracking()
        if self._source_tasks:
            await asyncio.gather(*self._source_tasks, return_exceptions=True)
        await super().async_will_remove_from_hass()

    @callback
    def _bind_power(self) -> None:
        registry = er.async_get(self.hass)
        reference = self._power_registry_id or self._configured_power_id
        entity_id = er.async_resolve_entity_id(registry, reference) if reference else None
        registered = registry.async_get(entity_id) if entity_id else None
        if entity_id and (
            not entity_id.startswith("switch.")
            or entity_id == self.entity_id
            or (registered and registered.platform == DOMAIN)
        ):
            entity_id = None
        if registered and self._power_registry_id is None:
            self._power_registry_id = registered.id
        if entity_id == self._power_entity_id:
            return
        if self._unsubscribe_power:
            self._unsubscribe_power()
            self._unsubscribe_power = None
        self._power_entity_id = entity_id
        if entity_id:
            self._unsubscribe_power = async_track_state_change_event(self.hass, entity_id, self._async_power_changed)

    @callback
    def _read_power(self) -> None:
        state = self.hass.states.get(self._power_entity_id) if self._power_entity_id else None
        registered = er.async_get(self.hass).async_get(self._power_entity_id) if self._power_entity_id else None
        trusted = state and not state.attributes.get("restored") and (registered is None or not registered.disabled)
        power = state.state if trusted and state.state in ("on", "off") else None
        if power == self._power:
            return
        self._power = power
        self._source_recovered = False
        self._power_on_at = state.last_changed if power == "on" else None
        if self._cancel_grace:
            self._cancel_grace()
            self._cancel_grace = None
        if power == "on":
            remaining = max(0, self._startup_grace - (dt_util.utcnow() - self._power_on_at).total_seconds())
            self._cancel_grace = async_call_later(self.hass, remaining, self._grace_finished)

    @callback
    def _grace_finished(self, _now) -> None:
        self._cancel_grace = None
        self.async_write_ha_state()

    @callback
    def _async_power_changed(self, event: Event) -> None:
        self._read_power()
        self.async_write_ha_state()
        if self._source_ready:
            self._schedule_source_work()

    @callback
    def _async_source_changed(self, event: Event) -> None:
        if self._power_linked:
            old = event.data.get("old_state")
            if self._power == "on" and super().available and (old is None or old.state in ("unavailable", "unknown")):
                self._source_recovered = True
            if self._source_ready:
                self._source_recovered = True
                self._schedule_source_work()
        self.async_write_ha_state()

    @callback
    def _async_registry_changed(self, event: Event) -> None:
        super()._async_registry_changed(event)
        if not self._power_linked:
            return
        self._bind_power()
        self._read_power()
        if self._source_identity != self._cache_identity:
            # A newly registered/replaced source cannot inherit another target.
            self._cache = None
            self._cache_identity = self._source_identity
            self._cache_store = ClimateCache(self.hass, self._station_id, self._cache_identity)
        self.async_write_ha_state()

    @callback
    def _schedule_source_work(self) -> None:
        if self._cache_store is not None and not self._removed:
            # An event received during a failing command must not turn into an
            # immediate retry after that in-flight command completes.
            task = self.hass.async_create_task(self._async_source_work(not self._flush_inflight))
            self._source_tasks.add(task)
            task.add_done_callback(self._source_tasks.discard)

    async def _async_source_work(self, allow_flush: bool) -> None:
        if self._removed or not self._source_ready:
            return
        await self._async_capture(self.source_state)
        if allow_flush and not self._removed:
            await self._async_flush_pending()

    def _snapshot_data(self, state: State) -> dict[str, Any] | None:
        try:
            mode = HVACMode(state.state).value
        except ValueError:
            return None
        attributes: dict[str, Any] = {}
        for key in (ATTR_CURRENT_TEMPERATURE, *_TARGET_KEYS, ATTR_MIN_TEMP, ATTR_MAX_TEMP, ATTR_TARGET_TEMP_STEP):
            value = state.attributes.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                attributes[key] = value
        attributes["temperature_unit"] = self.native_temperature_unit
        attributes[ATTR_SUPPORTED_FEATURES] = int(self.supported_features)
        attributes[ATTR_HVAC_MODES] = [mode.value for mode in self.hvac_modes]
        if self.hvac_action is not None:
            attributes[ATTR_HVAC_ACTION] = self.hvac_action.value
        return {
            "source_identity": self._source_identity,
            "source_time": state.last_reported.isoformat(),
            "state": mode,
            "attributes": attributes,
            "pending": None,
        }

    async def _async_capture(self, state: State | None) -> None:
        if state is None or not self._source_ready or self._cache_store is None:
            return
        if self._power == "on":
            self._source_recovered = True
        snapshot = self._snapshot_data(state)
        if snapshot is None:
            return
        async with self._cache_lock:
            snapshot["pending"] = self._cache.get("pending") if self._cache else None
            try:
                await self._cache_store.async_save(snapshot)
            except Exception:  # A cache write must never hide a live thermostat.
                _LOGGER.warning("Cannot persist pool thermostat snapshot", exc_info=True)
            self._cache = snapshot

    async def _async_flush_pending(self) -> None:
        if (
            self._removed
            or self._flush_inflight
            or self._power != "on"
            or not self._source_ready
            or not self._cache
            or self._cache_store is None
        ):
            return
        self._flush_inflight = True
        try:
            async with self._command_lock:
                # A newer online command may have superseded this pending
                # target while waiting for the same command lock.
                pending = self._cache.get("pending") if self._cache else None
                if (
                    self._removed
                    or self._power != "on"
                    or not pending
                    or self._cache_identity != self._source_identity
                    or not self._source_ready
                ):
                    return
                self._validate_targets(pending["targets"], pending["unit"])
                data = {
                    key: TemperatureConverter.convert(value, pending["unit"], self.hass.config.units.temperature_unit)
                    for key, value in pending["targets"].items()
                }
                await self._async_forward(SERVICE_SET_TEMPERATURE, data)
                async with self._cache_lock:
                    if self._cache and self._cache.get("pending") == pending:
                        updated = {**self._cache, "pending": None}
                        await self._cache_store.async_save(updated)
                        self._cache = updated
                        self._pending_error = None
        except Exception as error:
            # Retry only on a later source event, never in an immediate loop.
            self._pending_error = error.code if isinstance(error, _TargetValidationError) else "send_failed"
            _LOGGER.warning("Pending pool thermostat target was not confirmed; retaining it", exc_info=True)
        finally:
            self._flush_inflight = False
            if not self._removed:
                self.async_write_ha_state()

    async def _async_forward(self, service: str, data: dict[str, Any] | None = None) -> None:
        if self._removed or not self._source_ready:
            raise HomeAssistantError("Wait for the selected pool thermostat to reconnect")
        await super()._async_forward(service, data)

    def _attribute(self, key: str, default: Any = None) -> Any:
        """Read one source attribute without copying unrelated attributes."""
        if self._held:
            return self._cache["attributes"].get(key, default)
        if (state := self.source_state) is None:
            return default
        return state.attributes.get(key, default)

    def _temperature(self, key: str) -> float | None:
        """Keep missing and non-finite source temperatures unknown."""
        if self._held and (pending := self._cache.get("pending")) and key in pending["targets"]:
            return TemperatureConverter.convert(pending["targets"][key], pending["unit"], self.native_temperature_unit)
        value = self._attribute(key)
        if isinstance(value, bool):
            return None
        try:
            value = float(value)
        except TypeError, ValueError:
            return None
        return value if math.isfinite(value) else None

    @property
    def native_temperature_unit(self) -> str:
        """Source state temperatures are already in its displayed unit.

        HA 2026.11's native API converts these for display. Service forwarding
        converts back to HA's service unit, avoiding double conversion.
        """
        unit = self._attribute("temperature_unit", self.hass.config.units.temperature_unit)
        if unit in TemperatureConverter.VALID_UNITS:
            return unit
        return self.hass.config.units.temperature_unit

    @property
    def native_current_temperature(self) -> float | None:
        """Mirror the source's measured temperature."""
        return self._temperature(ATTR_CURRENT_TEMPERATURE)

    @property
    def native_target_temperature(self) -> float | None:
        """Mirror the source's single target temperature."""
        return self._temperature(ATTR_TEMPERATURE)

    @property
    def native_target_temperature_low(self) -> float | None:
        """Mirror a source thermostat's lower target."""
        return self._temperature(ATTR_TARGET_TEMP_LOW)

    @property
    def native_target_temperature_high(self) -> float | None:
        """Mirror a source thermostat's upper target."""
        return self._temperature(ATTR_TARGET_TEMP_HIGH)

    @property
    def _source_min_temp(self) -> float:
        """Retain the original source minimum for command validation."""
        value = self._temperature(ATTR_MIN_TEMP)
        return value if value is not None else super().min_temp

    @property
    def min_temp(self) -> float:
        """Keep metadata ordered even when the source range is incompatible.

        Such a thermostat is unavailable and its commands still validate
        against the original source minimum, never this display-only clamp.
        """
        return min(self._source_min_temp, self.max_temp)

    @property
    def max_temp(self) -> float:
        """Apply the selected pool limit while retaining a lower source limit."""
        cap = TemperatureConverter.convert(
            self._max_target_temperature, UnitOfTemperature.CELSIUS, self.native_temperature_unit
        )
        value = self._temperature(ATTR_MAX_TEMP)
        return min(value, cap) if value is not None else cap

    def _validate_targets(self, targets: dict[str, Any], unit: str) -> None:
        """Validate scalar/range targets against current native source limits."""
        if not targets:
            return
        source_min = self._source_min_temp
        maximum = self.max_temp
        if source_min > maximum:
            raise _TargetValidationError("target_out_of_range", "The source thermostat range exceeds the pool limit")
        if targets.keys() & {ATTR_TARGET_TEMP_LOW, ATTR_TARGET_TEMP_HIGH}:
            if set(targets) != {ATTR_TARGET_TEMP_LOW, ATTR_TARGET_TEMP_HIGH}:
                raise _TargetValidationError("invalid_target", "Both lower and upper targets are required")
        for key, value in targets.items():
            feature = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                if key == ATTR_TEMPERATURE
                else ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            )
            if not self.supported_features & feature:
                raise _TargetValidationError(
                    "unsupported_target", "The selected thermostat does not support this target"
                )
            try:
                finite = not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise _TargetValidationError("invalid_target", "A finite target temperature is required")
            native = TemperatureConverter.convert(value, unit, self.native_temperature_unit)
            if not source_min <= native <= maximum:
                raise _TargetValidationError(
                    "target_out_of_range",
                    f"The pool target must be between {source_min:g} and {maximum:g} {self.native_temperature_unit}",
                )
        if ATTR_TARGET_TEMP_LOW in targets and targets[ATTR_TARGET_TEMP_LOW] > targets[ATTR_TARGET_TEMP_HIGH]:
            raise _TargetValidationError("invalid_target", "The lower target must not exceed the upper target")

    @property
    def target_temperature_step(self) -> float | None:
        """Mirror the source's target adjustment step."""
        return self._temperature(ATTR_TARGET_TEMP_STEP)

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Expose only source features with implemented forwarding methods."""
        try:
            return ClimateEntityFeature(self._attribute(ATTR_SUPPORTED_FEATURES, 0)) & _SUPPORTED_FEATURES
        except TypeError, ValueError:
            return ClimateEntityFeature(0)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Mirror the source mode without inferring active heating."""
        if not self.available:
            return None
        try:
            return HVACMode(self._cache["state"] if self._held else self.source_state.state)
        except ValueError:
            return None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Only advertise valid modes actually supplied by the source."""
        modes = self._attribute(ATTR_HVAC_MODES, [])
        if not isinstance(modes, (list, tuple)):
            return []
        return [HVACMode(mode) for mode in modes if mode in HVACMode]

    @property
    def hvac_action(self) -> HVACAction | None:
        """Only expose an action when the source reports one."""
        if self._held:
            return None
        try:
            return HVACAction(self._attribute(ATTR_HVAC_ACTION))
        except TypeError, ValueError:
            return None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Forward a supported mode to the existing thermostat."""
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError("The selected thermostat does not support this mode")
        await self._async_forward(SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: hvac_mode})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Forward a target or persist its latest value during a confirmed pause."""
        native_targets = {key: kwargs[key] for key in _TARGET_KEYS if key in kwargs}
        command_unit = self.native_temperature_unit
        self._validate_targets(native_targets, command_unit)
        data = {
            key: TemperatureConverter.convert(value, command_unit, self.hass.config.units.temperature_unit)
            for key, value in native_targets.items()
        }
        if self._power_linked and self._power == "off":
            if ATTR_HVAC_MODE in kwargs:
                raise ServiceValidationError("Only a temperature target can be saved during a power pause")
            if not native_targets or not self._held or self._cache_store is None:
                raise HomeAssistantError("No validated thermostat snapshot is available for a pending target")
            async with self._cache_lock:
                self._validate_targets(native_targets, command_unit)
                updated = {
                    **self._cache,
                    "pending": {"unit": command_unit, "targets": native_targets},
                }
                try:
                    await self._cache_store.async_save(updated)
                except Exception as err:
                    raise HomeAssistantError("The pending thermostat target could not be saved") from err
                self._cache = updated
                self._pending_error = None
            self.async_write_ha_state()
            return
        if ATTR_HVAC_MODE in kwargs:
            if kwargs[ATTR_HVAC_MODE] not in self.hvac_modes:
                raise ServiceValidationError("The selected thermostat does not support this mode")
            data[ATTR_HVAC_MODE] = kwargs[ATTR_HVAC_MODE]
        if not data:
            raise ServiceValidationError("A target temperature or mode is required")
        async with self._command_lock:
            if self._removed or not self._source_ready:
                raise HomeAssistantError("Wait for the selected pool thermostat to reconnect")
            self._validate_targets(native_targets, command_unit)
            if native_targets and self._cache and self._cache.get("pending") and self._cache_store is not None:
                # A new direct user target supersedes any older queued target.
                # Persist cancellation before forwarding so it cannot reappear
                # after restart or race with a waiting recovery flush.
                async with self._cache_lock:
                    updated = {**self._cache, "pending": None}
                    try:
                        await self._cache_store.async_save(updated)
                    except Exception as err:
                        raise HomeAssistantError("The older pending thermostat target could not be cleared") from err
                    self._cache = updated
                    self._pending_error = None
            self._validate_targets(native_targets, command_unit)
            await self._async_forward(SERVICE_SET_TEMPERATURE, data)

    async def async_turn_on(self) -> None:
        """Delegate the source's explicit turn-on operation."""
        if not self.supported_features & ClimateEntityFeature.TURN_ON:
            raise ServiceValidationError("The selected thermostat does not support turn on")
        await self._async_forward(SERVICE_TURN_ON)

    async def async_turn_off(self) -> None:
        """Delegate the source's explicit turn-off operation."""
        if not self.supported_features & ClimateEntityFeature.TURN_OFF:
            raise ServiceValidationError("The selected thermostat does not support turn off")
        await self._async_forward(SERVICE_TURN_OFF)
