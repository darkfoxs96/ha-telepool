"""Native Q3 delivery never leaks into external sources or replays ambiguous POSTs."""

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry

from custom_components.telepool.availability import Availability
from custom_components.telepool.client import (
    CommandNotSent,
    ProtocolError,
    Q3Controls,
    SettingsRejected,
    Snapshot,
    Station,
    WriteOutcomeUnknown,
)
from custom_components.telepool.coordinator import TelePoolCoordinator
from custom_components.telepool.native_control import NativeControlManager

STATION = Station("test-station", "test-pool", "Pool", "Q3", 7)
CONTROLS = Q3Controls(STATION.id, 28.0, True, 5.0, 35.0, 0.5, True, True)


def assert_guarded_call(method, value):
    """The manager must supply a last-moment transport dispatch predicate."""
    method.assert_awaited_once()
    assert method.await_args.args == (STATION, value)
    assert set(method.await_args.kwargs) == {"dispatch_guard"}
    assert callable(method.await_args.kwargs["dispatch_guard"])


def manager(hass, *, paused=False, options=None):
    now = dt_util.utcnow()
    owner = SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(options=options or {"thermostat_mode": "native_q3", "power_mode": "external"}),
        station=STATION,
        snapshot=Snapshot(STATION.id, {"temperature": 27.0}, {}, now, now, False, False, None, None),
        availability=Availability("paused" if paused else "working", not paused, True),
        policy=SimpleNamespace(power="off" if paused else "on"),
        client=SimpleNamespace(
            async_get_controls=AsyncMock(return_value=CONTROLS),
            async_set_temperature=AsyncMock(return_value=replace(CONTROLS, target_temperature=29.0)),
            async_set_enabled=AsyncMock(return_value=replace(CONTROLS, menu_enable_fu=False)),
        ),
        async_update_listeners=MagicMock(),
    )
    subject = NativeControlManager(owner)
    subject.controls = CONTROLS
    subject._read_ok = not paused
    subject._store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    return subject, owner


async def test_paused_temperature_is_durable_latest_only_without_device_write(hass):
    subject, owner = manager(hass, paused=True)
    await subject.set_temperature(29.0)
    await subject.set_temperature(30.0)
    assert subject.pending_target == 30.0
    assert subject._store.async_save.call_args.args[0]["pending"] == 30.0
    assert subject._store.async_save.call_args.args[0]["delivery"] == "queued"
    owner.client.async_set_temperature.assert_not_awaited()


async def test_failed_durable_queue_does_not_acknowledge_or_dispatch(hass):
    subject, owner = manager(hass, paused=True)
    subject._store.async_save.side_effect = OSError("read only")
    with pytest.raises(OSError):
        await subject.set_temperature(29.0)
    assert subject.pending_target is None
    owner.client.async_set_temperature.assert_not_awaited()


async def test_startup_waits_for_fresh_q3_then_delivers_only_to_q3(hass):
    subject, owner = manager(hass, paused=True)
    await subject.set_temperature(29.0)
    owner.policy.power = "on"
    owner.availability = Availability("starting", False, True)
    await subject.refresh()
    owner.client.async_set_temperature.assert_not_awaited()
    owner.availability = Availability("working", True, True)
    await subject.refresh()
    assert_guarded_call(owner.client.async_set_temperature, 29.0)
    assert subject.pending_target is None
    records = [call.args[0] for call in subject._store.async_save.call_args_list]
    assert any(record["delivery"] == "sending" for record in records)


async def test_ambiguous_write_is_not_repeated_and_later_read_can_confirm(hass):
    subject, owner = manager(hass)
    owner.client.async_set_temperature.side_effect = WriteOutcomeUnknown("response lost")
    with pytest.raises(HomeAssistantError, match="unknown"):
        await subject.set_temperature(29.0)
    assert subject.pending_error == "write_outcome_unknown"
    await subject.refresh()
    await subject.refresh()
    owner.client.async_set_temperature.assert_awaited_once()
    owner.client.async_get_controls.return_value = replace(CONTROLS, target_temperature=29.0)
    await subject.refresh()
    assert subject.pending_target is None
    assert subject.pending_error is None
    owner.client.async_set_temperature.assert_awaited_once()


