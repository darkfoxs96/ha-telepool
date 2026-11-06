"""Common logical station device."""

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TelePoolCoordinator


class TelePoolEntity(CoordinatorEntity[TelePoolCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TelePoolCoordinator, key: str):
        super().__init__(coordinator)
        station = coordinator.station
        self._attr_unique_id = f"{station.id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, station.id)},
            name=station.name,
            manufacturer="Лаборатория Востротина",
            model="TelePool Q3",
            sw_version=station.firmware,
        )
