"""Shared power pauses retain display state and one durable target only."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.components.climate.const import DATA_COMPONENT
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.telepool.climate import TelePoolClimate

STATION = SimpleNamespace(id="pause-station", name="Pool")
ATTRIBUTES = {
    "temperature_unit": "°C",
    "current_temperature": 26.5,
    "temperature": 27,
    "min_temp": 5,
    "max_temp": 35,
    "target_temp_step": 0.5,
    "hvac_modes": ["off", "heat"],
    "hvac_action": "heating",
    "supported_features": int(
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
    ),
}


@pytest.fixture
async def make_climate(hass):
    entities = []

    async def make(*, registry_id=None, power_linked=True, attributes=None):
        entity = TelePoolClimate(
            STATION,
            "climate.external",
            registry_id,
            power_linked=power_linked,
            power_source="switch.external",
        )
        entity.hass = hass
        entity.entity_id = "climate.pool"
        entity.async_write_ha_state = Mock()
        await entity.async_added_to_hass()
        entities.append(entity)
        await hass.async_block_till_done()
        return entity

    yield make
    for entity in entities:
        entity._call_on_remove_callbacks()
    await hass.async_block_till_done()


async def prime(hass, make_climate, **kwargs):
    hass.states.async_set("switch.external", "on")
    hass.states.async_set("climate.external", "heat", kwargs.pop("attributes", ATTRIBUTES))
    return await make_climate(**kwargs)


async def pause(hass):
    hass.states.async_set("switch.external", "off")
    hass.states.async_set("climate.external", "unavailable")
    await hass.async_block_till_done()


async def reconnect(hass, attributes=None):
    hass.states.async_set("switch.external", "on")
    await hass.async_block_till_done()
    hass.states.async_set("climate.external", "heat", attributes or ATTRIBUTES)
    await hass.async_block_till_done()


async def test_off_holds_valid_snapshot_and_only_latest_target(hass, make_climate):
    entity = await prime(hass, make_climate)
    source_time = entity.extra_state_attributes["source_time"]
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    await pause(hass)

    assert entity.available and entity.hvac_mode is HVACMode.HEAT
    assert entity.native_current_temperature == 26.5
    assert entity.native_target_temperature == 27
    assert entity.hvac_action is None
    assert entity.extra_state_attributes["held"]
    assert not entity.extra_state_attributes["data_fresh"]
    await entity.async_set_temperature(temperature=28)
    await entity.async_set_temperature(temperature=29)

    assert entity.native_target_temperature == 29
    assert entity.extra_state_attributes["reported_target"]["targets"] == {"temperature": 27}
    assert entity.extra_state_attributes["source_time"] == source_time
    assert entity.extra_state_attributes["pending_target"] == {"unit": "°C", "targets": {"temperature": 29}}
    service.assert_not_called()
    for command in (entity.async_turn_on, entity.async_turn_off):
        with pytest.raises(HomeAssistantError, match="reconnect"):
            await command()
    with pytest.raises(HomeAssistantError):
        await entity.async_set_hvac_mode(HVACMode.OFF)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=30, hvac_mode="off")
    assert entity.extra_state_attributes["pending_target"]["targets"] == {"temperature": 29}


async def test_restart_restores_snapshot_and_pending_without_advancing_source_time(hass, make_climate):
    first = await prime(hass, make_climate)
    await pause(hass)
    await first.async_set_temperature(temperature=29)
    source_time = first.extra_state_attributes["source_time"]
    first._call_on_remove_callbacks()

    restored = await make_climate()

    assert restored.available
    assert restored.native_current_temperature == 26.5
    assert restored.native_target_temperature == 29
    assert restored.extra_state_attributes["source_time"] == source_time
    assert restored.extra_state_attributes["pending_target"]["targets"] == {"temperature": 29}


async def test_on_has_bounded_startup_grace_but_does_not_accept_offline_targets(hass, make_climate):
    entity = await prime(hass, make_climate)
    await pause(hass)
    hass.states.async_set("switch.external", "on")
    await hass.async_block_till_done()

    assert entity.available
    assert entity.extra_state_attributes["power_link_status"] == "starting"
    with pytest.raises(HomeAssistantError, match="reconnect"):
        await entity.async_set_temperature(temperature=29)
    assert entity.extra_state_attributes["pending_target"] is None
    with patch(
        "custom_components.telepool.climate.dt_util.utcnow", return_value=entity._power_on_at + timedelta(seconds=181)
    ):
        assert not entity.available
        assert entity.extra_state_attributes["power_link_status"] == "unavailable"


@pytest.mark.parametrize("relay,attributes", [("unknown", {}), ("off", {"restored": True})])
async def test_unknown_or_restored_relay_does_not_authorize_cache_or_queue(hass, make_climate, relay, attributes):
    entity = await prime(hass, make_climate)
    hass.states.async_set("switch.external", relay, attributes)
    hass.states.async_set("climate.external", "unavailable")
    await hass.async_block_till_done()
    assert not entity.available
    with pytest.raises(HomeAssistantError):
        await entity.async_set_temperature(temperature=29)
    assert entity.extra_state_attributes["pending_target"] is None


async def test_unknown_relay_never_flushes_old_pending_even_with_live_source(hass, make_climate):
    entity = await prime(hass, make_climate)
    await pause(hass)
    await entity.async_set_temperature(temperature=29)
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    hass.states.async_set("switch.external", "unknown")
    hass.states.async_set("climate.external", "heat", ATTRIBUTES)
    await hass.async_block_till_done()

    assert entity.available and entity.extra_state_attributes["data_fresh"]
    assert entity.extra_state_attributes["pending_target"]["targets"] == {"temperature": 29}
    service.assert_not_called()


async def test_unload_while_capture_waits_cancels_work_and_never_flushes(hass, make_climate):
    entity = await prime(hass, make_climate)
    await pause(hass)
    await entity.async_set_temperature(temperature=29)
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_capture(state):
        started.set()
        await release.wait()

    with patch.object(entity, "_async_capture", side_effect=blocked_capture):
        hass.states.async_set("switch.external", "on")
        await hass.async_block_till_done()
        hass.states.async_set("climate.external", "heat", ATTRIBUTES)
        await started.wait()
        await entity.async_will_remove_from_hass()
        release.set()
        await hass.async_block_till_done()

    service.assert_not_called()
    assert entity._source_tasks == set()
    assert entity.extra_state_attributes["pending_target"]["targets"] == {"temperature": 29}


async def test_pending_flushes_once_after_new_live_source_and_converts_current_service_unit(hass, make_climate):
    entity = await prime(hass, make_climate)
    await pause(hass)
    await entity.async_set_temperature(temperature=30)
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    hass.config.units = US_CUSTOMARY_SYSTEM

    await reconnect(hass)

    service.assert_awaited_once()
    assert service.call_args.args[0].data == {"entity_id": "climate.external", "temperature": 86}
    assert entity.extra_state_attributes["pending_target"] is None
    # The service returning successfully is not a physical target confirmation.
    assert entity.native_target_temperature == 27
    hass.states.async_set("climate.external", "heat", {**ATTRIBUTES, "current_temperature": 26.6})
    await hass.async_block_till_done()
    assert service.await_count == 1


@pytest.mark.parametrize("identical_report", [True, False])
async def test_old_valid_source_is_not_reconnected_until_a_new_report(hass, make_climate, identical_report):
    entity = await prime(hass, make_climate)
    hass.states.async_set("switch.external", "off")
    await hass.async_block_till_done()
    await entity.async_set_temperature(temperature=29)
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    # Some integrations leave a cached 'heat' state while hardware is off.
    hass.states.async_set("switch.external", "on")
    await hass.async_block_till_done()
    assert entity.extra_state_attributes["power_link_status"] == "starting"
    service.assert_not_called()
    attributes = ATTRIBUTES if identical_report else {**ATTRIBUTES, "current_temperature": 26.6}
    hass.states.async_set("climate.external", "heat", attributes)
    await hass.async_block_till_done()
    service.assert_awaited_once()


async def test_flush_failure_keeps_pending_and_retries_only_on_later_source_event(hass, make_climate):
    entity = await prime(hass, make_climate)
    await pause(hass)
    await entity.async_set_temperature(temperature=29)
    service = AsyncMock(side_effect=HomeAssistantError("source rejected target"))
    hass.services.async_register("climate", "set_temperature", service)
    await reconnect(hass)
    await hass.async_block_till_done()
    assert service.await_count == 1
    assert entity.extra_state_attributes["pending_target"]["targets"] == {"temperature": 29}
    assert entity.native_target_temperature == 27
    service.side_effect = None
    hass.states.async_set("climate.external", "heat", {**ATTRIBUTES, "current_temperature": 26.6})
    await hass.async_block_till_done()
    assert service.await_count == 2
    assert entity.extra_state_attributes["pending_target"] is None


async def test_replaced_registry_identity_cannot_receive_previous_pending(hass, make_climate):
    registry = er.async_get(hass)
    original = registry.async_get_or_create("climate", "test", "original", suggested_object_id="external")
    first = await prime(hass, make_climate, registry_id=original.id)
    await pause(hass)
    await first.async_set_temperature(temperature=29)
    first._call_on_remove_callbacks()
    registry.async_remove(original.entity_id)
    replacement = registry.async_get_or_create("climate", "test", "replacement", suggested_object_id="external")
    assert replacement.id != original.id
    second = await make_climate(registry_id=replacement.id)
    assert not second.available
    assert second.extra_state_attributes["pending_target"] is None
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    await reconnect(hass)
    service.assert_not_called()


async def test_queue_write_failure_reports_error_without_optimistic_pending(hass, make_climate):
    entity = await prime(hass, make_climate)
    await pause(hass)
    with patch.object(entity._cache_store, "async_save", side_effect=OSError("disk full")):
        with pytest.raises(HomeAssistantError, match="could not be saved"):
            await entity.async_set_temperature(temperature=29)
    assert entity.extra_state_attributes["pending_target"] is None
    assert entity.native_target_temperature == 27


async def test_range_queue_requires_complete_pair_and_never_saves_mode(hass, make_climate):
    attributes = {
        **ATTRIBUTES,
        "target_temp_low": 26,
        "target_temp_high": 28,
        "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE),
    }
    entity = await prime(hass, make_climate, attributes=attributes)
    await pause(hass)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(target_temp_low=25)
    assert entity.extra_state_attributes["pending_target"] is None
    await entity.async_set_temperature(target_temp_low=25, target_temp_high=29)
    assert entity.extra_state_attributes["pending_target"]["targets"] == {"target_temp_low": 25, "target_temp_high": 29}


@pytest.mark.parametrize(
    ("unit", "minimum", "maximum", "cap", "above_cap"),
    [("°C", 5, 60, 38, 38.1), ("°F", 41, 140, 100.4, 100.5), ("K", 278.15, 333.15, 311.15, 311.25)],
)
async def test_paused_target_respects_38c_cap_in_cached_native_units(
    hass, make_climate, unit, minimum, maximum, cap, above_cap
):
    attributes = {**ATTRIBUTES, "temperature_unit": unit, "min_temp": minimum, "max_temp": maximum}
    entity = await prime(hass, make_climate, attributes=attributes)
    await pause(hass)

    assert entity.max_temp == pytest.approx(cap)
    await entity.async_set_temperature(temperature=cap)
    with pytest.raises(ServiceValidationError, match="between"):
        await entity.async_set_temperature(temperature=above_cap)
    assert entity.extra_state_attributes["pending_target"] == {"unit": unit, "targets": {"temperature": cap}}
    assert entity.extra_state_attributes["pending_error"] is None


async def test_paused_older_35c_source_limit_is_not_silently_relaxed(hass, make_climate):
    first = await prime(hass, make_climate)
    await pause(hass)
    first._call_on_remove_callbacks()
    restored = await make_climate()

    assert restored.max_temp == 35
    with pytest.raises(ServiceValidationError, match="between"):
        await restored.async_set_temperature(temperature=38)
    assert restored.extra_state_attributes["pending_target"] is None


@pytest.mark.parametrize("source_max,pending_target", [(60, 39), (35, 38)])
async def test_invalid_legacy_pending_is_retained_with_error_and_never_forwarded(
    hass, make_climate, source_max, pending_target
):
    first = await prime(hass, make_climate, attributes={**ATTRIBUTES, "max_temp": 60})
    await pause(hass)
    # Simulate a target persisted before the pool cap was introduced.
    legacy = {**first._cache, "pending": {"unit": "°C", "targets": {"temperature": pending_target}}}
    await first._cache_store.async_save(legacy)
    first._call_on_remove_callbacks()
    restored = await make_climate()
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)

    await reconnect(hass, {**ATTRIBUTES, "max_temp": source_max})

    service.assert_not_called()
    assert restored.extra_state_attributes["pending_target"] == legacy["pending"]
    assert restored.extra_state_attributes["pending_error"] == "target_out_of_range"
    # An explicit replacement within the current limits clears the old intent.
    await restored.async_set_temperature(temperature=min(source_max, 38))
    service.assert_awaited_once()
    assert restored.extra_state_attributes["pending_target"] is None
    assert restored.extra_state_attributes["pending_error"] is None


async def test_paused_range_validates_both_bounds_and_flush_rechecks_new_source_limits(hass, make_climate):
    attributes = {
        **ATTRIBUTES,
        "max_temp": 60,
        "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE),
        "target_temp_low": 26,
        "target_temp_high": 28,
    }
    entity = await prime(hass, make_climate, attributes=attributes)
    await pause(hass)
    await entity.async_set_temperature(target_temp_low=5, target_temp_high=38)
    for low, high in ((5, 38.1), (4.9, 38)):
        with pytest.raises(ServiceValidationError):
            await entity.async_set_temperature(target_temp_low=low, target_temp_high=high)
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    await reconnect(hass, {**attributes, "max_temp": 35})

    service.assert_not_called()
    assert entity.extra_state_attributes["pending_target"]["targets"] == {"target_temp_low": 5, "target_temp_high": 38}
    assert entity.extra_state_attributes["pending_error"] == "target_out_of_range"


async def test_pending_is_not_flushed_when_reconnected_source_minimum_exceeds_pool_limit(hass, make_climate):
    entity = await prime(hass, make_climate, attributes={**ATTRIBUTES, "max_temp": 60})
    await pause(hass)
    await entity.async_set_temperature(temperature=38)
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)

    await reconnect(hass, {**ATTRIBUTES, "min_temp": 50, "max_temp": 60})

    assert not entity.available
    assert (entity.min_temp, entity.max_temp) == (38, 38)
    assert entity.extra_state_attributes["pending_target"]["targets"] == {"temperature": 38}
    assert entity.extra_state_attributes["pending_error"] == "target_out_of_range"
    service.assert_not_called()


async def test_new_online_target_waits_for_pending_flush_and_wins(hass, make_climate):
    entity = await prime(hass, make_climate)
    await pause(hass)
    await entity.async_set_temperature(temperature=28)
    started, release = asyncio.Event(), asyncio.Event()
    received = []

    async def service(call):
        received.append(call.data["temperature"])
        if len(received) == 1:
            started.set()
            await release.wait()

    hass.services.async_register("climate", "set_temperature", service)
    hass.states.async_set("switch.external", "on")
    await hass.async_block_till_done()
    hass.states.async_set("climate.external", "heat", ATTRIBUTES)
    await started.wait()
    newer = hass.async_create_task(entity.async_set_temperature(temperature=30))
    await asyncio.sleep(0)
    assert received == [28]
    release.set()
    await newer
    await hass.async_block_till_done()
    assert received == [28, 30]
    assert entity.extra_state_attributes["pending_target"] is None


async def test_real_ha_service_queues_during_pause_and_reaches_only_source_on_reconnect(hass):
    """Exercise the actual climate service unit conversions with fake hardware."""
    assert await async_setup_component(hass, "climate", {})
    component = hass.data[DATA_COMPONENT]

    class PhysicalThermostat(ClimateEntity):
        _attr_name = "External thermostat"
        _attr_should_poll = False
        _attr_hvac_mode = HVACMode.HEAT
        _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
        _attr_native_temperature_unit = "°C"
        _attr_native_current_temperature = 26.5
        _attr_native_target_temperature = 27
        _enable_turn_on_off_backwards_compatibility = False

        def __init__(self):
            self.commands = []

        async def async_set_temperature(self, **kwargs):
            self.commands.append(kwargs["temperature"])
            self._attr_native_target_temperature = kwargs["temperature"]
            self.async_write_ha_state()

    hass.states.async_set("switch.external", "on")
    source = PhysicalThermostat()
    source.entity_id = "climate.external"
    await component.async_add_entities([source])
    entity = TelePoolClimate(STATION, source.entity_id, power_linked=True, power_source="switch.external")
    entity.entity_id = "climate.pool"
    entity._attr_device_info = None
    entity._attr_name = "Pool thermostat"
    await component.async_add_entities([entity])
    try:
        hass.states.async_set("switch.external", "off")
        source._attr_available = False
        source.async_write_ha_state()
        await hass.async_block_till_done()
        assert hass.states.get(entity.entity_id).state == "heat"
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": entity.entity_id, "temperature": 30}, blocking=True
        )
        assert source.commands == []
        assert hass.states.get(entity.entity_id).attributes["temperature"] == 30
        assert hass.states.get(entity.entity_id).attributes["reported_target"]["targets"] == {"temperature": 27}
        hass.config.units = US_CUSTOMARY_SYSTEM
        hass.states.async_set("switch.external", "on")
        await hass.async_block_till_done()
        source._attr_available = True
        source.async_write_ha_state()
        await hass.async_block_till_done()
        assert source.commands == [30]
        assert entity.extra_state_attributes["pending_target"] is None
        assert hass.states.get(entity.entity_id).attributes["temperature"] == 86
        # After a successful reconnect a later connection loss is a fault,
        # rather than restarting the already completed startup grace.
        source._attr_available = False
        source.async_write_ha_state()
        await hass.async_block_till_done()
        assert not entity.available
    finally:
        await entity.async_remove(force_remove=True)
