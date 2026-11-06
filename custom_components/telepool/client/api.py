"""Async telemetry and narrowly scoped settings client for TelePool's website.

Pass a dedicated aiohttp ClientSession with its own cookie jar, never Home
Assistant's shared session. The caller owns/ closes that session. Credentials
and session cookies are held only in memory here. Settings writes change only
SetPointTemp or the raw MenuEnableFu flag, preserving the fresh successful form
and forcing btnId=Empty. No dosing, calibration or service command is exposed.
"""

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from .controls import (
    _Q3Form,
    control_url,
    has_form_errors,
    parse_controls_form,
    temperature_value,
    validation_error_fields,
)
from .exceptions import (
    AuthenticationError,
    CommandNotSent,
    ConnectionError,
    ProtocolError,
    SettingsRejected,
    WriteOutcomeUnknown,
)
from .models import Q3Controls, Snapshot, Station
from .parsers import (
    Q3_DEVICE_TYPE,
    extract_csrf_token,
    is_login_page,
    parse_dashboard,
    parse_face_data,
    parse_snapshot,
)

BASE_URL = "https://telepool.ru"
_REDIRECTS = frozenset((301, 302, 303, 307, 308))
_POST_PATHS = frozenset(("/Account/Login", "/Home/FaceData", "/Home/ChartQ3"))
_GET_PATHS = frozenset(("/", "/Account/Login"))
_MAX_RESPONSE_LENGTH = 4 * 1024 * 1024


class _SessionExpired(Exception):
    """Internal signal; one login and one read retry are allowed."""