async def test_known_preflight_error_cause_reaches_ui_and_log_without_exception_context(hass, caplog):
    subject, owner = manager(hass)
    reason = "TelePool settings contain an unknown successful control"
    error = ProtocolError(reason)
    error.__cause__ = RuntimeError("password=SYNTHETIC_SECRET&verification_token=PRIVATE_TOKEN")
    owner.client.async_set_temperature.side_effect = error
    caplog.set_level(logging.WARNING, logger="custom_components.telepool.native_control")

    with pytest.raises(HomeAssistantError, match="ProtocolError") as reported:
        await subject.set_temperature(29.0)

    assert reason in str(reported.value)
    assert reason in caplog.text and "ProtocolError" in caplog.text
    assert "SYNTHETIC_SECRET" not in str(reported.value) + caplog.text
    assert "PRIVATE_TOKEN" not in str(reported.value) + caplog.text
    assert subject._delivery == "blocked" and subject.pending_error == "write_failed"
    await subject.refresh()
    await subject.refresh()
    assert_guarded_call(owner.client.async_set_temperature, 29.0)


@pytest.mark.parametrize(
    ("reason", "reported_reason"),
    [
        ("http_400", "HTTP 400"),
        ("http_403", "HTTP 403"),
        ("access_denied", "denied access"),
        ("validation_error", "validation errors"),
    ],
)
async def test_typed_settings_rejection_is_distinct_safe_and_not_replayed(hass, caplog, reason, reported_reason):
    subject, owner = manager(hass)
    owner.client.async_set_temperature.side_effect = SettingsRejected(reason)
    caplog.set_level(logging.WARNING, logger="custom_components.telepool.native_control")

    with pytest.raises(HomeAssistantError, match="SettingsRejected") as reported:
        await subject.set_temperature(29.0)

    assert reported_reason in str(reported.value)
    assert reported_reason in caplog.text
    assert subject._delivery == "blocked" and subject.pending_error == "write_failed"
    await subject.refresh()
    owner.client.async_set_temperature.assert_awaited_once()


async def test_validation_reason_displays_only_reviewed_field_names_not_private_payload(hass, caplog):
    subject, owner = manager(hass)
    error = SettingsRejected("validation_error", invalid_fields=("SenMaxPh", "SenMaxRx"))
    secret = "password=SYNTHETIC_SECRET&verification_token=PRIVATE_TOKEN"
    error.private_payload = secret
    error.args = (secret,)
    # Simulate a future caller bypassing the client's own field filter.
    error.invalid_fields = ("SenMaxRx", secret, "SenMaxPh", "__RequestVerificationToken", "WiFiName", "SenMaxPh")
    owner.client.async_set_temperature.side_effect = error
    caplog.set_level(logging.WARNING, logger="custom_components.telepool.native_control")

    with pytest.raises(HomeAssistantError, match="fields: SenMaxPh, SenMaxRx") as reported:
        await subject.set_temperature(29.0)

    combined = str(reported.value) + caplog.text
    assert "fields: SenMaxPh, SenMaxRx" in combined
    for forbidden in (secret, "SYNTHETIC_SECRET", "PRIVATE_TOKEN", "__RequestVerificationToken", "WiFiName"):
        assert forbidden not in combined
    assert subject.pending_error == "write_failed" and subject._delivery == "blocked"
    assert_guarded_call(owner.client.async_set_temperature, 29.0)


async def test_validation_field_names_are_bounded_again_at_the_ui_boundary(hass, caplog):
    subject, owner = manager(hass)
    error = SettingsRejected("validation_error")
    error.invalid_fields = tuple(f"Hour{hour}" for hour in range(10)) + ("unknown_private_field",)
    owner.client.async_set_temperature.side_effect = error
    caplog.set_level(logging.WARNING, logger="custom_components.telepool.native_control")

    with pytest.raises(HomeAssistantError) as reported:
        await subject.set_temperature(29.0)

    fields = str(reported.value).split("fields: ", 1)[1].split(")", 1)[0].split(", ")
    assert fields == [f"Hour{hour}" for hour in range(8)]
    assert "unknown_private_field" not in str(reported.value) + caplog.text


@pytest.mark.parametrize("reason", ["http_400", "http_403", "access_denied"])
async def test_non_validation_rejection_does_not_claim_invalid_fields(hass, caplog, reason):
    subject, owner = manager(hass)
    owner.client.async_set_temperature.side_effect = SettingsRejected(reason, invalid_fields=("SenMaxPh", "SenMaxRx"))
    caplog.set_level(logging.WARNING, logger="custom_components.telepool.native_control")

    with pytest.raises(HomeAssistantError) as reported:
        await subject.set_temperature(29.0)

    assert "fields:" not in str(reported.value) + caplog.text


