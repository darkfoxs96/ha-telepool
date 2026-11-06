from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.telepool.availability import Availability
from custom_components.telepool.client import Snapshot
from custom_components.telepool.sensor import READINGS, TelePoolReading


def coordinator(*, fresh=False, available=True):
    return SimpleNamespace(
        station=SimpleNamespace(id="test", name="Pool", firmware=None),
        snapshot=Snapshot(
            "test",
            {"ph": 6.48, "rx": 751, "temperature": None},
            {},
            datetime(2026, 11, 5, tzinfo=UTC),
            None,
            False,
            True,
            -73,
            54,
        ),
        availability=Availability("paused", fresh, available),
    )


def test_display_holds_while_optional_measurement_has_gap():
    coord = coordinator()
    display = TelePoolReading(coord, next(r for r in READINGS if r.key == "ph"))
    measured = TelePoolReading(coord, next(r for r in READINGS if r.key == "ph_measurement"))
    assert display.native_value == 6.48 and display.available
    assert display.state_class is None
    assert not measured.available
    assert display.extra_state_attributes["data_fresh"] is False


def test_alias_scaling_does_not_change_original_and_missing_temperature_is_unavailable():
    coord = coordinator()
    alias = TelePoolReading(coord, next(r for r in READINGS if r.key == "rx_export"))
    temp = TelePoolReading(coord, next(r for r in READINGS if r.key == "temperature"))
    assert alias.native_value == pytest.approx(75.1)
    assert coord.snapshot.values["rx"] == 751
    assert temp.native_value is None and not temp.available
