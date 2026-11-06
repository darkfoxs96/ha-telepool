"""Freshness is separate from the station's unclassified error flag."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .entity import TelePoolEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        [TelePoolFreshness(entry.runtime_data.coordinator), TelePoolErrorFlag(entry.runtime_data.coordinator)]
    )


class TelePoolFreshness(TelePoolEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:update"

    def __init__(self, coordinator):
        super().__init__(coordinator, "data_fresh")

    @property
    def available(self):
        return True

    @property
    def is_on(self):
        return self.coordinator.availability.fresh


class TelePoolErrorFlag(TelePoolEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator):
        super().__init__(coordinator, "station_error")

    @property
    def available(self):
        return self.coordinator.availability.fresh and self.coordinator.snapshot is not None

    @property
    def is_on(self):
        return self.coordinator.snapshot.error if self.coordinator.snapshot else None