@pytest.mark.parametrize("future_subclass", [False, True])
async def test_unreviewed_error_text_and_future_subclass_are_not_exposed(hass, caplog, future_subclass):
    class FutureClientError(ProtocolError):
        def __str__(self):
            raise AssertionError("Future exceptions must never be stringified")

    subject, owner = manager(hass)
    secret = "https://example.invalid/?password=SYNTHETIC_SECRET&token=PRIVATE_TOKEN"
    owner.client.async_set_temperature.side_effect = (FutureClientError if future_subclass else ProtocolError)(secret)
    caplog.set_level(logging.WARNING, logger="custom_components.telepool.native_control")

    with pytest.raises(HomeAssistantError, match="no safe diagnostic detail") as reported:
        await subject.set_temperature(29.0)

    combined = str(reported.value) + caplog.text
    assert secret not in combined and "SYNTHETIC_SECRET" not in combined and "PRIVATE_TOKEN" not in combined
    assert subject._delivery == "blocked" and subject.pending_error == "write_failed"
    owner.client.async_set_temperature.assert_awaited_once()


async def test_fu_setting_rejection_reports_safe_reason_without_mutating_or_queueing_flag(hass, caplog):
    subject, owner = manager(hass, options={"thermostat_mode": "disabled", "power_mode": "native_q3"})
    owner.client.async_set_enabled.side_effect = SettingsRejected("http_403")
    caplog.set_level(logging.WARNING, logger="custom_components.telepool.native_control")

    with pytest.raises(HomeAssistantError, match="HTTP 403"):
        await subject.set_enabled(False)

    assert "SettingsRejected" in caplog.text and "HTTP 403" in caplog.text
    assert subject.controls.menu_enable_fu is True
    assert subject.pending_target is None
    assert_guarded_call(owner.client.async_set_enabled, False)


async def test_crash_after_sending_marker_restores_uncertain_without_replay(hass):
    subject, owner = manager(hass)
    await subject._save(29.0, "sending", None)
    saved = subject._store.async_save.call_args.args[0]
    restored, restored_owner = manager(hass)
    restored._store.async_load.return_value = saved
    await restored.initialize()
    assert restored.pending_target == 29.0
    assert restored.pending_error == "write_outcome_unknown"
    await restored.refresh()
    restored_owner.client.async_set_temperature.assert_not_awaited()


@pytest.mark.parametrize("value", [35.5, 38.0, 28.1, float("nan"), True])
async def test_native_target_obeys_lower_form_limit_step_and_finite_values(hass, value):
    subject, owner = manager(hass)
    with pytest.raises(ServiceValidationError):
        await subject.set_temperature(value)
    owner.client.async_set_temperature.assert_not_awaited()
    subject._store.async_save.assert_not_awaited()


async def test_changed_live_range_blocks_old_pending_target(hass):
    subject, owner = manager(hass, paused=True)
    await subject.set_temperature(30.0)
    owner.policy.power = "on"
    owner.availability = Availability("working", True, True)
    owner.client.async_get_controls.return_value = replace(CONTROLS, max_temperature=29.0)
    await subject.refresh()
    assert subject.pending_target == 30.0
    assert subject.pending_error == "target_out_of_range"
    owner.client.async_set_temperature.assert_not_awaited()


async def test_unknown_power_during_actual_connection_failure_cannot_queue(hass):
    subject, owner = manager(hass)
    owner.policy.power = None
    owner.availability = Availability("no_connection", False, False)
    with pytest.raises(HomeAssistantError):
        await subject.set_temperature(29.0)
    assert subject.pending_target is None
    owner.client.async_set_temperature.assert_not_awaited()


async def test_native_disabled_never_reads_writes_or_restores_pending(hass):
    subject, owner = manager(hass, options={"thermostat_mode": "external", "power_mode": "disabled"})
    await subject.initialize()
    await subject.refresh()
    with pytest.raises(ServiceValidationError):
        await subject.set_temperature(29.0)
    subject._store.async_load.assert_not_awaited()
    owner.client.async_get_controls.assert_not_awaited()
    owner.client.async_set_temperature.assert_not_awaited()


