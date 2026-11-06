from datetime import UTC, datetime, timedelta

from custom_components.telepool.availability import PowerPolicy

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


def evaluate(policy, observed=NOW, *, cloud=True, stale=False, values=True, now=NOW):
    return policy.evaluate(now, observed, server_stale=stale, cloud_ok=cloud, has_values=values)


def test_expected_power_off_holds_valid_values_even_when_cloud_fails():
    policy = PowerPolicy()
    policy.set_power("off", NOW, NOW)
    result = evaluate(policy, NOW - timedelta(days=1), cloud=False, stale=True)
    assert (result.status, result.fresh, result.display_available) == ("paused", False, True)
    assert not evaluate(policy, None, values=False).display_available


def test_power_on_requires_new_timestamp_then_becomes_unavailable_after_grace():
    policy = PowerPolicy(startup_grace=180)
    policy.set_power("off", NOW, NOW)
    policy.set_power("on", NOW, NOW)
    result = evaluate(policy)
    assert result.status == "starting" and result.display_available and not result.fresh
    result = evaluate(policy, now=NOW + timedelta(seconds=181))
    assert result.status == "no_connection" and not result.display_available
    result = evaluate(policy, NOW + timedelta(minutes=1), now=NOW + timedelta(minutes=2))
    assert result.status == "working" and result.fresh


def test_native_pause_or_unknown_relay_does_not_hold_old_readings():
    policy = PowerPolicy(stale_after=900)
    for source in (None, "unknown", "unavailable"):
        policy.set_power(source, NOW, NOW)
        assert not evaluate(policy, NOW - timedelta(seconds=901)).display_available
        assert evaluate(policy).fresh


def test_stale_flag_invalid_future_and_cloud_failure_are_not_fresh():
    policy = PowerPolicy()
    assert not evaluate(policy, stale=True).fresh
    assert not evaluate(policy, cloud=False).fresh
    assert not evaluate(policy, NOW + timedelta(hours=1)).fresh
    assert not evaluate(policy, None).fresh
