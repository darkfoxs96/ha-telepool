"""Async HTTP behavior tests without contacting TelePool or using credentials."""

import asyncio
import unittest
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from custom_components.telepool.client import (
    AuthenticationError,
    ConnectionError,
    ProtocolError,
    Station,
    TelePoolClient,
)

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "examples"
STATION_ID = "123456789012345"
USER_ID = "00000000-0000-4000-8000-000000000001"
LOGIN = '<form action="/Account/Login"><input name="Password"><input name="__RequestVerificationToken" value="login-token"></form>'
DASHBOARD = f"""
<input name="__RequestVerificationToken" value="cabinet-token">
<input id="DevicesMy" value='[{{"Id":{STATION_ID},"Type":9}}]'>
<div id="q3-data-user" data-q3-data='{{"myPool":true,"user":"{USER_ID}"}}'></div>
<section id="accordion_pool-a"><h2 class="accordion-header"><button data-bs-target="#collapse_pool_pool-a">Бассейн</button></h2>
<div id="collapse_pool_pool-a"><div id="q3-display-{STATION_ID}"></div></div></section>
"""


@dataclass
class Response:
    """Small fake of aiohttp's response context manager."""

    body: str = ""
    status: int = 200
    headers: dict = field(default_factory=dict)
    wait: object = None
    error: Exception | None = None

    @property
    def content_length(self):
        return len(self.body)

    async def __aenter__(self):
        if self.wait:
            await self.wait()
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return self.body