async def test_native_fu_flag_is_explicit_online_only_without_queue(hass):
    subject, owner = manager(hass, options={"thermostat_mode": "disabled", "power_mode": "native_q3"})
    await subject.set_enabled(False)
    assert_guarded_call(owner.client.async_set_enabled, False)
    assert subject.controls.menu_enable_fu is False
    owner.availability = Availability("paused", False, True)
    owner.policy.power = "off"
    with pytest.raises(HomeAssistantError):
        await subject.set_enabled(True)
    owner.client.async_set_enabled.assert_awaited_once()


async def test_unload_cancels_control_read_before_any_delivery(hass):
    subject, owner = manager(hass, paused=True)
    await subject.set_temperature(29.0)
    owner.availability = Availability("working", True, True)
    owner.policy.power = "on"
    entered = asyncio.Event()

    async def slow_read(_station):
        entered.set()
        await asyncio.Future()

    owner.client.async_get_controls.side_effect = slow_read
    task = asyncio.create_task(subject.refresh())
    await entered.wait()
    subject.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    owner.client.async_set_temperature.assert_not_awaited()


async def test_backend_generation_has_distinct_pending_storage_namespace(hass):
    old, _ = manager(hass, options={"thermostat_mode": "native_q3", "native_cache_generation": "a" * 32})
    new, _ = manager(hass, options={"thermostat_mode": "native_q3", "native_cache_generation": "b" * 32})
    # Use real instances here; helper normally substitutes in-memory storage.
    first = NativeControlManager(old.owner)
    second = NativeControlManager(new.owner)
    assert first._store.key != second._store.key


async def test_queued_off_target_never_auto_flushes_with_unknown_relay_even_if_cloud_is_fresh(hass):
    subject, owner = manager(hass, paused=True)
    await subject.set_temperature(29.0)
    owner.policy.power = None
    owner.availability = Availability("working", True, True)

    await subject.refresh()

    assert subject.available
    assert subject.pending_target == 29.0
    owner.client.async_set_temperature.assert_not_awaited()


async def test_auto_delivery_rechecks_relay_after_durable_sending_marker(hass):
    subject, owner = manager(hass, paused=True)
    await subject.set_temperature(29.0)
    owner.policy.power = "on"
    owner.availability = Availability("working", True, True)

    async def lose_relay_confirmation(record):
        if record["delivery"] == "sending":
            owner.policy.power = None

    subject._store.async_save.side_effect = lose_relay_confirmation
    await subject.refresh()

    owner.client.async_set_temperature.assert_not_awaited()
    assert subject.pending_target == 29.0
    assert subject._delivery == "queued"


@pytest.mark.parametrize("save_phase", ["queued", "sending"])
@pytest.mark.parametrize("changed", ["unknown", "disconnected", "off"])
async def test_explicit_conditions_change_during_either_save_never_silently_queues_connection_loss(
    hass, save_phase, changed
):
    subject, owner = manager(hass)

    async def lose_relay_confirmation(record):
        if record["delivery"] == save_phase:
            if changed == "unknown":
                owner.policy.power = None
            elif changed == "disconnected":
                owner.availability = Availability("no_connection", False, False)
            else:
                owner.policy.power = "off"
                owner.availability = Availability("paused", False, True)

    subject._store.async_save.side_effect = lose_relay_confirmation
    if changed == "off":
        await subject.set_temperature(29.0)
        assert subject._delivery == "queued"
        assert subject.pending_error is None
    else:
        with pytest.raises(HomeAssistantError, match="no command was sent"):
            await subject.set_temperature(29.0)
        assert subject._delivery == "blocked"
        assert subject.pending_error == "write_failed"

    owner.client.async_set_temperature.assert_not_awaited()
    assert subject.pending_target == 29.0


@pytest.mark.parametrize("paused_after_preflight", [True, False])
async def test_explicit_transport_guard_loss_only_queues_for_confirmed_off(hass, paused_after_preflight):
    subject, owner = manager(hass)
    actual_posts = []

    async def change_conditions_before_post(_station, _target, *, dispatch_guard):
        owner.policy.power = "off" if paused_after_preflight else None
        owner.availability = (
            Availability("paused", False, True)
            if paused_after_preflight
            else Availability("no_connection", False, False)
        )
        if not dispatch_guard():
            raise CommandNotSent("Guard denied before POST")
        actual_posts.append(_target)
        return replace(CONTROLS, target_temperature=_target)

    owner.client.async_set_temperature.side_effect = change_conditions_before_post
    if paused_after_preflight:
        await subject.set_temperature(29.0)
        assert subject._delivery == "queued" and subject.pending_error is None
    else:
        with pytest.raises(HomeAssistantError, match="no command was sent"):
            await subject.set_temperature(29.0)
        assert subject._delivery == "blocked" and subject.pending_error == "write_failed"
    assert_guarded_call(owner.client.async_set_temperature, 29.0)
    assert actual_posts == []
    assert subject.pending_target == 29.0


