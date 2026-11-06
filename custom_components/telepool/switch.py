"""Optional proxy for an existing pool power switch."""

from typing import Any

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import get_control_mode
from .proxy import StationReference, TelePoolSourceEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Create exactly the explicitly selected switch backend."""
    mode = get_control_mode(entry.options, "power")
    if mode == "native_q3":
        from .native_switch import TelePoolNativeFuSwitch

        async_add_entities([TelePoolNativeFuSwitch(entry.runtime_data.coordinator)])
    elif mode == "external" and (source := entry.options.get("power_source")):
        async_add_entities(
            [
                TelePoolPowerSwitch(
                    entry.runtime_data.station,
                    source,
                    entry.options.get("power_registry_id"),
                )
            ]
        )


class TelePoolPowerSwitch(TelePoolSourceEntity, SwitchEntity):
    """A pass-through control that stays usable when TelePool is offline."""

    _attr_translation_key = "power"

    def __init__(
        self,
        station: StationReference,
        source_entity_id: str,
        registry_id: str | None = None,
    ) -> None:
        """Initialize the stable logical pool control."""
        super().__init__(station, source_entity_id, registry_id, SWITCH_DOMAIN)
        self._attr_unique_id = f"{station.id}_power"

    @property
    def is_on(self) -> bool | None:
        """Mirror the physical switch's reported state."""
        if not self.available or (state := self.source_state) is None:
            return None
        return state.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the selected external switch."""
        await self._async_forward(SERVICE_TURN_ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the selected external switch."""
        await self._async_forward(SERVICE_TURN_OFF)