class Session:
    """Route-based queue permits realistic interleaved session recovery."""

    def __init__(self):
        self.routes = defaultdict(deque)
        self.calls = []

    def add(self, method, path, *responses):
        self.routes[method, path].extend(responses)

    def login(self, *, token="cabinet-token", success=True):
        self.add("GET", "/Account/Login", Response(LOGIN))
        if success:
            self.add("POST", "/Account/Login", Response(status=302, headers={"Location": "/"}))
            self.add("GET", "/", Response(DASHBOARD.replace("cabinet-token", token)))
        else:
            self.add("POST", "/Account/Login", Response(LOGIN))

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        key = method, urlsplit(url).path
        if not self.routes[key]:
            raise AssertionError(f"Unexpected request to {key}")
        return self.routes[key].popleft()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = Session()
        self.client = TelePoolClient(self.session, "synthetic-login", "synthetic-password")
        self.station = Station(STATION_ID, "pool-a", "Бассейн", "Q3", 7, user_id=USER_ID)
        self.face = (FIXTURES / "face-data.json").read_text()
        self.chart = (FIXTURES / "chart-q3.json").read_text()

    def successful_fetch(self):
        self.session.add("POST", "/Home/FaceData", Response(self.face))
        self.session.add("POST", "/Home/ChartQ3", Response(self.chart))

    async def test_discovery_and_fetch_use_the_observed_forms(self):
        self.session.login()
        self.session.add("GET", "/", Response(DASHBOARD))
        self.session.add("POST", "/Home/FaceData", Response(self.face))
        stations = await self.client.async_discover()
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0].screen_type, 7)
        self.assertEqual(stations[0].pool_name, "Бассейн")
        self.successful_fetch()
        result = await self.client.async_fetch(stations[0])
        self.assertEqual(result.values["rx"], 751)
        login_calls = [call for call in self.session.calls if call[:2] == ("POST", "https://telepool.ru/Account/Login")]
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(
            login_calls[0][2]["data"],
            {
                "Mail": "synthetic-login",
                "Password": "synthetic-password",
                "RememberMe": "false",
                "__RequestVerificationToken": "login-token",
            },
        )
        face_call = next(call for call in self.session.calls if call[1].endswith("/Home/FaceData"))
        self.assertEqual(
            face_call[2]["data"],
            {
                "devices[0][Id]": STATION_ID,
                "devices[0][Type]": "9",
                "__RequestVerificationToken": "cabinet-token",
            },
        )
        chart_call = self.session.calls[-1]
        self.assertEqual(
            chart_call[2]["data"],
            {
                "num": STATION_ID,
                "datetime": "",
                "span": "30",
                "compress": "0",
                "__RequestVerificationToken": "cabinet-token",
            },
        )
        self.assertTrue(all(not call[2]["allow_redirects"] for call in self.session.calls))

    async def test_login_redirect_reauthenticates_once_and_uses_new_csrf(self):
        self.session.login()
        self.session.login(token="replacement-token")
        self.session.add(
            "POST",
            "/Home/FaceData",
            Response(status=302, headers={"Location": "/Account/Login?ReturnUrl=%2F"}),
            Response(self.face),
        )
        self.session.add("POST", "/Home/ChartQ3", Response(self.chart))
        result = await self.client.async_fetch(self.station)
        self.assertEqual(result.values["ph"], 6.48)
        face_calls = [call for call in self.session.calls if call[1].endswith("/Home/FaceData")]
        self.assertEqual(
            [call[2]["data"]["__RequestVerificationToken"] for call in face_calls],
            ["cabinet-token", "replacement-token"],
        )
        self.assertEqual(
            sum(call[0] == "POST" and call[1].endswith("/Account/Login") for call in self.session.calls), 2
        )

    async def test_200_login_page_is_not_parsed_as_json(self):
        self.session.login()
        self.session.login()
        self.session.add("POST", "/Home/FaceData", Response(LOGIN), Response(self.face))
        self.session.add("POST", "/Home/ChartQ3", Response(self.chart))
        self.assertFalse((await self.client.async_fetch(self.station)).stale)

    async def test_repeated_unauthorized_response_stops_after_one_retry(self):
        self.session.login()
        self.session.login()
        self.session.add("POST", "/Home/FaceData", Response(status=401), Response(status=401))
        with self.assertRaises(AuthenticationError):
            await self.client.async_fetch(self.station)
        self.assertEqual(
            sum(call[0] == "POST" and call[1].endswith("/Account/Login") for call in self.session.calls), 2
        )

    async def test_access_denied_and_csrf_failure_do_not_reauthenticate(self):
        for response in (
            Response(status=302, headers={"Location": "/Account/AccessDenied"}),
            Response(status=403),
            Response(status=400),
        ):
            with self.subTest(status=response.status):
                session = Session()
                session.login()
                session.add("POST", "/Home/FaceData", response)
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(ProtocolError):
                    await client.async_fetch(self.station)
                self.assertEqual(
                    sum(call[0] == "POST" and call[1].endswith("/Account/Login") for call in session.calls), 1
                )

    async def test_bad_credentials_are_sanitized(self):
        self.session.login(success=False)
        with self.assertRaises(AuthenticationError) as raised:
            await self.client.async_fetch(self.station)
        self.assertNotIn("synthetic-login", str(raised.exception))
        self.assertNotIn("synthetic-password", str(raised.exception))
        self.assertNotIn("login-token", str(raised.exception))
        # The next scheduled poll does not submit the same bad password again.
        with self.assertRaises(AuthenticationError):
            await self.client.async_fetch(self.station)
        self.assertEqual(len(self.session.calls), 2)

    async def test_foreign_and_control_redirects_cannot_forward_credentials(self):
        for location, status in (
            ("https://attacker.invalid/", 307),
            ("http://telepool.ru/", 302),
            ("/Control/Q3/Index/123", 307),
            ("/Home/FaceData", 307),
        ):
            with self.subTest(location=location):
                session = Session()
                session.add("GET", "/Account/Login", Response(LOGIN))
                session.add("POST", "/Account/Login", Response(status=status, headers={"Location": location}))
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(ProtocolError):
                    await client.async_fetch(self.station)
                self.assertEqual(len(session.calls), 2)
                self.assertTrue(all(urlsplit(call[1]).hostname == "telepool.ru" for call in session.calls))

    async def test_network_errors_and_invalid_json_never_expose_response_secrets(self):
        for response, exception in (
            (Response(error=asyncio.TimeoutError("sensitive text")), ConnectionError),
            (Response(error=aiohttp.ClientConnectionError("sensitive text")), ConnectionError),
            (Response(status=503, body="sensitive text"), ConnectionError),
            (Response(body="sensitive text"), ProtocolError),
        ):
            with self.subTest(exception=exception):
                session = Session()
                session.login()
                session.add("POST", "/Home/FaceData", response)
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(exception) as raised:
                    await client.async_fetch(self.station)
                self.assertNotIn("sensitive", str(raised.exception))

    async def test_concurrent_expiry_causes_only_one_new_login(self):
        self.session.login()
        await self.client._ensure_authenticated()
        self.session.login(token="replacement-token")
        both_started = asyncio.Event()
        started = 0

        async def expired_together():
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()

        self.session.add(
            "POST",
            "/Home/FaceData",
            Response(status=302, headers={"Location": "/Account/Login"}, wait=expired_together),
            Response(status=302, headers={"Location": "/Account/Login"}, wait=expired_together),
            Response(self.face),
            Response(self.face),
        )
        self.session.add("POST", "/Home/ChartQ3", Response(self.chart), Response(self.chart))
        results = await asyncio.wait_for(
            asyncio.gather(
                self.client.async_fetch(self.station),
                self.client.async_fetch(self.station),
            ),
            timeout=2,
        )
        self.assertEqual([result.values["ph"] for result in results], [6.48, 6.48])
        self.assertEqual(
            sum(call[0] == "POST" and call[1].endswith("/Account/Login") for call in self.session.calls), 2
        )

    async def test_empty_or_missing_station_is_not_silently_invented(self):
        self.session.login()
        self.session.add("POST", "/Home/FaceData", Response("[]"))
        with self.assertRaises(ProtocolError):
            await self.client.async_fetch(self.station)
        self.assertFalse(any(call[1].endswith("/Home/ChartQ3") for call in self.session.calls))
