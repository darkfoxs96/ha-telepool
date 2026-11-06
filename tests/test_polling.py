"""Availability notifications must not postpone actual cloud polling."""

from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.telepool.client import Snapshot, Station
from custom_components.telepool.credential_store import async_encrypt_credentials


@pytest.mark.freeze_time("2026-11-05T12:00:00+00:00")
async def test_30_second_availability_ticks_do_not_delay_300_second_cloud_polls(hass, freezer):
    station = Station("polling-station", "pool", "Pool", "Q3", 7)
    start = dt_util.utcnow()
    unchanged = Snapshot(
        station.id,
        {"ph": 6.48, "rx": 751, "temperature": None},
        {},
        start - timedelta(minutes=1),
        start,
        False,
        False,
        None,
        None,
    )
    client = SimpleNamespace(async_fetch=AsyncMock(return_value=unchanged))
    entry = MockConfigEntry(
        domain="telepool",
        unique_id=station.id,
        data={
            "credentials": await async_encrypt_credentials(hass, "polling@example.invalid", "test-password"),
            "station": asdict(station),
        },
        options={"poll_interval": 300, "thermostat_mode": "disabled", "power_mode": "disabled"},
    )
    entry.add_to_hass(hass)
    session = SimpleNamespace(detach=Mock(return_value=None))
    with (
        patch("custom_components.telepool.TelePoolClient", return_value=client),
        patch("custom_components.telepool.async_create_clientsession", return_value=session),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        assert client.async_fetch.await_count == 1
        assert coordinator.data is unchanged
        assert coordinator._listeners
        try:
            with patch.object(
                coordinator, "async_update_listeners", wraps=coordinator.async_update_listeners
            ) as notify:
                for seconds in range(30, 601, 30):
                    # Move both UTC and monotonic time. Merely firing future
                    # timer callbacks without advancing the clock would miss
                    # a bug that reset the next poll on every 30-second tick.
                    freezer.move_to(start + timedelta(seconds=seconds))
                    async_fire_time_changed(hass)
                    await hass.async_block_till_done()
                    assert client.async_fetch.await_count == 1 + seconds // 300
                assert notify.call_count >= 20
            assert coordinator.snapshot is coordinator.data is unchanged
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
    session.detach.assert_called_once()
