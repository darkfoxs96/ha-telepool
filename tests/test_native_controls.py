"""Q3 entity behavior uses only coordinator mocks, never station requests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.components.climate.const import DATA_COMPONENT
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.telepool.climate import TelePoolClimate
from custom_components.telepool.climate import async_setup_entry as setup_climate
from custom_components.telepool.native_climate import TelePoolNativeClimate
from custom_components.telepool.native_switch import TelePoolNativeFuSwitch
from custom_components.telepool.switch import TelePoolPowerSwitch
from custom_components.telepool.switch import async_setup_entry as setup_switch


def coordinator(**changes):
    result = SimpleNamespace(
        station=SimpleNamespace(id="q3-test", name="Pool", firmware="3.3.0"),
        entry=SimpleNamespace(options={}),
        native_controls=SimpleNamespace(
            station_id="q3-test",
            target_temperature=28.0,
            menu_enable_fu=False,
            min_temperature=5.0,
            max_temperature=35.0,
            temperature_step=0.1,
            can_set_temperature=True,
            can_set_enabled=True,
        ),
        snapshot=SimpleNamespace(values={"temperature": 26.5}),
        native_available=True,
        native_held=False,
        native_pending_target=None,
        native_pending_target_queued=False,
        native_pending_error=None,
        native_source_time="2026-11-05T12:00:00+00:00",
        async_set_native_temperature=AsyncMock(),
        async_set_native_enabled=AsyncMock(),
        async_add_listener=Mock(return_value=Mock()),
        async_request_refresh=AsyncMock(),
    )
    for key, value in changes.items():
        setattr(result, key, value)
    return result


def test_native_entities_preserve_external_unique_ids_and_logical_device():
    subject = coordinator()
    climate = TelePoolNativeClimate(subject)
    switch = TelePoolNativeFuSwitch(subject)
    assert climate.unique_id == TelePoolClimate(subject.station, "climate.external").unique_id == "q3-test_climate"
    assert switch.unique_id == TelePoolPowerSwitch(subject.station, "switch.external").unique_id == "q3-test_power"
    assert climate.device_info["identifiers"] == switch.device_info["identifiers"] == {("telepool", "q3-test")}
    assert switch.translation_key == "native_fu_menu"
    assert switch.extra_state_attributes["configuration_field"] == "MenuEnableFu"


def test_native_temperature_reports_only_known_sensor_and_target_without_heating_action():
    subject = coordinator()
    climate = TelePoolNativeClimate(subject)
    assert climate.available
    assert climate.native_current_temperature == 26.5
    assert climate.native_target_temperature == 28
    assert (climate.min_temp, climate.max_temp, climate.target_temperature_step) == (5, 35, 0.1)
    assert climate.native_temperature_unit == "°C"
    assert climate.supported_features == ClimateEntityFeature.TARGET_TEMPERATURE
    assert climate.hvac_mode is HVACMode.HEAT
    assert climate.hvac_modes == [HVACMode.HEAT]
    assert climate.hvac_action is None
    subject.snapshot.values["temperature"] = None
    assert climate.available and climate.native_current_temperature is None
    subject.snapshot = None
    assert climate.available and climate.native_current_temperature is None


async def test_native_temperature_delegates_only_single_setpoint_and_never_confirms_optimistically():
    subject = coordinator()
    climate = TelePoolNativeClimate(subject)
    await climate.async_set_temperature(temperature=30)
    subject.async_set_native_temperature.assert_awaited_once_with(30.0)
    assert climate.native_target_temperature == 28
    for kwargs in (
        {"temperature": 36},
        {"temperature": 4.9},
        {"temperature": True},
        {"temperature": float("nan")},
        {"temperature": "30"},
        {"temperature": 27.15},
        {"temperature": 30, "hvac_mode": "heat"},
        {"target_temp_low": 25, "target_temp_high": 30},
        {},
    ):
        with pytest.raises(ServiceValidationError):
            await climate.async_set_temperature(**kwargs)
    for command in (climate.async_turn_on, climate.async_turn_off):
        with pytest.raises(ServiceValidationError):
            await command()
    with pytest.raises(ServiceValidationError):
        await climate.async_set_hvac_mode(HVACMode.HEAT)
    assert subject.async_set_native_temperature.await_count == 1
    subject.async_set_native_enabled.assert_not_called()


@pytest.mark.parametrize("limit", [35, 38, 40])
async def test_native_temperature_caps_a_higher_form_range_at_configured_limit(limit):
    subject = coordinator()
    subject.entry.options["max_target_temperature"] = limit
    subject.native_controls.max_temperature = 60
    climate = TelePoolNativeClimate(subject)
    assert climate.max_temp == limit
    await climate.async_set_temperature(temperature=limit)
    with pytest.raises(ServiceValidationError):
        await climate.async_set_temperature(temperature=limit + 0.1)
    subject.async_set_native_temperature.assert_awaited_once_with(limit)
    subject.native_controls.min_temperature = 50
    assert not climate.available
    assert (climate.min_temp, climate.max_temp) == (limit, limit)
    with pytest.raises(HomeAssistantError):
        await climate.async_set_temperature(temperature=limit)
    assert subject.async_set_native_temperature.await_count == 1


async def test_native_raised_pool_limit_delegates_above_cached_form_without_optimistic_confirmation():
    subject = coordinator(entry=SimpleNamespace(options={"max_target_temperature": 38}))
    climate = TelePoolNativeClimate(subject)
    assert climate.max_temp == 38
    await climate.async_set_temperature(temperature=38)
    subject.async_set_native_temperature.assert_awaited_once_with(38)
    assert climate.native_target_temperature == 28
    assert subject.native_pending_target is None


async def test_native_held_raised_target_can_be_queued_above_cached_cloud_maximum():
    subject = coordinator(
        entry=SimpleNamespace(options={"max_target_temperature": 38}), native_available=False, native_held=True
    )
    climate = TelePoolNativeClimate(subject)
    await climate.async_set_temperature(temperature=38)
    subject.async_set_native_temperature.assert_awaited_once_with(38)
    assert climate.native_target_temperature == 28


async def test_native_held_temperature_exposes_pending_desired_and_delegates_queue_policy():
    subject = coordinator(
        native_available=False, native_held=True, native_pending_target=29, native_pending_target_queued=True
    )
    climate = TelePoolNativeClimate(subject)
    switch = TelePoolNativeFuSwitch(subject)
    assert climate.available
    assert climate.native_current_temperature == 26.5
    assert climate.native_target_temperature == 29
    attributes = climate.extra_state_attributes
    assert attributes["held"] and not attributes["data_fresh"]
    assert attributes["control_backend"] == "native_q3"
    assert attributes["pending_target"] == {"unit": "°C", "targets": {"temperature": 29}}
    assert attributes["reported_target"] == {"unit": "°C", "targets": {"temperature": 28}}
    assert attributes["source_time"] == "2026-11-05T12:00:00+00:00"
    await climate.async_set_temperature(temperature=30)
    subject.async_set_native_temperature.assert_awaited_once_with(30)
    assert not switch.available and switch.is_on is None
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()
    subject.async_set_native_enabled.assert_not_called()


@pytest.mark.parametrize("queued", [False, True])
async def test_held_display_only_uses_queued_intention_and_keeps_rejected_target_in_diagnostics(queued):
    subject = coordinator(
        entry=SimpleNamespace(options={"max_target_temperature": 38}),
        native_available=False,
        native_held=True,
        native_pending_target=38,
        native_pending_target_queued=queued,
        native_pending_error=None if queued else "write_failed",
    )
    subject.native_controls.target_temperature = 35
    climate = TelePoolNativeClimate(subject)
    assert climate.available
    assert climate.native_target_temperature == (38 if queued else 35)
    attributes = climate.extra_state_attributes
    assert attributes["pending_target"] == {"unit": "°C", "targets": {"temperature": 38}}
    assert attributes["reported_target"] == {"unit": "°C", "targets": {"temperature": 35}}
    assert attributes["pending_error"] == (None if queued else "write_failed")


async def test_native_stale_offline_without_hold_rejects_temperature_and_switch_commands():
    subject = coordinator(native_available=False, native_held=False)
    climate = TelePoolNativeClimate(subject)
    switch = TelePoolNativeFuSwitch(subject)
    assert not climate.available and not switch.available
    for command in (switch.async_turn_on, switch.async_turn_off):
        with pytest.raises(HomeAssistantError):
            await command()
    with pytest.raises(HomeAssistantError):
        await climate.async_set_temperature(temperature=30)
    subject.async_set_native_temperature.assert_not_called()
    subject.async_set_native_enabled.assert_not_called()


async def test_native_readonly_or_incomplete_form_is_never_a_writable_target():
    subject = coordinator()
    climate = TelePoolNativeClimate(subject)
    switch = TelePoolNativeFuSwitch(subject)
    subject.native_controls.can_set_temperature = False
    subject.native_controls.can_set_enabled = False
    assert climate.available and climate.native_target_temperature == 28
    assert climate.supported_features == 0
    assert not switch.available
    with pytest.raises(ServiceValidationError):
        await climate.async_set_temperature(temperature=30)
    subject.native_controls.can_set_temperature = True
    for field in ("min_temperature", "max_temperature", "temperature_step"):
        previous = getattr(subject.native_controls, field)
        setattr(subject.native_controls, field, None)
        assert climate.supported_features == 0
        with pytest.raises(ServiceValidationError):
            await climate.async_set_temperature(temperature=30)
        setattr(subject.native_controls, field, previous)
    subject.async_set_native_temperature.assert_not_called()


async def test_native_switch_mirrors_only_menu_enable_flag_and_requires_fresh_editable_form():
    subject = coordinator()
    switch = TelePoolNativeFuSwitch(subject)
    assert switch.available and switch.is_on is False
    await switch.async_turn_on()
    subject.async_set_native_enabled.assert_awaited_once_with(True)
    assert switch.is_on is False
    subject.native_controls.menu_enable_fu = True
    assert switch.is_on is True
    await switch.async_turn_off()
    assert subject.async_set_native_enabled.await_args.args == (False,)
    subject.native_controls.menu_enable_fu = None
    assert not switch.available and switch.is_on is None


@pytest.mark.parametrize(
    ("options", "climate_type", "switch_type"),
    [
        ({}, None, None),
        ({"thermostat_mode": "disabled", "power_mode": "disabled"}, None, None),
        ({"thermostat_mode": "native_q3", "power_mode": "native_q3"}, TelePoolNativeClimate, TelePoolNativeFuSwitch),
        (
            {
                "thermostat_mode": "native_q3",
                "power_mode": "native_q3",
                "climate_source": "climate.external",
                "power_source": "switch.external",
            },
            TelePoolNativeClimate,
            TelePoolNativeFuSwitch,
        ),
        (
            {
                "thermostat_mode": "disabled",
                "power_mode": "disabled",
                "climate_source": "climate.external",
                "power_source": "switch.external",
            },
            None,
            None,
        ),
        (
            {"climate_source": "climate.external", "power_source": "switch.external"},
            TelePoolClimate,
            TelePoolPowerSwitch,
        ),
        (
            {
                "thermostat_mode": "external",
                "power_mode": "external",
                "climate_source": "climate.external",
                "power_source": "switch.external",
            },
            TelePoolClimate,
            TelePoolPowerSwitch,
        ),
    ],
)
async def test_platform_routing_creates_only_selected_backend(hass, options, climate_type, switch_type):
    subject = coordinator()
    entry = SimpleNamespace(options=options, runtime_data=SimpleNamespace(station=subject.station, coordinator=subject))
    add_climate, add_switch = Mock(), Mock()
    await setup_climate(hass, entry, add_climate)
    await setup_switch(hass, entry, add_switch)
    for callback, expected in ((add_climate, climate_type), (add_switch, switch_type)):
        if expected is None:
            callback.assert_not_called()
        else:
            callback.assert_called_once()
            entities = callback.call_args.args[0]
            assert len(entities) == 1 and isinstance(entities[0], expected)


@pytest.mark.parametrize(("fahrenheit", "celsius"), [(100.4, 38), (95.0, 35), (83.3, 28.5), (82.4, 28)])
async def test_native_real_ha_climate_service_converts_fahrenheit_to_celsius_once(hass, fahrenheit, celsius):
    hass.config.units = US_CUSTOMARY_SYSTEM
    assert await async_setup_component(hass, "climate", {})
    subject = coordinator()
    subject.entry.options["max_target_temperature"] = 38
    subject.native_controls.max_temperature = 60
    subject.native_controls.temperature_step = 0.5
    entity = TelePoolNativeClimate(subject)
    entity.entity_id = "climate.q3"
    entity._attr_device_info = None
    entity._attr_name = "Q3 thermostat"
    await hass.data[DATA_COMPONENT].async_add_entities([entity])

    state = hass.states.get(entity.entity_id)
    assert state.attributes["max_temp"] == pytest.approx(100.4)
    assert state.attributes["current_temperature"] == pytest.approx(79.7)
    assert state.attributes["target_temp_step"] == pytest.approx(0.9)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": entity.entity_id, "temperature": fahrenheit}, blocking=True
    )
    subject.async_set_native_temperature.assert_awaited_once_with(pytest.approx(celsius))
    assert entity.native_target_temperature == 28
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": entity.entity_id, "temperature": 100.5}, blocking=True
        )
    assert subject.async_set_native_temperature.await_count == 1
