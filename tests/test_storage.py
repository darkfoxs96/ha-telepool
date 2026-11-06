"""Protect the awaited-persistence contract across Home Assistant updates."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.telepool.storage import StrictStore


@pytest.mark.asyncio
async def test_write_error_suppressed_by_base_store_reaches_caller(hass):
    store = StrictStore(hass, 1, "telepool.test", private=True, atomic_writes=True)
    with patch.object(Store, "_async_write_data", new=AsyncMock(side_effect=WriteError("synthetic write failure"))):
        with pytest.raises(WriteError):
            await store.async_save({"pending": "synthetic"})


@pytest.mark.asyncio
async def test_save_waits_for_write_completion(hass):
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def write(_data):
        entered.set()
        await finish.wait()

    store = StrictStore(hass, 1, "telepool.test", private=True, atomic_writes=True)
    with patch.object(Store, "_async_write_data", new=AsyncMock(side_effect=write)):
        task = asyncio.create_task(store.async_save({"value": 1}))
        await entered.wait()
        assert not task.done()
        finish.set()
        await task


@pytest.mark.asyncio
async def test_stopping_does_not_acknowledge_a_deferred_write(hass):
    store = StrictStore(hass, 1, "telepool.test", private=True, atomic_writes=True)
    previous = hass.state
    hass.set_state(CoreState.stopping)
    try:
        with pytest.raises(HomeAssistantError, match="stopping"):
            await store.async_save({"value": 1})
    finally:
        hass.set_state(previous)


@pytest.mark.asyncio
async def test_read_only_store_does_not_acknowledge_a_discarded_write(hass):
    store = StrictStore(hass, 1, "telepool.test", read_only=True)
    with pytest.raises(HomeAssistantError, match="read-only"):
        await store.async_save({"value": 1})
