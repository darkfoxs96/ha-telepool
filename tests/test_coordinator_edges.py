"""Regressions for untrusted relay state and malformed persistent telemetry."""

from dataclasses import asdict
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry

from custom_components.telepool.client import ConnectionError, Snapshot, Station
from custom_components.telepool.coordinator import TelePoolCoordinator

STATION = Station("station-test", "pool-test", "Pool", "TelePool Test", 7)


def sample():
    return Snapshot(
        STATION.id,
        {"ph": 7.2, "rx": 650},
        {"ph": 3, "rx": 3},
        dt_util.utcnow() - timedelta(days=1),
        dt_util.utcnow(),
        True,
        False,
        -70,
        60,
    )


def coordinator(hass, options=None):
    entry = MockConfigEntry(domain="telepool", options=options or {})
    entry.add_to_hass(hass)
    client = MagicMock(async_fetch=AsyncMock(side_effect=ConnectionError("offline")))
    return TelePoolCoordinator(hass, entry, client, STATION)


@pytest.mark.parametrize("kind", ["restored", "disabled", "own_proxy"])
async def test_untrusted_off_does_not_hold_cached_readings_or_skip_polling(hass, kind):
    registry = er.async_get(hass)
    registered = registry.async_get_or_create(
        "switch",
        "telepool" if kind == "own_proxy" else "test",
        "external",
        suggested_object_id="external",
        disabled_by=er.RegistryEntryDisabler.USER if kind == "disabled" else None,
    )
    hass.states.async_set(registered.entity_id, "off", {"restored": True} if kind == "restored" else {})
    subject = coordinator(hass, {"power_source": registered.entity_id, "power_registry_id": registered.id})
    subject.snapshot = sample()

    subject._read_power()
    await subject._async_update_data()

    assert subject.policy.power is None
    assert subject.availability.status == "no_connection"
    assert not subject.availability.display_available
    subject.client.async_fetch.assert_awaited_once_with(STATION)


async def test_unreadable_optional_cache_does_not_block_initialization(hass):
    subject = coordinator(hass)
    subject._store = MagicMock(async_load=AsyncMock(side_effect=HomeAssistantError("storage unavailable")))

    try:
        await subject.async_initialize()
    finally:
        await subject.entry._async_process_on_unload(hass)

    assert subject.snapshot is None
    assert subject.availability.status == "no_connection"
    assert subject.cloud_error == "connection_or_protocol"
    subject.client.async_fetch.assert_awaited_once_with(STATION)


async def test_failed_poll_records_actual_attempt_without_freshening_old_measurement(hass):
    subject = coordinator(hass)
    historic = sample()
    subject.snapshot = historic
    before = dt_util.utcnow()

    await subject._async_update_data()

    assert before <= subject.last_attempt <= dt_util.utcnow()
    assert subject.last_success is None
    assert subject.snapshot is historic
    assert subject.snapshot.observed_at == historic.observed_at
    assert subject.availability.status == "no_connection"


async def test_successful_stale_response_updates_poll_time_but_keeps_station_stale(hass):
    subject = coordinator(hass)
    historic = sample()
    subject.client.async_fetch.side_effect = None
    subject.client.async_fetch.return_value = historic
    subject._store.async_save = AsyncMock()
    before = dt_util.utcnow()

    await subject._async_update_data()

    assert before <= subject.last_attempt <= subject.last_success <= dt_util.utcnow()
    assert subject.snapshot.received_at == historic.received_at
    assert not subject.availability.fresh
    assert subject.cloud_ok


async def test_expected_power_pause_does_not_claim_an_api_attempt(hass):
    hass.states.async_set("switch.external", "off")
    subject = coordinator(hass, {"power_source": "switch.external"})
    subject.snapshot = sample()
    previous_attempt = dt_util.utcnow() - timedelta(minutes=10)
    subject.last_attempt = previous_attempt
    subject._read_power()

    await subject._async_update_data()

    assert subject.last_attempt == previous_attempt
    assert subject.availability.status == "paused"
    subject.client.async_fetch.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["naive_timestamp", "invalid_values", "other_station"])
async def test_invalid_cache_is_discarded_before_offline_initialization(hass, invalid):
    cached = asdict(sample())
    for key in ("observed_at", "received_at"):
        cached[key] = cached[key].isoformat()
    if invalid == "naive_timestamp":
        cached["observed_at"] = "2026-11-04T12:00:00"
    elif invalid == "invalid_values":
        cached["values"] = None
    else:
        cached["station_id"] = "a-different-station"
    subject = coordinator(hass)
    subject._store = MagicMock(async_load=AsyncMock(return_value=cached), async_save=AsyncMock())

    try:
        await subject.async_initialize()
    finally:
        # This test calls the coordinator without a config-entry setup/unload cycle.
        await subject.entry._async_process_on_unload(hass)

    assert subject.snapshot is None
    assert subject.availability.status == "no_connection"
    assert not subject.availability.display_available
    assert subject.cloud_error == "connection_or_protocol"
    subject.client.async_fetch.assert_awaited_once_with(STATION)
