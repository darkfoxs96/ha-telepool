"""Protocol regression tests using sanitized Q3 captures and synthetic cabinet HTML."""

import html
import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from custom_components.telepool.client import ProtocolError, Station
from custom_components.telepool.client.parsers import (
    extract_csrf_token,
    is_login_page,
    parse_dashboard,
    parse_datetime,
    parse_face_data,
    parse_snapshot,
)

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "examples"
STATION_ID = "123456789012345"
USER_ID = "00000000-0000-4000-8000-000000000001"


def dashboard_html(devices=None) -> str:
    """Synthetic structure, not a captured or proven full dashboard fixture."""
    if devices is None:
        devices = [{"Id": int(STATION_ID), "Type": 9}, {"Id": "second-q3", "Type": 9}]
    devices_json = html.escape(json.dumps(devices), quote=True)
    context_json = html.escape(json.dumps({"myPool": True, "user": USER_ID}), quote=True)
    return f"""
    <input name="__RequestVerificationToken" value="cabinet-csrf">
    <input id="DevicesMy" value="{devices_json}">
    <div id="q3-data-user" data-q3-data="{context_json}"></div>
    <section id="accordion_pool-a">
      <h2 class="accordion-header"><button data-bs-target="#collapse_pool_pool-a">Большой бассейн</button></h2>
      <div id="collapse_pool_pool-a"><div id="q3-display-{STATION_ID}"></div></div>
    </section>
    <section id="accordion_pool-b">
      <h2 class="accordion-header"><button aria-controls="collapse_pool_pool-b">Малый бассейн</button></h2>
      <div id="collapse_pool_pool-b"><div id="q3-display-second-q3"></div></div>
    </section>
    """


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.chart = json.loads((FIXTURES / "chart-q3.json").read_text())
        self.face_payload = json.loads((FIXTURES / "face-data.json").read_text())
        self.station = Station(STATION_ID, "pool-a", "Бассейн", "Q3", 7, user_id=USER_ID)

    def snapshot(self, chart=None, face_payload=None, station=None):
        station = station or self.station
        face = parse_face_data(self.face_payload if face_payload is None else face_payload)[station.id]
        return parse_snapshot(station, self.chart if chart is None else chart, face)

    def test_real_json_values_modes_and_distinct_times(self):
        result = self.snapshot()
        self.assertEqual(result.values, {"ph": 6.48, "rx": 751, "temperature": None, "set_ph": 7.3, "set_rx": 600})
        self.assertEqual(result.modes, {"ph": 3, "rx": 3, "fu": 24, "temperature": 15})
        self.assertEqual(result.observed_at.isoformat(), "2026-01-02T10:10:00+03:00")
        self.assertEqual(result.received_at.isoformat(), "2026-01-02T10:10:30+03:00")
        self.assertFalse(result.stale)
        self.assertTrue(result.error)
        self.assertEqual(result.rssi, -73)
        self.assertEqual(result.quality, 56)
        self.assertNotIn("fcl", result.values)
        self.assertNotIn("fcl", result.modes)

    def test_html_date_discrepancy_is_not_used(self):
        screen = (FIXTURES / "q3-screen.html").read_text()
        self.assertIn("01.01.26", screen)
        self.assertEqual(self.snapshot().observed_at.day, 2)

    def test_chart_need_not_arrive_in_time_order(self):
        self.chart["data"] = list(reversed(self.chart["data"]))
        result = self.snapshot()
        self.assertEqual(result.observed_at.minute, 10)
        self.assertEqual(result.values["rx"], 751)

    def test_server_stale_flags_are_combined_without_inventing_online(self):
        self.face_payload[0]["oldDateTime"] = True
        self.assertTrue(self.snapshot().stale)
        self.face_payload[0]["oldDateTime"] = False
        self.chart["old"] = True
        self.assertTrue(self.snapshot().stale)
        self.chart.pop("old")
        self.assertTrue(self.snapshot().stale)

    def test_missing_samples_have_no_fabricated_values(self):
        self.chart["data"] = []
        result = self.snapshot()
        self.assertTrue(result.stale)
        self.assertIsNone(result.observed_at)
        self.assertTrue(all(value is None for value in result.values.values()))
        self.assertEqual(result.modes, {})

    def test_missing_timestamp_is_explicitly_stale(self):
        self.chart["data"] = [{"calcPh": 6.48, "visit": "invalid"}]
        result = self.snapshot()
        self.assertEqual(result.values["ph"], 6.48)
        self.assertIsNone(result.observed_at)
        self.assertTrue(result.stale)

    def test_fcl_only_exists_when_the_reported_mask_contains_it(self):
        self.face_payload[0]["q3"]["screenType"] = 15
        result = self.snapshot()
        self.assertEqual(result.values["fcl"], 0.0)
        self.assertEqual(result.values["set_fcl"], 0.1)
        self.assertEqual(result.modes["fcl"], 20)
        self.face_payload[0]["q3"]["screenType"] = 1
        result = self.snapshot()
        self.assertIsNone(result.values["ph"])
        self.assertIsNone(result.values["rx"])
        self.assertNotIn("ph", result.modes)
        self.assertNotIn("fcl", result.values)

    def test_empty_current_minute_bucket_does_not_erase_last_measurement(self):
        # Observed in the live API: 30 numeric points followed by one null bucket.
        tail = dict.fromkeys(self.chart["data"][-1])
        tail["visit"] = "2026-01-02T10:11:00"
        self.chart["data"].append(tail)
        result = self.snapshot()
        self.assertEqual(result.values["ph"], 6.48)
        self.assertEqual(result.values["rx"], 751)
        self.assertEqual(result.observed_at.isoformat(), "2026-01-02T10:10:00+03:00")
        self.assertFalse(result.stale)

    def test_all_null_buckets_do_not_provide_a_measurement_timestamp(self):
        self.chart["data"] = [{"visit": "2026-01-02T10:11:00", "calcPh": None, "calcRx": None}]
        result = self.snapshot()
        self.assertIsNone(result.observed_at)
        self.assertTrue(result.stale)

    def test_real_temperature_zero_is_not_absent_and_nonfinite_is_rejected(self):
        latest = self.chart["data"][-1]
        latest.update(tempFu=0, calcPh="6,48", calcRx=float("nan"), setRx=float("inf"))
        result = self.snapshot()
        self.assertEqual(result.values["temperature"], 0)
        self.assertEqual(result.values["ph"], 6.48)
        self.assertIsNone(result.values["rx"])
        self.assertIsNone(result.values["set_rx"])

    def test_string_false_is_not_truthy_and_invalid_quality_falls_back(self):
        self.face_payload[0]["oldDateTime"] = "false"
        self.face_payload[0]["q3"]["error"] = "False"
        self.chart.update(old="false", quality=101)
        result = self.snapshot()
        self.assertFalse(result.stale)
        self.assertFalse(result.error)
        self.assertEqual(result.quality, 54)

    def test_dates_have_an_explicit_moscow_assumption_but_keep_offsets(self):
        self.assertEqual(parse_datetime("2026-01-02T10:10:00").utcoffset(), timedelta(hours=3))
        self.assertEqual(parse_datetime("02.01.2026 10:11").isoformat(), "2026-01-02T10:11:00+03:00")
        self.assertEqual(parse_datetime("2026-01-02T10:10:00Z"), datetime(2026, 1, 2, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(parse_datetime("2026-01-02T10:10:00-02:00").utcoffset(), timedelta(hours=-2))
        self.assertIsNone(parse_datetime(None))
        self.assertIsNone(parse_datetime("not a timestamp"))

    def test_synthetic_multiple_pools_keep_associations_and_deduplicate(self):
        stations = parse_dashboard(
            dashboard_html(
                [
                    {"Id": STATION_ID, "Type": 9},
                    {"Id": "second-q3", "Type": 9},
                    {"Id": STATION_ID, "Type": 9},
                    {"Id": "other", "Type": 2},
                ]
            )
        )
        self.assertEqual([station.id for station in stations], [STATION_ID, "second-q3"])
        self.assertEqual([station.pool_id for station in stations], ["pool-a", "pool-b"])
        self.assertEqual([station.pool_name for station in stations], ["Большой бассейн", "Малый бассейн"])
        self.assertEqual(stations[0].user_id, USER_ID)
        self.assertNotIn(USER_ID, repr(stations[0]))
        self.assertIsNone(stations[0].firmware)
        self.assertEqual(parse_dashboard(dashboard_html([])), [])

    def test_changed_or_malformed_pages_fail_without_echoing_payload(self):
        for page in (
            "sensitive-response-content",
            dashboard_html().replace("DevicesMy", "changed"),
            dashboard_html().replace(f"q3-display-{STATION_ID}", "changed"),
            dashboard_html().replace("data-q3-data", "changed-context"),
            dashboard_html()
            .replace("collapse_pool_pool-a", "changed-pool")
            .replace("accordion_pool-a", "changed-accordion"),
        ):
            with self.subTest(page=page[:20]):
                with self.assertRaises(ProtocolError) as raised:
                    parse_dashboard(page)
                self.assertNotIn("sensitive-response-content", str(raised.exception))
                self.assertNotIn(USER_ID, str(raised.exception))

    def test_login_token_and_login_detection(self):
        self.assertEqual(extract_csrf_token('<input name="__RequestVerificationToken" value="a&amp;b">'), "a&b")
        self.assertTrue(is_login_page('<form action="/Account/Login"><input name="Mail"></form>'))
        self.assertTrue(is_login_page('<input name="Password">'))
        self.assertFalse(is_login_page(dashboard_html()))
        with self.assertRaises(ProtocolError):
            extract_csrf_token("<html></html>")

    def test_invalid_json_structures_and_wrong_station_raise_protocol_errors(self):
        for face in ({}, [None], [{"q3": {}}], [{"num": STATION_ID, "q3": {"screenType": 16}}]):
            with self.subTest(face=face), self.assertRaises(ProtocolError):
                parse_face_data(face)
        for chart in ({}, {"data": {}}, {"data": [None]}):
            with self.subTest(chart=chart), self.assertRaises(ProtocolError):
                self.snapshot(chart=chart)
        wrong_face = deepcopy(self.face_payload)
        wrong_face[0]["num"] = "another-station"
        with self.assertRaises(ProtocolError):
            parse_snapshot(self.station, self.chart, parse_face_data(wrong_face)["another-station"])
