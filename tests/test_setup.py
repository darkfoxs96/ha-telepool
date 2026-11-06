"""Exercise real HA entity setup/unload with a fake read-only cloud client."""

from dataclasses import asdict, replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry

from custom_components.telepool.client import ConnectionError, Snapshot, Station
from custom_components.telepool.credential_store import async_encrypt_credentials

STATION = Station("123456789012345", "pool-test", "Pool", "TelePool Test", 7, "3.3.0", "private-account-id")


def sample():
    return Snapshot(
        STATION.id,
        {"ph": 6.48, "rx": 751, "temperature": None, "set_ph": 7.3, "set_rx": 600},
        {"ph": 3, "rx": 3, "fu": 24, "temperature": 15},
        dt_util.utcnow() - timedelta(seconds=60),
        dt_util.utcnow(),
        False,
        True,
        -73,
        54,
    )


async def create_entry(hass, *, options=None):
    entry = MockConfigEntry(
        domain="telepool",
        unique_id=STATION.id,
        title="TelePool Test",
        data={
            "credentials": await async_encrypt_credentials(hass, "test@example.invalid", "test-password"),
            "station": asdict(STATION),
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def entity_id(hass, domain, key):
    return er.async_get(hass).async_get_entity_id(domain, "telepool", f"{STATION.id}_{key}")


async def test_full_setup_real_entities_and_unload_detaches_session(hass):
    entry = await create_entry(hass)
    client = MagicMock(async_fetch=AsyncMock(return_value=sample()))
    session = MagicMock()
    with (
        patch("custom_components.telepool.TelePoolClient", return_value=client),
        patch("custom_components.telepool.async_create_clientsession", return_value=session),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "sensor", "ph")).state == "6.48"
    assert hass.states.get(entity_id(hass, "sensor", "rx")).state == "751"
    assert hass.states.get(entity_id(hass, "sensor", "temperature")).state == "unavailable"
    assert hass.states.get(entity_id(hass, "sensor", "operating_status")).state == "working"
    assert hass.states.get(entity_id(hass, "binary_sensor", "data_fresh")).state == "on"
    assert entity_id(hass, "climate", "climate") is None
    assert entity_id(hass, "switch", "power") is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    session.detach.assert_called_once()


async def test_cloud_failure_does_not_block_external_controls(hass):
    hass.states.async_set("switch.external", "off")
    hass.states.async_set(
        "climate.external",
        "heat",
        {
            "temperature": 28,
            "current_temperature": 27,
            "temperature_unit": "°C",
            "hvac_modes": ["off", "heat"],
            "supported_features": 1,
        },
    )
    entry = await create_entry(hass, options={"power_source": "switch.external", "climate_source": "climate.external"})
    client = MagicMock(async_fetch=AsyncMock(side_effect=ConnectionError("offline")))
    with (
        patch("custom_components.telepool.TelePoolClient", return_value=client),
        patch("custom_components.telepool.async_create_clientsession", return_value=MagicMock(close=AsyncMock())),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "switch", "power")).state == "off"
    assert hass.states.get(entity_id(hass, "climate", "climate")).state == "heat"
    assert hass.states.get(entity_id(hass, "sensor", "ph")).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_power_off_keeps_original_cached_value_across_reload(hass):
    hass.states.async_set("switch.external", "off")
    entry = await create_entry(hass, options={"power_source": "switch.external", "export_aliases": True})
    historic = replace(sample(), observed_at=dt_util.utcnow() - timedelta(days=1), stale=True)
    client = MagicMock(async_fetch=AsyncMock(return_value=historic))
    with (
        patch("custom_components.telepool.TelePoolClient", return_value=client),
        patch(
            "custom_components.telepool.async_create_clientsession",
            side_effect=lambda *a, **k: MagicMock(close=AsyncMock()),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        ph_id = entity_id(hass, "sensor", "ph")
        previous_time = hass.states.get(ph_id).attributes["data_timestamp"]
        assert hass.states.get(ph_id).state == "6.48"
        assert hass.states.get(entity_id(hass, "sensor", "operating_status")).state == "paused"
        assert hass.states.get(entity_id(hass, "sensor", "rx_export")).state != "unavailable"
        assert await hass.config_entries.async_unload(entry.entry_id)
        client.async_fetch.side_effect = ConnectionError("offline")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(ph_id).state == "6.48"
        assert hass.states.get(ph_id).attributes["data_timestamp"] == previous_time
        assert not entry.runtime_data.coordinator.availability.fresh
        # OFF with cache does not keep polling the powered-down station.
        assert client.async_fetch.await_count == 1
        assert await hass.config_entries.async_unload(entry.entry_id)