async def test_auto_transport_guard_loss_preserves_queue_until_a_later_fresh_on(hass):
    subject, owner = manager(hass, paused=True)
    await subject.set_temperature(29.0)
    owner.policy.power = "on"
    owner.availability = Availability("working", True, True)
    actual_posts = []

    async def refuse_before_post(_station, _target, *, dispatch_guard):
        owner.policy.power = None
        assert not dispatch_guard()
        raise CommandNotSent("Guard denied before POST")

    owner.client.async_set_temperature.side_effect = refuse_before_post
    await subject.refresh()
    assert subject._delivery == "queued"
    assert subject.pending_target == 29.0 and subject.pending_error is None

    async def accept_when_guarded(_station, _target, *, dispatch_guard):
        assert dispatch_guard()
        actual_posts.append(_target)
        return replace(CONTROLS, target_temperature=_target)

    owner.policy.power = "on"
    owner.client.async_set_temperature.side_effect = accept_when_guarded
    await subject.refresh()
    assert actual_posts == [29.0]
    assert owner.client.async_set_temperature.await_count == 2
    assert subject.pending_target is None


async def test_fu_dispatch_guard_refusal_never_queues_or_changes_reported_flag(hass):
    subject, owner = manager(hass, options={"thermostat_mode": "disabled", "power_mode": "native_q3"})

    async def refuse_before_post(_station, _enabled, *, dispatch_guard):
        owner.availability = Availability("no_connection", False, False)
        assert not dispatch_guard()
        raise CommandNotSent("Guard denied before POST")

    owner.client.async_set_enabled.side_effect = refuse_before_post
    with pytest.raises(HomeAssistantError, match="no command was sent"):
        await subject.set_enabled(False)
    assert_guarded_call(owner.client.async_set_enabled, False)
    assert subject.controls.menu_enable_fu is True
    assert subject.pending_target is None
    subject._store.async_save.assert_not_awaited()


async def test_explicit_online_native_target_without_external_relay_is_allowed(hass):
    subject, owner = manager(hass, options={"thermostat_mode": "native_q3", "power_mode": "disabled"})
    owner.policy.power = None

    await subject.set_temperature(29.0)

    assert_guarded_call(owner.client.async_set_temperature, 29.0)
    assert subject.pending_target is None


async def test_unload_during_post_restores_sending_marker_as_unknown_and_never_replays(hass):
    subject, owner = manager(hass)
    entered = asyncio.Event()

    async def wait_for_unknown_outcome(_station, _target, *, dispatch_guard):
        assert dispatch_guard()
        entered.set()
        await asyncio.Future()

    owner.client.async_set_temperature.side_effect = wait_for_unknown_outcome
    command = asyncio.create_task(subject.set_temperature(29.0))
    await entered.wait()
    saved = subject._store.async_save.call_args.args[0]
    assert saved["delivery"] == "sending"
    subject.close()
    with pytest.raises(asyncio.CancelledError):
        await command

    restored, restored_owner = manager(hass)
    restored._store.async_load.return_value = saved
    await restored.initialize()
    await restored.refresh()

    assert restored.pending_target == 29.0
    assert restored.pending_error == "write_outcome_unknown"
    restored_owner.client.async_set_temperature.assert_not_awaited()


async def test_controls_read_without_q3_point_does_not_invent_measurement_time(hass):
    subject, owner = manager(hass)
    owner.snapshot = None
    owner.availability = Availability("no_connection", False, False)

    await subject.refresh()

    assert subject.controls == CONTROLS
    assert subject.source_time is None
    assert not subject.available
    subject._store.async_save.assert_not_awaited()