class TelePoolClient:
    """Authenticate and read the user's own Q3 stations.

    Concurrent readers share a generation-checked login lock, so a burst of
    expired-session responses causes one login rather than one per reader.
    Website access denial and CSRF failures are protocol errors, not reasons
    to submit the user's password repeatedly.
    """

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._csrf_token: str | None = None
        self._authenticated = False
        self._credentials_rejected = False
        self._auth_generation = 0
        self._auth_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()

    async def async_get_controls(self, station: Station) -> Q3Controls:
        """Read the current native form; no actuator state is inferred."""
        async with self._control_lock:
            return (await self._get_controls_form(station)).controls

    async def _get_controls_form(self, station: Station) -> _Q3Form:
        html = await self._authenticated_request("GET", control_url(station))
        return parse_controls_form(html, station)

    async def async_set_temperature(
        self, station: Station, temperature: float, *, dispatch_guard: Callable[[], bool] | None = None
    ) -> Q3Controls:
        """Change only the native target using fresh form bounds, with no POST retry."""
        async with self._control_lock:
            form = await self._get_controls_form(station)
            value = temperature_value(form, temperature)
            if form.controls.target_temperature == float(value):
                return form.controls
            return await self._set_control(station, form, "SetPointTemp", value, dispatch_guard=dispatch_guard)

    async def async_set_enabled(
        self, station: Station, enabled: bool, *, dispatch_guard: Callable[[], bool] | None = None
    ) -> Q3Controls:
        """Change raw MenuEnableFu; physical run/stop semantics are unverified."""
        if not isinstance(enabled, bool):
            raise ProtocolError("TelePool FU menu control requires a boolean")
        async with self._control_lock:
            form = await self._get_controls_form(station)
            if not form.controls.can_set_enabled:
                raise ProtocolError("TelePool FU menu setting is unavailable")
            if form.controls.menu_enable_fu is enabled:
                return form.controls
            return await self._set_control(
                station, form, "MenuEnableFu", "1" if enabled else "0", dispatch_guard=dispatch_guard
            )

    async def _set_control(
        self,
        station: Station,
        form: _Q3Form,
        name: str,
        value: str,
        *,
        dispatch_guard: Callable[[], bool] | None = None,
    ) -> Q3Controls:
        generation = self._auth_generation
        fields = form.with_setting(name, value)
        # Do not allow a telemetry re-login to replace the form's cookie context
        # between preflight and sending. This lock never performs a login.
        async with self._auth_lock:
            if not self._authenticated or self._auth_generation != generation:
                raise ProtocolError("TelePool session changed after settings preflight")
            if dispatch_guard is not None:
                try:
                    approved = dispatch_guard() is True
                except Exception:
                    raise CommandNotSent(
                        "TelePool command was not sent because its dispatch guard could not approve it"
                    ) from None
                if not approved:
                    raise CommandNotSent("TelePool command was not sent because its dispatch conditions changed")
            # No await separates the final synchronous guard from dispatch.
            await self._post_controls_once(station, form.action, fields)
        try:
            observed = (await self._get_controls_form(station)).controls
        except AuthenticationError, ConnectionError, ProtocolError:
            raise WriteOutcomeUnknown("TelePool settings were submitted but readback could not be confirmed") from None
        matches = (
            observed.target_temperature == float(value)
            if name == "SetPointTemp"
            else observed.menu_enable_fu is (value == "1")
        )
        if not matches:
            raise WriteOutcomeUnknown("TelePool settings may be queued; the requested value is not confirmed yet")
        return observed

    async def _post_controls_once(self, station: Station, action: str, fields: tuple[tuple[str, str], ...]) -> None:
        """One form POST, without redirect following, login recovery or replay."""
        try:
            async with self._session.request(
                "POST",
                action,
                data=fields,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 401:
                    self._authenticated = False
                    raise AuthenticationError("TelePool session expired before the settings submission")
                if response.status in (400, 403):
                    invalid_fields: tuple[str, ...] = ()
                    if response.content_length is None or response.content_length <= _MAX_RESPONSE_LENGTH:
                        try:
                            rejection_html = await response.text()
                            if len(rejection_html) <= _MAX_RESPONSE_LENGTH:
                                invalid_fields = validation_error_fields(rejection_html)
                        except aiohttp.ClientError, asyncio.TimeoutError, OSError, UnicodeDecodeError:
                            pass  # Rejection is known even if its body cannot be read.
                    raise SettingsRejected(f"http_{response.status}", invalid_fields=invalid_fields)
                if response.status in (302, 303):
                    location = response.headers.get("Location")
                    if not location:
                        raise WriteOutcomeUnknown("TelePool returned an unrecognized settings response")
                    destination = urljoin(action, location)
                    target = urlsplit(destination)
                    if target.scheme != "https" or target.netloc.lower() not in ("telepool.ru", "telepool.ru:443"):
                        raise WriteOutcomeUnknown("TelePool settings response redirected outside its HTTPS origin")
                    if target.path.rstrip("/").lower() == "/account/login":
                        self._authenticated = False
                        raise AuthenticationError("TelePool session expired before the settings submission")
                    if target.path.rstrip("/").lower() == "/account/accessdenied":
                        raise SettingsRejected("access_denied")
                    if target.path not in ("/", f"/Control/Q3/Index/{station.id}"):
                        raise WriteOutcomeUnknown("TelePool returned an unrecognized settings destination")
                    return
                if response.status != 200:
                    raise WriteOutcomeUnknown("TelePool returned an unrecognized settings response")
                if response.content_length is not None and response.content_length > _MAX_RESPONSE_LENGTH:
                    raise WriteOutcomeUnknown("TelePool settings response exceeded the supported size")
                html = await response.text()
                if len(html) > _MAX_RESPONSE_LENGTH:
                    raise WriteOutcomeUnknown("TelePool settings response exceeded the supported size")
                if is_login_page(html):
                    self._authenticated = False
                    raise AuthenticationError("TelePool session expired before the settings submission")
                if has_form_errors(html):
                    invalid_fields = validation_error_fields(html)
                    if not invalid_fields:
                        # A site may put a queued/success notice into the same
                        # summary CSS class. No identified invalid setting means
                        # we cannot prove rejection or safely replay this POST.
                        raise WriteOutcomeUnknown(
                            "TelePool returned an unclassified settings notice; do not resend automatically"
                        )
                    raise SettingsRejected("validation_error", invalid_fields=invalid_fields)
                try:
                    parse_controls_form(html, station)
                except ProtocolError:
                    raise WriteOutcomeUnknown("TelePool returned an unrecognized settings response") from None
        except aiohttp.ClientError, asyncio.TimeoutError, OSError, UnicodeDecodeError:
            raise WriteOutcomeUnknown(
                "TelePool settings submission outcome is unknown; do not resend automatically"
            ) from None

    async def async_discover(self) -> list[Station]:
        """Discover own Q3 stations and obtain their channel masks."""
        html = await self._authenticated_request("GET", "/")
        self._csrf_token = extract_csrf_token(html)
        stations = parse_dashboard(html)
        if not stations:
            return []
        face = parse_face_data(await self._post_json("/Home/FaceData", self._devices_form(stations)))
        result: list[Station] = []
        for station in stations:
            status = face.get(station.id)
            if status is None or status.screen_type is None:
                raise ProtocolError("TelePool did not return a discovered Q3's screen mask")
            result.append(replace(station, screen_type=status.screen_type))
        return result

    async def async_fetch(self, station: Station) -> Snapshot:
        """Fetch read-only status and chart data; no screen HTML date is used."""
        face = parse_face_data(await self._post_json("/Home/FaceData", self._devices_form([station])))
        status = face.get(station.id)
        if status is None:
            raise ProtocolError("TelePool did not return the requested Q3's status")
        chart = await self._post_json(
            "/Home/ChartQ3",
            {
                "num": station.id,
                "datetime": "",
                "span": "30",
                "compress": "0",
            },
        )
        return parse_snapshot(station, chart, status)

    @staticmethod
    def _devices_form(stations: list[Station]) -> dict[str, str]:
        result: dict[str, str] = {}
        for index, station in enumerate(stations):
            result[f"devices[{index}][Id]"] = station.id
            result[f"devices[{index}][Type]"] = str(Q3_DEVICE_TYPE)
        return result

    async def _ensure_authenticated(self) -> None:
        if self._credentials_rejected:
            raise AuthenticationError("TelePool did not accept the supplied credentials")
        if self._authenticated:
            return
        async with self._auth_lock:
            if self._credentials_rejected:
                raise AuthenticationError("TelePool did not accept the supplied credentials")
            if not self._authenticated:
                await self._login()

    async def _reauthenticate(self, generation: int) -> None:
        async with self._auth_lock:
            if self._credentials_rejected:
                raise AuthenticationError("TelePool did not accept the supplied credentials")
            if self._auth_generation == generation:
                self._authenticated = False
                await self._login()

    async def _login(self) -> None:
        try:
            login_html = await self._send("GET", "/Account/Login", login=True)
            token = extract_csrf_token(login_html)
            result = await self._send(
                "POST",
                "/Account/Login",
                data={
                    "Mail": self._username,
                    "Password": self._password,
                    "RememberMe": "false",
                    "__RequestVerificationToken": token,
                },
                login=True,
            )
            if is_login_page(result):
                raise AuthenticationError("TelePool did not accept the supplied credentials")
        except AuthenticationError:
            # A new client with new credentials is required after rejection;
            # queued/periodic readers must not keep submitting the bad password.
            self._credentials_rejected = True
            self._csrf_token = None
            self._authenticated = False
            raise
        self._csrf_token = extract_csrf_token(result)
        self._authenticated = True
        self._auth_generation += 1

    async def _post_json(self, path: str, data: Mapping[str, str]) -> Any:
        text = await self._authenticated_request("POST", path, data=data)
        try:
            return json.loads(text)
        except ValueError, TypeError:
            raise ProtocolError("TelePool returned invalid JSON") from None

    async def _authenticated_request(self, method: str, path: str, *, data: Mapping[str, str] | None = None) -> str:
        await self._ensure_authenticated()
        for attempt in range(2):
            generation = self._auth_generation
            form = dict(data) if data is not None else None
            if method == "POST":
                if not self._csrf_token:
                    raise ProtocolError("TelePool request verification token is unavailable")
                form = {**(form or {}), "__RequestVerificationToken": self._csrf_token}
            try:
                result = await self._send(method, path, data=form)
                if is_login_page(result):
                    raise _SessionExpired
                return result
            except _SessionExpired:
                if attempt:
                    self._authenticated = False
                    raise AuthenticationError("TelePool session could not be restored") from None
                await self._reauthenticate(generation)
        raise AuthenticationError("TelePool session could not be restored")  # pragma: no cover

    @staticmethod
    def _checked_url(url: str, method: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc.lower() not in ("telepool.ru", "telepool.ru:443"):
            raise ProtocolError("TelePool redirected outside its HTTPS origin")
        allowed = _POST_PATHS if method == "POST" else _GET_PATHS
        is_control_read = method == "GET" and re.fullmatch(r"/Control/Q3/Index/[A-Za-z0-9_-]+", parsed.path)
        if parsed.path not in allowed and not is_control_read:
            raise ProtocolError("TelePool redirected to an unsupported endpoint")
        return url

    async def _send(self, method: str, path: str, *, data: Mapping[str, str] | None = None, login: bool = False) -> str:
        url = urljoin(BASE_URL + "/", path)
        try:
            for _ in range(6):
                self._checked_url(url, method)
                async with self._session.request(
                    method,
                    url,
                    data=data,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status in _REDIRECTS:
                        location = response.headers.get("Location")
                        if not location:
                            raise ProtocolError("TelePool returned a redirect without a destination")
                        next_url = urljoin(url, location)
                        target = urlsplit(next_url)
                        # Check the origin before classifying or following redirects.
                        if target.scheme != "https" or target.netloc.lower() not in ("telepool.ru", "telepool.ru:443"):
                            raise ProtocolError("TelePool redirected outside its HTTPS origin")
                        target_path = target.path.rstrip("/").lower()
                        if target_path == "/account/accessdenied":
                            raise ProtocolError("TelePool denied access to this operation")
                        if target_path == "/account/login" and not login:
                            raise _SessionExpired
                        if response.status in (307, 308) and method == "POST" and target.path != urlsplit(url).path:
                            raise ProtocolError("TelePool redirected a form to an unexpected endpoint")
                        if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                            method, data = "GET", None
                        url = next_url
                        continue
                    if response.status == 401:
                        if login:
                            raise AuthenticationError("TelePool did not accept the supplied credentials")
                        raise _SessionExpired
                    if response.status == 403:
                        raise ProtocolError("TelePool denied access to this operation")
                    if response.status >= 500 or response.status in (408, 429):
                        raise ConnectionError("TelePool is temporarily unavailable")
                    if response.status != 200:
                        raise ProtocolError("TelePool returned an unexpected HTTP status")
                    if response.content_length is not None and response.content_length > _MAX_RESPONSE_LENGTH:
                        raise ProtocolError("TelePool response exceeded the supported size")
                    result = await response.text()
                    if len(result) > _MAX_RESPONSE_LENGTH:
                        raise ProtocolError("TelePool response exceeded the supported size")
                    return result
            raise ProtocolError("TelePool returned too many redirects")
        except aiohttp.ClientError, asyncio.TimeoutError, OSError:
            raise ConnectionError("Could not connect to TelePool") from None
        except UnicodeDecodeError:
            raise ProtocolError("TelePool returned invalid response text") from None
