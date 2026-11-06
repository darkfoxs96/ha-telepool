"""Parse the limited, observed TelePool Q3 website protocol.

The server's timestamps have no time zone. Europe/Moscow is an explicit
initial assumption based on ClockGmt=3 and the observed account; it is not
a documented rule for all TelePool installations. Explicit offsets, when
provided, are preserved. The HTML screen's date is deliberately not used:
the real fixture has a one-day discrepancy with the JSON timestamps.
"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .exceptions import ProtocolError
from .models import Number, Snapshot, Station

ASSUMED_TIME_ZONE = ZoneInfo("Europe/Moscow")
Q3_DEVICE_TYPE = 9
CHANNEL_FU = 1
CHANNEL_RX = 2
CHANNEL_PH = 4
CHANNEL_FCL = 8


@dataclass(frozen=True, slots=True)
class FaceData:
    """The subset of one FaceData entry used by this client."""

    station_id: str
    screen_type: int | None
    stale: bool
    error: bool
    quality: int | None


def parse_datetime(value: object) -> datetime | None:
    """Parse JSON or cabinet timestamps, applying the documented assumption."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"):
            try:
                parsed = datetime.strptime(value, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ASSUMED_TIME_ZONE)
    return parsed


def _number(value: object) -> Number | None:
    """Reject booleans, non-numbers and infinities instead of inventing zero."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace("\xa0", "").replace(",", ".")
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _boolean(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, "0", "false", "False"):
        return False
    if value in (1, "1", "true", "True"):
        return True
    return default


def _identifier(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or isinstance(value, str):
        result = str(value).strip()
        if result and re.fullmatch(r"[A-Za-z0-9_-]+", result):
            return result
    return None


def extract_csrf_token(html: str) -> str:
    """Extract the antiforgery token without retaining other account HTML."""
    token = BeautifulSoup(html, "html.parser").find("input", attrs={"name": "__RequestVerificationToken"})
    if not isinstance(token, Tag) or not isinstance(token.get("value"), str):
        raise ProtocolError("TelePool did not provide a request verification token")
    value = str(token["value"]).strip()
    if not value:
        raise ProtocolError("TelePool provided an empty request verification token")
    return value


def is_login_page(html: str) -> bool:
    """Recognize an HTML login response even when it arrived with status 200."""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        if urlsplit(str(form.get("action", ""))).path.rstrip("/").lower() == "/account/login":
            return True
    return soup.find("input", attrs={"name": "Password"}) is not None


def _json_attribute(value: object) -> Any:
    if not isinstance(value, str):
        raise ProtocolError("TelePool dashboard is missing Q3 metadata")
    try:
        return json.loads(value)
    except ValueError, TypeError:
        raise ProtocolError("TelePool dashboard contains invalid Q3 metadata") from None


def _pool_container(display: Tag) -> tuple[Tag | None, str]:
    for parent in display.parents:
        if isinstance(parent, Tag):
            match = re.fullmatch(r"(?:collapse_pool|accordion)_(.+)", str(parent.get("id", "")))
            if match:
                return parent, match[1]
    return None, ""


def _pool_name(soup: BeautifulSoup, container: Tag | None, pool_id: str) -> str:
    if pool_id:
        # Bootstrap's collapse button may be outside the collapsed body.
        for element in soup.find_all(["button", "a"]):
            targets = (
                element.get("data-bs-target"),
                element.get("data-target"),
                element.get("href"),
                element.get("aria-controls"),
            )
            if any(target in (f"#collapse_pool_{pool_id}", f"collapse_pool_{pool_id}") for target in targets):
                name = element.get_text(" ", strip=True)
                if name:
                    return name
    if container is not None:
        outer = container.find_parent(id=f"accordion_{pool_id}") or container
        heading = outer.find(class_="accordion-header")
        if heading is not None and (name := heading.get_text(" ", strip=True)):
            return name
    return f"Бассейн {pool_id}" if pool_id else "Бассейн"


def parse_dashboard(html: str) -> list[Station]:
    """Discover Q3s in DevicesMy, tying each display to its own pool.

    No JSON device-list endpoint has been observed. Trusted/shared pools are
    deliberately not guessed from undocumented arrays. Name/firmware are
    optional: a stable Q3 label is used when only the observed IDs exist.
    Screen masks are completed by FaceData in ``async_discover``.
    """
    soup = BeautifulSoup(html, "html.parser")
    devices_input = soup.find("input", id="DevicesMy")
    if not isinstance(devices_input, Tag):
        raise ProtocolError("TelePool dashboard device list was not found")
    devices = _json_attribute(devices_input.get("value"))
    if not isinstance(devices, list):
        raise ProtocolError("TelePool dashboard device list has an unexpected format")
    supported: list[str] = []
    for device in devices:
        if not isinstance(device, Mapping):
            raise ProtocolError("TelePool dashboard contains an invalid device")
        if _integer(device.get("Type")) != Q3_DEVICE_TYPE:
            continue
        station_id = _identifier(device.get("Id"))
        if station_id is None:
            raise ProtocolError("TelePool dashboard contains an invalid station identifier")
        if station_id not in supported:
            supported.append(station_id)
    if not supported:
        return []

    context_element = soup.find(id="q3-data-user")
    context = _json_attribute(context_element.get("data-q3-data") if isinstance(context_element, Tag) else None)
    if not isinstance(context, Mapping):
        raise ProtocolError("TelePool dashboard has invalid account routing context")
    context = {str(key).lower(): value for key, value in context.items()}
    user_id = _identifier(context.get("user"))
    if user_id is None:
        raise ProtocolError("TelePool dashboard is missing account routing context")

    stations: list[Station] = []
    for station_id in supported:
        display = soup.find(id=f"q3-display-{station_id}")
        if not isinstance(display, Tag):
            raise ProtocolError("TelePool dashboard is missing a Q3 display")
        container, pool_id = _pool_container(display)
        if not pool_id or _identifier(pool_id) is None:
            raise ProtocolError("TelePool dashboard is missing the Q3 pool association")
        stations.append(
            Station(
                id=station_id,
                pool_id=pool_id,
                pool_name=_pool_name(soup, container, pool_id),
                name=f"TelePool Q3 {station_id}",
                screen_type=0,
                user_id=user_id,
            )
        )
    return stations


def parse_face_data(payload: object) -> dict[str, FaceData]:
    """Parse the observed JSON array; no station-online boolean is inferred."""
    if not isinstance(payload, list):
        raise ProtocolError("TelePool FaceData is not an array")
    result: dict[str, FaceData] = {}
    for entry in payload:
        if not isinstance(entry, Mapping):
            raise ProtocolError("TelePool FaceData contains an invalid entry")
        station_id = _identifier(entry.get("num"))
        if station_id is None:
            raise ProtocolError("TelePool FaceData has no station identifier")
        q3 = entry.get("q3")
        if not isinstance(q3, Mapping):
            continue  # Other device types can be returned in a mixed cabinet.
        screen_type = _integer(q3.get("screenType"))
        if screen_type is not None and not 0 <= screen_type <= 15:
            raise ProtocolError("TelePool returned an unsupported Q3 screen mask")
        quality = _integer(entry.get("quality"))
        result[station_id] = FaceData(
            station_id=station_id,
            screen_type=screen_type,
            stale=_boolean(entry.get("oldDateTime"), default=True),
            error=_boolean(q3.get("error"), default=False),
            quality=quality if quality is not None and 0 <= quality <= 100 else None,
        )
    return result


def parse_snapshot(station: Station, chart: object, face: FaceData) -> Snapshot:
    """Combine the latest dated measurement with its own station's FaceData.

    A live ChartQ3 response may end in an empty minute bucket: ``visit`` is
    populated but all measurement fields are null. Such a bucket must not
    erase real measurements or make their timestamp appear newer.
    """
    if face.station_id != station.id:
        raise ProtocolError("TelePool telemetry belongs to a different station")
    if not isinstance(chart, Mapping) or not isinstance(chart.get("data"), list):
        raise ProtocolError("TelePool ChartQ3 has an unexpected format")
    points = chart["data"]
    if any(not isinstance(point, Mapping) for point in points):
        raise ProtocolError("TelePool ChartQ3 contains an invalid point")
    mask = face.screen_type if face.screen_type is not None else station.screen_type

    def has_measurement(point: Mapping[str, Any]) -> bool:
        for bit, field in (
            (CHANNEL_PH, "calcPh"),
            (CHANNEL_RX, "calcRx"),
            (CHANNEL_FCL, "calcFcl"),
            (CHANNEL_FU, "tempFu"),
        ):
            if mask & bit and (value := _number(point.get(field))) is not None:
                if field != "tempFu" or value != -127:
                    return True
        return False

    dated = [
        (timestamp, point)
        for point in points
        if (timestamp := parse_datetime(point.get("visit"))) is not None and has_measurement(point)
    ]
    observed_at, latest = max(dated, key=lambda item: item[0]) if dated else (None, points[-1] if points else {})
    values: dict[str, Number | None] = dict.fromkeys(("ph", "rx", "temperature", "set_ph", "set_rx"))
    modes: dict[str, int] = {}
    for channel, bit, value_field, target_field, mode_field in (
        ("ph", CHANNEL_PH, "calcPh", "setPh", "modePh"),
        ("rx", CHANNEL_RX, "calcRx", "setRx", "modeRx"),
        ("fcl", CHANNEL_FCL, "calcFcl", "setFcl", "modeFcl"),
    ):
        if mask & bit:
            values[channel] = _number(latest.get(value_field))
            values[f"set_{channel}"] = _number(latest.get(target_field))
            mode = _integer(latest.get(mode_field))
            if mode is not None:
                modes[channel] = mode
    if mask & CHANNEL_FU:
        temperature = _number(latest.get("tempFu"))
        values["temperature"] = None if temperature == -127 else temperature
        for key, field in (("fu", "modeFu"), ("temperature", "modeTemp")):
            if (mode := _integer(latest.get(field))) is not None:
                modes[key] = mode
    quality = _integer(chart.get("quality"))
    return Snapshot(
        station_id=station.id,
        values=values,
        modes=modes,
        observed_at=observed_at,
        received_at=parse_datetime(chart.get("last")),
        stale=face.stale or _boolean(chart.get("old"), default=True) or observed_at is None,
        error=face.error,
        rssi=_integer(latest.get("rssi")),
        quality=quality if quality is not None and 0 <= quality <= 100 else face.quality,
    )
