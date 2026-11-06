"""Exercise source identity, independent availability, and delegated commands."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.components.climate.const import DATA_COMPONENT
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.telepool.climate import TelePoolClimate
from custom_components.telepool.climate import async_setup_entry as setup_climate
from custom_components.telepool.switch import TelePoolPowerSwitch
from custom_components.telepool.switch import async_setup_entry as setup_switch

STATION = SimpleNamespace(id="station-123", name="Pool")


async def attach(hass, entity, entity_id):
    """Attach state/registry listeners; never connect a real device."""
    entity.hass = hass
    entity.entity_id = entity_id
    entity.async_write_ha_state = Mock()
    await entity.async_added_to_hass()
    return entity


async def test_sources_are_optional_and_unique_ids_do_not_depend_on_source(hass):
    add_entities = Mock()
    entry = SimpleNamespace(options={}, runtime_data=SimpleNamespace(station=STATION))
    await setup_climate(hass, entry, add_entities)
    await setup_switch(hass, entry, add_entities)
    add_entities.assert_not_called()

    entry.options = {"power_source": "switch.external", "climate_source": "climate.external"}
    await setup_climate(hass, entry, add_entities)
    await setup_switch(hass, entry, add_entities)
    assert add_entities.call_count == 2
    assert TelePoolPowerSwitch(STATION, "switch.first").unique_id == "station-123_power"
    assert TelePoolPowerSwitch(STATION, "switch.second").unique_id == "station-123_power"
    thermostat = TelePoolClimate(STATION, "climate.external")
    assert thermostat.unique_id == "station-123_climate"
    assert thermostat.device_info["identifiers"] == {("telepool", STATION.id)}


async def test_power_proxy_controls_source_independently_of_telepool(hass):
    hass.states.async_set("switch.external", "off")
    power = await attach(hass, TelePoolPowerSwitch(STATION, "switch.external"), "switch.pool_power")
    turn_on, turn_off = AsyncMock(), AsyncMock()
    hass.services.async_register("switch", "turn_on", turn_on)
    hass.services.async_register("switch", "turn_off", turn_off)
    assert power.available and power.is_on is False

    await power.async_turn_on()
    assert turn_on.call_args.args[0].data == {"entity_id": "switch.external"}
    # Sending a command does not optimistically invent a changed source state.
    assert power.is_on is False
    hass.states.async_set("switch.external", "on")
    await hass.async_block_till_done()
    assert power.is_on is True
    power.async_write_ha_state.assert_called()
    assert turn_on.call_count == 1
    assert turn_off.call_count == 0

    await power.async_turn_off()
    assert turn_off.call_args.args[0].data == {"entity_id": "switch.external"}


@pytest.mark.parametrize("state", [None, "unknown", "unavailable"])
async def test_unavailable_source_rejects_commands(hass, state):
    if state is not None:
        hass.states.async_set("switch.external", state)
    power = await attach(hass, TelePoolPowerSwitch(STATION, "switch.external"), "switch.pool_power")
    turn_on = AsyncMock()
    hass.services.async_register("switch", "turn_on", turn_on)
    assert not power.available
    assert power.is_on is None
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await power.async_turn_on()
    turn_on.assert_not_called()


async def test_registry_uuid_follows_rename_and_refuses_replacement(hass):
    registry = er.async_get(hass)
    source = registry.async_get_or_create("switch", "test", "original", suggested_object_id="external")
    hass.states.async_set(source.entity_id, "off")
    power = await attach(hass, TelePoolPowerSwitch(STATION, source.entity_id, source.id), "switch.pool_power")
    turn_on = AsyncMock()
    hass.services.async_register("switch", "turn_on", turn_on)

    registry.async_update_entity(source.entity_id, new_entity_id="switch.renamed")
    hass.states.async_set("switch.renamed", "on")
    await hass.async_block_till_done()
    assert power.source_entity_id == "switch.renamed"
    assert power.is_on is True
    power.async_write_ha_state.reset_mock()
    hass.states.async_set("switch.external", "on")
    await hass.async_block_till_done()
    power.async_write_ha_state.assert_not_called()
    hass.states.async_set("switch.renamed", "off")
    await hass.async_block_till_done()
    power.async_write_ha_state.assert_called_once()
    turn_on.assert_not_called()

    await power.async_turn_on()
    assert turn_on.call_args.args[0].data == {"entity_id": "switch.renamed"}
    registry.async_remove("switch.renamed")
    await hass.async_block_till_done()
    assert not power.available
    replacement = registry.async_get_or_create("switch", "test", "replacement", suggested_object_id="external")
    assert replacement.id != source.id
    hass.states.async_set(replacement.entity_id, "on")
    await hass.async_block_till_done()
    assert not power.available


async def test_registry_uuid_resolves_rename_from_previous_session(hass):
    registry = er.async_get(hass)
    source = registry.async_get_or_create("switch", "test", "source", suggested_object_id="new_name")
    hass.states.async_set(source.entity_id, "on")
    power = await attach(hass, TelePoolPowerSwitch(STATION, "switch.old_name", source.id), "switch.pool_power")
    assert power.source_entity_id == source.entity_id
    assert power.available and power.is_on


@pytest.mark.parametrize("source_id,platform", [("switch.own_proxy", "telepool"), ("climate.wrong_domain", "test")])
async def test_own_or_wrong_domain_sources_cannot_create_cycles(hass, source_id, platform):
    registry = er.async_get(hass)
    domain, _, object_id = source_id.partition(".")
    source = registry.async_get_or_create(domain, platform, "source", suggested_object_id=object_id)
    hass.states.async_set(source.entity_id, "on")
    power = await attach(hass, TelePoolPowerSwitch(STATION, source.entity_id, source.id), "switch.pool_power")
    assert power.source_entity_id is None
    assert not power.available


async def test_disabled_or_restored_source_is_not_available(hass):
    registry = er.async_get(hass)
    source = registry.async_get_or_create("switch", "test", "source", suggested_object_id="external")
    hass.states.async_set(source.entity_id, "on", {"restored": True})
    power = await attach(hass, TelePoolPowerSwitch(STATION, source.entity_id, source.id), "switch.pool_power")
    assert not power.available
    hass.states.async_set(source.entity_id, "on")
    assert power.available
    registry.async_update_entity(source.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert not power.available


async def test_climate_mirrors_measurements_targets_and_only_implemented_features(hass):
    source_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_OFF
    )
    hass.states.async_set(
        "climate.external",
        "heat_cool",
        {
            "temperature_unit": UnitOfTemperature.CELSIUS,
            "current_temperature": 27.4,
            "target_temp_low": 27,
            "target_temp_high": 29,
            "min_temp": 5,
            "max_temp": 35,
            "target_temp_step": 0.5,
            "hvac_modes": ["off", "heat_cool", "invalid"],
            "hvac_action": "idle",
            "supported_features": source_features,
        },
    )
    climate = await attach(hass, TelePoolClimate(STATION, "climate.external"), "climate.pool")
    assert climate.available
    assert climate.native_current_temperature == 27.4
    assert climate.native_target_temperature is None
    assert climate.native_target_temperature_low == 27
    assert climate.native_target_temperature_high == 29
    assert (climate.min_temp, climate.max_temp, climate.target_temperature_step) == (5, 35, 0.5)
    assert climate.hvac_mode is HVACMode.HEAT_COOL
    assert climate.hvac_modes == [HVACMode.OFF, HVACMode.HEAT_COOL]
    assert climate.hvac_action is HVACAction.IDLE
    assert climate.supported_features == (ClimateEntityFeature.TARGET_TEMPERATURE_RANGE | ClimateEntityFeature.TURN_OFF)

    hass.states.async_set("climate.external", "heat", {"hvac_modes": None, "current_temperature": float("nan")})
    await hass.async_block_till_done()
    assert climate.hvac_action is None
    assert climate.native_current_temperature is None
    assert climate.hvac_modes == []


async def test_climate_targets_convert_to_service_units_and_preserve_range(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set(
        "climate.external",
        "heat_cool",
        {
            "temperature_unit": UnitOfTemperature.CELSIUS,
            "hvac_modes": ["off", "heat_cool"],
            "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE),
        },
    )
    climate = await attach(hass, TelePoolClimate(STATION, "climate.external"), "climate.pool")
    set_temperature, set_mode = AsyncMock(), AsyncMock()
    hass.services.async_register("climate", "set_temperature", set_temperature)
    hass.services.async_register("climate", "set_hvac_mode", set_mode)
    # A native-method call receives native units, while a HA service expects
    # the installation's displayed units. 25/30 C must become 77/86 F.
    await climate.async_set_temperature(target_temp_low=25, target_temp_high=30)
    assert set_temperature.call_args.args[0].data == {
        "entity_id": "climate.external",
        "target_temp_low": 77,
        "target_temp_high": 86,
    }
    await climate.async_set_hvac_mode(HVACMode.OFF)
    assert set_mode.call_args.args[0].data == {"entity_id": "climate.external", "hvac_mode": HVACMode.OFF}
    with pytest.raises(ServiceValidationError):
        await climate.async_set_temperature(temperature=28)
    with pytest.raises(ServiceValidationError):
        await climate.async_set_hvac_mode(HVACMode.COOL)
    with pytest.raises(ServiceValidationError):
        await climate.async_turn_on()


async def test_climate_delegates_explicit_on_off_without_a_regulator(hass):
    features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TARGET_TEMPERATURE
    hass.states.async_set(
        "climate.external", "off", {"hvac_modes": ["off", "heat"], "supported_features": int(features)}
    )
    climate = await attach(hass, TelePoolClimate(STATION, "climate.external"), "climate.pool")
    turn_on, turn_off, set_target = AsyncMock(), AsyncMock(), AsyncMock()
    hass.services.async_register("climate", "turn_on", turn_on)
    hass.services.async_register("climate", "turn_off", turn_off)
    hass.services.async_register("climate", "set_temperature", set_target)
    await climate.async_turn_on()
    await climate.async_turn_off()
    await climate.async_set_temperature(temperature=28)
    assert turn_on.call_count == turn_off.call_count == set_target.call_count == 1
    hass.states.async_set("climate.external", "heat", {"current_temperature": 15})
    await hass.async_block_till_done()
    # A cold measurement never causes an automatic heating command.
    assert turn_on.call_count == turn_off.call_count == set_target.call_count == 1


@pytest.mark.parametrize(
    ("unit", "minimum", "source_max", "cap", "above_cap"),
    [
        (UnitOfTemperature.CELSIUS, 5, 60, 38, 38.1),
        (UnitOfTemperature.FAHRENHEIT, 41, 140, 100.4, 100.5),
        (UnitOfTemperature.KELVIN, 278.15, 333.15, 311.15, 311.25),
    ],
)
async def test_configured_pool_maximum_validates_native_targets_before_forwarding(
    hass, unit, minimum, source_max, cap, above_cap
):
    hass.states.async_set(
        "climate.external",
        "heat",
        {
            "temperature_unit": unit,
            "min_temp": minimum,
            "max_temp": source_max,
            "hvac_modes": ["off", "heat"],
            "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE),
        },
    )
    climate = await attach(
        hass, TelePoolClimate(STATION, "climate.external", max_target_temperature=38), "climate.pool"
    )
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)

    assert climate.max_temp == pytest.approx(cap)
    await climate.async_set_temperature(temperature=cap)
    assert service.call_args.args[0].data["temperature"] == pytest.approx(38)
    with pytest.raises(ServiceValidationError, match="between"):
        await climate.async_set_temperature(temperature=above_cap)
    with pytest.raises(ServiceValidationError, match="between"):
        await climate.async_set_temperature(temperature=minimum - 0.1)
    service.assert_awaited_once()


async def test_pool_cap_keeps_a_lower_source_limit_and_checks_both_range_bounds(hass):
    hass.states.async_set(
        "climate.external",
        "heat_cool",
        {
            "temperature_unit": "°C",
            "min_temp": 5,
            "max_temp": 35,
            "hvac_modes": ["off", "heat_cool"],
            "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE),
        },
    )
    climate = await attach(hass, TelePoolClimate(STATION, "climate.external"), "climate.pool")
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    assert climate.max_temp == 35
    await climate.async_set_temperature(target_temp_low=5, target_temp_high=35)
    for low, high in ((5, 35.1), (4.9, 35), (36, 37), (25, 24)):
        with pytest.raises(ServiceValidationError):
            await climate.async_set_temperature(target_temp_low=low, target_temp_high=high)
    service.assert_awaited_once()


@pytest.mark.parametrize(
    ("limit", "source_max", "effective_max"), [(35, 50, 35), (38, 50, 38), (40, 50, 40), (38, 32, 32)]
)
async def test_external_configured_ceiling_preserves_lower_device_limit_and_actual_readings(
    hass, limit, source_max, effective_max
):
    hass.states.async_set(
        "climate.external",
        "heat",
        {
            "temperature_unit": "°C",
            "min_temp": 5,
            "max_temp": source_max,
            "temperature": 39,
            "current_temperature": 41,
            "hvac_modes": ["heat"],
            "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE),
        },
    )
    climate = await attach(
        hass, TelePoolClimate(STATION, "climate.external", max_target_temperature=limit), "climate.pool"
    )
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    assert climate.max_temp == effective_max
    # The cap governs new commands, never masks readings or changes the device.
    assert climate.native_target_temperature == 39
    assert climate.native_current_temperature == 41
    service.assert_not_called()
    with pytest.raises(ServiceValidationError):
        await climate.async_set_temperature(temperature=effective_max + 0.5)
    service.assert_not_called()
    await climate.async_set_temperature(temperature=effective_max)
    service.assert_awaited_once()


async def test_source_minimum_above_pool_cap_is_unavailable_without_inverted_metadata(hass):
    hass.states.async_set(
        "climate.external",
        "heat",
        {
            "temperature_unit": "°C",
            "min_temp": 50,
            "max_temp": 60,
            "hvac_modes": ["off", "heat"],
            "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE),
        },
    )
    climate = await attach(hass, TelePoolClimate(STATION, "climate.external"), "climate.pool")
    service = AsyncMock()
    hass.services.async_register("climate", "set_temperature", service)
    assert not climate.available
    assert (climate.min_temp, climate.max_temp) == (35, 35)
    for target in (38, 50):
        with pytest.raises(ServiceValidationError, match="source thermostat range"):
            await climate.async_set_temperature(temperature=target)
    service.assert_not_called()


@pytest.mark.parametrize(
    ("global_fahrenheit", "source_unit", "current", "target", "minimum", "maximum", "command", "native_command"),
    [
        (False, UnitOfTemperature.FAHRENHEIT, 77, 80.6, 50, 95, 30, 86),
        (True, UnitOfTemperature.CELSIUS, 25, 27, 10, 35, 86, 30),
    ],
)
async def test_climate_real_service_and_published_attributes_convert_once(
    hass, global_fahrenheit, source_unit, current, target, minimum, maximum, command, native_command
):
    """Use HA's real climate service path with a fake physical thermostat."""
    if global_fahrenheit:
        hass.config.units = US_CUSTOMARY_SYSTEM
    assert await async_setup_component(hass, "climate", {})
    component = hass.data[DATA_COMPONENT]

    class PhysicalThermostat(ClimateEntity):
        _attr_name = "External thermostat"
        _attr_should_poll = False
        _attr_hvac_mode = HVACMode.HEAT
        _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
        _enable_turn_on_off_backwards_compatibility = False
        _attr_target_temperature_step = 0.5

        async def async_set_temperature(self, **kwargs):
            self.received = kwargs
            self._attr_native_target_temperature = kwargs["temperature"]
            self.async_write_ha_state()

    source = PhysicalThermostat()
    source.entity_id = "climate.external"
    source._attr_native_temperature_unit = source_unit
    source._attr_native_current_temperature = current
    source._attr_native_target_temperature = target
    source._attr_min_temp = minimum
    source._attr_max_temp = maximum
    await component.async_add_entities([source])
    proxy = TelePoolClimate(STATION, "climate.external")
    proxy.entity_id = "climate.pool"
    proxy._attr_name = "Pool thermostat"
    # The core component used by this unit test has no config entry. The real
    # TelePool platform does, and attaches the device_info tested above.
    proxy._attr_device_info = None
    await component.async_add_entities([proxy])

    source_state = hass.states.get(source.entity_id)
    proxy_state = hass.states.get(proxy.entity_id)
    assert source_state is not None and proxy_state is not None
    for attribute in (
        "current_temperature",
        "temperature",
        "temperature_unit",
        "min_temp",
        "max_temp",
        "target_temp_step",
    ):
        assert proxy_state.attributes[attribute] == source_state.attributes[attribute]

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": proxy.entity_id, "temperature": command}, blocking=True
    )
    await hass.async_block_till_done()
    assert source.received["temperature"] == pytest.approx(native_command)
    assert hass.states.get(proxy.entity_id).attributes["temperature"] == command
