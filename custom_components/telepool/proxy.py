"""External entity tracking shared by the optional pool controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.event import async_track_state_change_event, async_track_state_report_event

from .const import DOMAIN


class StationReference(Protocol):
    """The station fields needed for the logical pool device."""

    id: str
    name: str


class TelePoolSourceEntity(Entity):
    """Mirror a selected HA entity without coupling it to cloud telemetry.

    The registry UUID identifies the source across renames. State notifications
    only refresh this entity: they never write back to the source.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _track_source_reports = False

    def __init__(
        self,
        station: StationReference,
        source_entity_id: str,
        registry_id: str | None,
        source_domain: str,
    ) -> None:
        """Remember the selected source and logical station."""
        super().__init__()
        self._configured_source_id = source_entity_id
        self._source_registry_id = registry_id
        self._source_domain = source_domain
        self._source_entity_id: str | None = None
        self._unsubscribe_source: Callable[[], None] | None = None
        self._unsubscribe_report: Callable[[], None] | None = None
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, station.id)}, name=station.name)

    @property
    def source_entity_id(self) -> str | None:
        """Return the current, validated source entity ID."""
        return self._source_entity_id

    @property
    def source_state(self) -> State | None:
        """Read the source without retaining stale state snapshots."""
        if self._source_entity_id is None:
            return None
        return self.hass.states.get(self._source_entity_id)

    @property
    def available(self) -> bool:
        """Availability comes solely from the source, not the cloud."""
        if (state := self.source_state) is None:
            return False
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        if state.attributes.get("restored"):
            return False
        entry = er.async_get(self.hass).async_get(state.entity_id)
        return entry is None or not entry.disabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Identify which existing entity provides this control."""
        return {"source_entity_id": self._source_entity_id}

    async def async_added_to_hass(self) -> None:
        """Subscribe to the source and registry changes."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_changed))
        self.async_on_remove(self._async_unsubscribe_source)
        self._async_bind_source()

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Keep the source guard valid if this proxy itself is renamed."""
        super().async_entity_id_changed(old_entity_id)
        self._async_bind_source()

    @callback
    def _async_unsubscribe_source(self) -> None:
        """Remove the current source state listener."""
        if self._unsubscribe_source is not None:
            self._unsubscribe_source()
            self._unsubscribe_source = None
        if self._unsubscribe_report is not None:
            self._unsubscribe_report()
            self._unsubscribe_report = None

    @callback
    def _async_bind_source(self) -> None:
        """Resolve the source identity and follow its latest entity ID."""
        registry = er.async_get(self.hass)
        reference = self._source_registry_id or self._configured_source_id
        entity_id = er.async_resolve_entity_id(registry, reference)
        if entity_id is not None:
            entry = registry.async_get(entity_id)
            if (
                entity_id.partition(".")[0] != self._source_domain
                or entity_id == self.entity_id
                or (entry is not None and entry.platform == DOMAIN)
            ):
                entity_id = None
            elif entry is not None and self._source_registry_id is None:
                self._source_registry_id = entry.id

        if entity_id == self._source_entity_id:
            return
        self._async_unsubscribe_source()
        self._source_entity_id = entity_id
        if entity_id is not None:
            self._unsubscribe_source = async_track_state_change_event(self.hass, entity_id, self._async_source_changed)
            if self._track_source_reports:
                # A thermostat can reconnect and report exactly the same
                # measurement/target. HA updates last_reported for that case
                # without emitting a state_changed event.
                self._unsubscribe_report = async_track_state_report_event(
                    self.hass, entity_id, self._async_source_changed
                )

    @callback
    def _async_source_changed(self, event: Event) -> None:
        """Reflect a source state update without issuing commands."""
        self.async_write_ha_state()

    @callback
    def _async_registry_changed(self, event: Event) -> None:
        """Follow a source rename, removal, or enabled-state change."""
        current_ids = {value for value in (self._source_entity_id, self._configured_source_id) if value}
        if event.data.get("entity_id") not in current_ids and event.data.get("old_entity_id") not in current_ids:
            return
        self._async_bind_source()
        self.async_write_ha_state()

    async def _async_forward(self, service: str, data: dict[str, Any] | None = None) -> None:
        """Delegate one explicit user command to the available source."""
        if not self.available or self._source_entity_id is None:
            raise HomeAssistantError("The selected pool control source is unavailable")
        await self.hass.services.async_call(
            self._source_domain,
            service,
            {**(data or {}), ATTR_ENTITY_ID: self._source_entity_id},
            blocking=True,
            context=self._context,
        )
