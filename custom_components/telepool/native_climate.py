"""Q3 temperature target control, with power-pause policy owned by the coordinator."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.unit_conversion import TemperatureDeltaConverter

from .const import POOL_MAX_TEMP_CELSIUS
from .entity import TelePoolEntity


def _finite(value: Any) -> float | None:
    """Do not manufacture a temperature from a missing/invalid Q3 value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


class TelePoolNativeClimate(TelePoolEntity, ClimateEntity):
    """Expose a Q3 setpoint without inferring heating activity or enable state."""

    _enable_turn_on_off_backwards_compatibility = False
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_TENTHS
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_action = None

    def __init__(self, coordinator) -> None:
        # Match the external proxy's unique ID so existing HomeKit mappings survive.
        super().__init__(coordinator, "climate")

    @property
    def _controls(self):
        return self.coordinator.native_controls

    def _control_number(self, field: str) -> float | None:
        return _finite(getattr(self._controls, field, None))

    @property
    def _source_minimum(self) -> float:
        value = self._control_number("min_temperature")
        return value if value is not None else super().min_temp

    @property
    def _valid_writable_range(self) -> bool:
        minimum = self._control_number("min_temperature")
        maximum = self._control_number("max_temperature")
        step = self._control_number("temperature_step")
        return bool(
            minimum is not None
            and maximum is not None
            and step is not None
            and step > 0
            and minimum <= min(maximum, POOL_MAX_TEMP_CELSIUS)
        )

    @property
    def available(self) -> bool:
        known_target = self._control_number("target_temperature") is not None
        return bool(
            self._controls is not None
            and known_target
            and (self.coordinator.native_available or self.coordinator.native_held)
            and self._source_minimum <= self.max_temp
        )

    @property
    def supported_features(self) -> ClimateEntityFeature:
        if self._controls and self._controls.can_set_temperature and self._valid_writable_range:
            return ClimateEntityFeature.TARGET_TEMPERATURE
        return ClimateEntityFeature(0)

    @property
    def hvac_mode(self) -> HVACMode | None:
        # HEAT identifies this single-setpoint thermostat's function. It is not
        # a claim that a heater, pump, relay, or Q3 temperature mode is running.
        return HVACMode.HEAT if self.available else None

    @property
    def native_current_temperature(self) -> float | None:
        snapshot = self.coordinator.snapshot
        return _finite(snapshot.values.get("temperature")) if snapshot else None

    @property
    def native_target_temperature(self) -> float | None:
        pending = _finite(self.coordinator.native_pending_target)
        if self.coordinator.native_held and pending is not None:
            return pending
        return self._control_number("target_temperature")

    @property
    def min_temp(self) -> float:
        # Keep exported metadata ordered; command validation uses the original
        # form minimum and rejects an incompatible source range entirely.
        return min(self._source_minimum, self.max_temp)

    @property
    def max_temp(self) -> float:
        maximum = self._control_number("max_temperature")
        return min(maximum, POOL_MAX_TEMP_CELSIUS) if maximum is not None else POOL_MAX_TEMP_CELSIUS

    @property
    def target_temperature_step(self) -> float | None:
        step = self._control_number("temperature_step")
        if step is None or step <= 0:
            return None
        # HA 2026.11 publishes this capability verbatim, unlike native target
        # temperatures. A native 0.5 °C increment must appear as 0.9 °F.
        if (hass := getattr(self, "hass", None)) is not None:
            return TemperatureDeltaConverter.convert(
                step, UnitOfTemperature.CELSIUS, hass.config.units.temperature_unit
            )
        return step

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pending = _finite(self.coordinator.native_pending_target)
        reported = self._control_number("target_temperature")
        return {
            "control_backend": "native_q3",
            "held": self.coordinator.native_held,
            "data_fresh": self.coordinator.native_available,
            "source_time": self.coordinator.native_source_time,
            "pending_target": {"unit": "°C", "targets": {"temperature": pending}} if pending is not None else None,
            "pending_error": self.coordinator.native_pending_error,
            "reported_target": ({"unit": "°C", "targets": {"temperature": reported}} if pending is not None else None),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Send or durably retain one Q3 target through the coordinator only."""
        if not self.available:
            raise HomeAssistantError("Wait for the Q3 temperature control to become available")
        if not self.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE:
            raise ServiceValidationError("The Q3 temperature target is not editable in the current control form")
        # HA 2026.11's entity service path includes entity_id in method kwargs;
        # routing still comes exclusively from this configured coordinator.
        if ATTR_TEMPERATURE not in kwargs or set(kwargs) - {ATTR_TEMPERATURE, ATTR_ENTITY_ID}:
            if set(kwargs) & {ATTR_TARGET_TEMP_LOW, ATTR_TARGET_TEMP_HIGH}:
                raise ServiceValidationError("Q3 provides one temperature target, not a target range")
            raise ServiceValidationError("Only the Q3 temperature target can be changed")
        value = _finite(kwargs[ATTR_TEMPERATURE])
        if value is None:
            raise ServiceValidationError("A finite Q3 target temperature is required")
        if not self._source_minimum <= value <= self.max_temp:
            raise ServiceValidationError(
                f"The Q3 target must be between {self._source_minimum:g} and {self.max_temp:g} °C"
            )
        step = self._control_number("temperature_step")
        minimum_decimal = Decimal(str(self._source_minimum))
        step_decimal = Decimal(str(step))
        requested = Decimal(str(value))
        nearest_steps = ((requested - minimum_decimal) / step_decimal).to_integral_value()
        canonical = minimum_decimal + nearest_steps * step_decimal
        # HA's F -> C conversion can produce 28.499999999999996 for an exact
        # 28.5 °C setting. Remove only that numeric noise, never round a target
        # such as 27.15 onto a 0.1 °C grid.
        if abs(requested - canonical) > Decimal("0.000000001"):
            raise ServiceValidationError("The Q3 target must follow the current control form's temperature step")
        # The coordinator revalidates the live form, owns the destination-bound
        # durable pending target, and performs no optimistic state confirmation.
        await self.coordinator.async_set_native_temperature(float(canonical))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        raise ServiceValidationError("Q3 heating enable and HVAC mode controls are not exposed")

    async def async_turn_on(self) -> None:
        raise ServiceValidationError("Q3 heating enable is not exposed")

    async def async_turn_off(self) -> None:
        raise ServiceValidationError("Q3 heating enable is not exposed")
