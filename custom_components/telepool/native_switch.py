"""Expose the exact editable Q3 MenuEnableFu flag, without pump feedback claims."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import TelePoolEntity


class TelePoolNativeFuSwitch(TelePoolEntity, SwitchEntity):
    """An immediate configuration control, not station power or pump state."""

    def __init__(self, coordinator) -> None:
        # Preserve the logical switch ID when its selected backend changes.
        super().__init__(coordinator, "power")
        self._attr_translation_key = "native_fu_menu"

    @property
    def available(self) -> bool:
        controls = self.coordinator.native_controls
        return bool(
            self.coordinator.native_available
            and controls is not None
            and controls.can_set_enabled
            and isinstance(controls.menu_enable_fu, bool)
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.native_controls.menu_enable_fu if self.available else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "control_backend": "native_q3",
            "configuration_field": "MenuEnableFu",
            "source_time": self.coordinator.native_source_time,
        }

    async def _async_set_enabled(self, enabled: bool) -> None:
        if not self.available:
            raise HomeAssistantError("Wait for the editable Q3 FU menu control to become available")
        # The coordinator/client verifies a fresh form and the read-back flag.
        # An unavailable source never queues an on/off operation.
        await self.coordinator.async_set_native_enabled(enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_enabled(False)
