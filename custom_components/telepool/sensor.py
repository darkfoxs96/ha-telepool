"""Truthful measurements and optional held export copies."""

from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)

from .entity import TelePoolEntity


@dataclass(frozen=True)
class Reading:
    key: str
    source: str
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    fresh_only: bool = False
    diagnostic: bool = False
    scale: float = 1


READINGS = (
    Reading("ph", "ph", SensorDeviceClass.PH),
    Reading("rx", "rx", unit=UnitOfElectricPotential.MILLIVOLT),
    Reading("temperature", "temperature", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    Reading("ph_measurement", "ph", SensorDeviceClass.PH, fresh_only=True, diagnostic=True),
    Reading("rx_measurement", "rx", unit=UnitOfElectricPotential.MILLIVOLT, fresh_only=True, diagnostic=True),
    Reading(
        "temperature_measurement",
        "temperature",
        SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.CELSIUS,
        fresh_only=True,
        diagnostic=True,
    ),
    Reading("set_ph", "set_ph", SensorDeviceClass.PH, diagnostic=True),
    Reading("set_rx", "set_rx", unit=UnitOfElectricPotential.MILLIVOLT, diagnostic=True),
    Reading("rssi", "rssi", SensorDeviceClass.SIGNAL_STRENGTH, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, diagnostic=True),
    Reading("quality", "quality", unit="%", diagnostic=True),
    *(Reading(f"mode_{key}", f"mode_{key}", diagnostic=True) for key in ("ph", "rx", "fu", "temperature")),
    Reading("ph_export", "ph", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    Reading("rx_export", "rx", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, scale=0.1),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = []
    for reading in READINGS:
        if reading.key.endswith("_export") and not entry.options.get("export_aliases", False):
            continue
        # Temperature entities may remain unavailable until a real sensor is connected.
        entities.append(TelePoolReading(coordinator, reading))
    entities.extend(
        TelePoolTimestamp(coordinator, key) for key in ("data_timestamp", "received_timestamp", "last_poll_timestamp")
    )
    entities.append(TelePoolStatus(coordinator))
    async_add_entities(entities)


class TelePoolReading(TelePoolEntity, SensorEntity):
    def __init__(self, coordinator, reading: Reading):
        super().__init__(coordinator, reading.key)
        self.reading = reading
        self._attr_native_unit_of_measurement = reading.unit
        self._attr_device_class = reading.device_class
        self._attr_entity_category = EntityCategory.DIAGNOSTIC if reading.diagnostic else None
        self._attr_entity_registry_enabled_default = not reading.fresh_only
        self._attr_state_class = SensorStateClass.MEASUREMENT if reading.fresh_only else None
        if reading.source == "ph":
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self):
        sample = self.coordinator.snapshot
        if sample is None:
            return None
        key = self.reading.source
        if key.startswith("mode_"):
            return sample.modes.get(key.removeprefix("mode_"))
        value = getattr(sample, key) if key in ("rssi", "quality") else sample.values.get(key)
        return value * self.reading.scale if value is not None else None

    @property
    def available(self) -> bool:
        availability = self.coordinator.availability
        return self.native_value is not None and (
            availability.fresh if self.reading.fresh_only else availability.display_available
        )

    @property
    def extra_state_attributes(self):
        sample = self.coordinator.snapshot
        return {
            "data_fresh": self.coordinator.availability.fresh,
            "data_timestamp": sample.observed_at.isoformat() if sample and sample.observed_at else None,
            **(
                {"export_scale": self.reading.scale, "original_quantity": self.reading.source}
                if self.reading.key.endswith("_export")
                else {}
            ),
        }


class TelePoolStatus(TelePoolEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["working", "paused", "starting", "no_connection"]
    _attr_icon = "mdi:pool"

    def __init__(self, coordinator):
        super().__init__(coordinator, "operating_status")

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return self.coordinator.availability.status

    @property
    def extra_state_attributes(self):
        return {
            "cloud_error": self.coordinator.cloud_error,
            "external_power": self.coordinator.policy.power or "unknown",
        }


class TelePoolTimestamp(TelePoolEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self._key = key

    @property
    def available(self):
        return self.native_value is not None

    @property
    def native_value(self):
        if self._key == "last_poll_timestamp":
            return self.coordinator.last_attempt
        if sample := self.coordinator.snapshot:
            return sample.observed_at if self._key == "data_timestamp" else sample.received_at
        return None

    @property
    def extra_state_attributes(self):
        if self._key != "last_poll_timestamp":
            return None
        last_success = self.coordinator.last_success
        return {"last_successful_read": last_success.isoformat() if last_success else None}
