"""Validate source isolation, schema limits and persistence failures."""

import hashlib
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.telepool.climate_cache import ClimateCache
from custom_components.telepool.storage import StrictStore

IDENTITY = "external_climate:synthetic-registry-uuid"
VALID = {
    "source_identity": IDENTITY,
    "source_time": "2026-11-05T10:00:00+03:00",
    "state": "heat",
    "attributes": {
        "current_temperature": 25.5,
        "temperature": 28.0,
        "target_temp_low": None,
        "target_temp_high": None,
        "min_temp": 5,
        "max_temp": 35,
        "target_temp_step": 0.5,
        "temperature_unit": "°C",
        "hvac_modes": ["off", "heat"],
        "hvac_action": "heating",
        "supported_features": 1,
    },
    "pending": {"unit": "°C", "targets": {"temperature": 29.0}},
}


@pytest.mark.asyncio
async def test_valid_roundtrip_is_private_and_source_bound(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    with patch("custom_components.telepool.climate_cache.StrictStore", wraps=StrictStore) as factory:
        cache = ClimateCache(hass, "synthetic-station", IDENTITY)
    assert factory.call_args.kwargs == {"private": True, "atomic_writes": True}
    await cache.async_save(VALID)
    reloaded = ClimateCache(hass, "synthetic-station", IDENTITY)
    assert await reloaded.async_load() == VALID
    assert cache._store.key == f"telepool.synthetic-station.{hashlib.sha256(IDENTITY.encode()).hexdigest()}.climate"
    assert IDENTITY not in cache._store.key
    assert await ClimateCache(hass, "synthetic-station", "external_climate:another-source").async_load() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"source_identity": "external_climate:wrong-source"},
        {"source_time": "2026-11-05T10:00:00"},
        {"source_time": "invalid"},
        {"state": "unavailable"},
        {"credentials": "synthetic-forbidden-field"},
        {"attributes": {"temperature_unit": "mV"}},
        {"attributes": {"current_temperature": float("nan")}},
        {"attributes": {"temperature": True}},
        {"attributes": {"preset_mode": "eco"}},
        {"pending": {"unit": "mV", "targets": {"temperature": 25}}},
        {"pending": {"unit": "°C", "targets": {"temperature": float("inf")}}},
        {"pending": {"unit": "°C", "targets": {"hvac_mode": "off"}}},
        {"pending": {"unit": "°C", "targets": {"temperature": 25, "target_temp_low": 20}}},
        {"pending": {"unit": "°C", "targets": {"target_temp_low": 20}}},
        {"pending": {"unit": "°C", "targets": {"target_temp_high": 25}}},
        {"pending": {"unit": "°C", "targets": {"target_temp_low": 30, "target_temp_high": 25}}},
    ],
)
async def test_invalid_cache_is_not_saved_or_loaded(hass, mutation):
    cache = ClimateCache(hass, "synthetic-station", IDENTITY)
    invalid = {**deepcopy(VALID), **mutation}
    with patch.object(cache._store, "async_save", new=AsyncMock()) as save:
        with pytest.raises(ValueError):
            await cache.async_save(invalid)
        save.assert_not_awaited()
    with patch.object(cache._store, "async_load", new=AsyncMock(return_value=invalid)):
        assert await cache.async_load() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [HomeAssistantError("synthetic"), OSError("synthetic"), KeyError("data"), NotImplementedError()]
)
async def test_unreadable_cache_is_ignored(hass, error):
    cache = ClimateCache(hass, "synthetic-station", IDENTITY)
    with patch.object(cache._store, "async_load", new=AsyncMock(side_effect=error)):
        assert await cache.async_load() is None


@pytest.mark.asyncio
async def test_failed_write_is_not_acknowledged_as_durable(hass):
    cache = ClimateCache(hass, "synthetic-station", IDENTITY)
    with patch.object(Store, "_async_write_data", new=AsyncMock(side_effect=WriteError("synthetic write failure"))):
        with pytest.raises(WriteError):
            await cache.async_save(VALID)


@pytest.mark.asyncio
async def test_complete_ordered_range_is_saved(hass):
    cache = ClimateCache(hass, "synthetic-station", IDENTITY)
    data = {**deepcopy(VALID), "pending": {"unit": "°C", "targets": {"target_temp_low": 20, "target_temp_high": 25}}}
    with patch.object(cache._store, "async_save", new=AsyncMock()) as save:
        await cache.async_save(data)
        save.assert_awaited_once_with(data)