async def test_confirmed_fu_update_persists_readback_without_clearing_temperature_intention(hass):
    subject, owner = manager(hass, options={"thermostat_mode": "native_q3", "power_mode": "native_q3"})
    subject.pending_target = 30.0
    subject._delivery = "unknown"
    subject.pending_error = "write_outcome_unknown"
    returned = replace(CONTROLS, target_temperature=29.0, menu_enable_fu=False)
    owner.client.async_set_enabled.return_value = returned

    await subject.set_enabled(False)

    stored = subject._store.async_save.call_args.args[0]
    assert stored["controls"]["target_temperature"] == 29.0
    assert stored["controls"]["menu_enable_fu"] is False
    assert (stored["pending"], stored["delivery"], stored["error"]) == (30.0, "unknown", "write_outcome_unknown")
    assert subject.controls == returned
    subject._store.async_save.side_effect = OSError("disk full")
    # Remote read-back was confirmed; an optional cache failure cannot claim
    # that the remote operation failed or issue another POST.
    await subject.set_enabled(False)
    assert owner.client.async_set_enabled.await_count == 2


@pytest.fixture
async def real_coordinator(hass):
    """Exercise the actual relay/watermark policy with network methods mocked."""
    entries = []

    def make():
        entry = MockConfigEntry(
            domain="telepool",
            options={"thermostat_mode": "native_q3", "power_mode": "external", "power_source": "switch.external"},
        )
        entry.add_to_hass(hass)
        entries.append(entry)
        now = dt_util.utcnow()
        cached = Snapshot(
            STATION.id,
            {"temperature": 27.0, "ph": 7.2},
            {},
            now - timedelta(minutes=1),
            now,
            False,
            False,
            None,
            None,
        )
        client = SimpleNamespace(
            async_fetch=AsyncMock(return_value=cached),
            async_get_controls=AsyncMock(return_value=CONTROLS),
            async_set_temperature=AsyncMock(return_value=replace(CONTROLS, target_temperature=29.0)),
            async_set_enabled=AsyncMock(return_value=CONTROLS),
        )
        subject = TelePoolCoordinator(hass, entry, client, STATION)
        subject.snapshot = cached
        subject.cloud_ok = True
        subject._store = SimpleNamespace(async_save=AsyncMock())
        subject._native.controls = CONTROLS
        subject._native._read_ok = True
        subject._native._store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
        hass.states.async_set("switch.external", "off")
        subject._read_power()
        return subject, client, cached

    yield make
    for entry in entries:
        await entry._async_process_on_unload(hass)
    await hass.async_block_till_done()


async def test_actual_coordinator_off_cache_holds_and_skips_all_cloud_control_io(real_coordinator):
    subject, client, cached = real_coordinator()

    assert await subject._async_update_data() is cached
    assert subject.availability.status == "paused"
    assert subject.native_held and not subject.native_available
    await subject.async_set_native_temperature(29.0)

    assert subject.native_pending_target == 29.0
    assert subject._native._store.async_save.call_args.args[0]["delivery"] == "queued"
    client.async_fetch.assert_not_awaited()
    client.async_get_controls.assert_not_awaited()
    client.async_set_temperature.assert_not_awaited()
    subject._store.async_save.assert_not_awaited()


async def test_actual_coordinator_on_watermark_requires_new_q3_point_before_one_delivery(hass, real_coordinator):
    subject, client, cached = real_coordinator()
    await subject.async_set_native_temperature(29.0)
    hass.states.async_set("switch.external", "on")
    subject._read_power()
    assert subject.policy.boot_watermark == cached.observed_at
    assert subject.availability.status == "starting"

    # A new HTTP response and readable controls do not make an unchanged Q3
    # measurement a new post-boot point.
    client.async_fetch.return_value = replace(cached, received_at=dt_util.utcnow())
    await subject._async_update_data()
    assert subject.native_held and not subject.native_available
    assert subject.native_pending_target == 29.0
    assert subject.native_source_time == cached.observed_at.isoformat()
    client.async_set_temperature.assert_not_awaited()

    newer = replace(cached, observed_at=dt_util.utcnow(), received_at=dt_util.utcnow())
    client.async_fetch.return_value = newer
    await subject._async_update_data()

    assert subject.native_available and not subject.native_held
    assert_guarded_call(client.async_set_temperature, 29.0)
    assert subject.native_pending_target is None
    assert subject.native_source_time == newer.observed_at.isoformat()
    client.async_get_controls.return_value = replace(CONTROLS, target_temperature=29.0)
    await subject._async_update_data()
    client.async_set_temperature.assert_awaited_once()
