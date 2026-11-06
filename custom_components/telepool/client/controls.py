"""Parse and round-trip the observed Q3 HTML settings form.

The body is an ordered sequence of successful controls, never a dictionary:
ASP.NET repeats __Invariant and submits checked HourN=true before its hidden
HourN=false fallback. Disabled controls (including WiFiName) are not submitted;
readonly controls are successful under HTML rules. Tokens/full bodies remain
private transport data and must never be attached to HA entities/diagnostics.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .exceptions import ProtocolError
from .models import Q3Controls, Station

_CONTEXT_FIELDS = frozenset(
    (
        "__RequestVerificationToken",
        "Id",
        "Visit",
        "back",
        "btnId",
        "PhPlug",
        "RxPlug",
        "FclPlug",
        "FuPlug",
        "Disable",
        "ModePh",
        "ModeRx",
        "ModeFcl",
        "ModeFu",
        "ModeTemp",
    )
)
_GENERAL_FIELDS = frozenset(("SysPoolType", "SysPoolTent", "SysPoolVolume"))
_PH_FIELDS = frozenset(
    (
        "SetPointPh",
        "SetDeltaPh",
        "DosTypePh",
        "DosEnablePh",
        "DosPriorityPh",
        "DosDayVolumePh",
        "DosVolumePh",
        "DosWaitPh",
        "DosGainPh",
        "DosProtectEnablePh",
        "DosProtectPh",
        "DosProtectWaitPh",
        "SenCorrectPh",
        "SenMinPh",
        "SenMaxPh",
    )
)
_RX_FIELDS = frozenset(name.replace("Ph", "Rx") for name in _PH_FIELDS if name != "DosPriorityPh")
_FU_FIELDS = frozenset(("SetPointTemp", "SetDeltaTemp", "MenuEnableFu", "SysPriorityTemp"))
_KNOWN_FORM_FIELDS = (
    _CONTEXT_FIELDS
    | _GENERAL_FIELDS
    | _PH_FIELDS
    | _RX_FIELDS
    | _FU_FIELDS
    | {f"Hour{hour}" for hour in range(24)}
    | frozenset(
        (
            "__Invariant",
            "ClockGmt",
            "WiFiName",
            "SysPassEnable",
            "SysFlow",
            "SysFlowWait",
            "SysStp",
            "SysStpWait",
            "DosModePh",
            "SysZozEnableTemp",
            "SysZozTemp",
            "SysDcTemp",
            "SysTypeTemp",
            "SenCorrectTemp",
            "PumpPerfLeft",
            "PumpModeLeft",
            "PumpCanTypeLeft",
            "PumpCanWaitLeft",
            "PumpPerfRight",
            "PumpModeRight",
            "PumpCanTypeRight",
            "PumpCanWaitRight",
        )
    )
)
_NATIVE_MIN = Decimal("5")
_NATIVE_MAX = Decimal("35")


@dataclass(frozen=True, slots=True, repr=False)
class _Q3Form:
    controls: Q3Controls
    action: str
    fields: tuple[tuple[str, str], ...]

    def with_setting(self, name: str, value: str) -> tuple[tuple[str, str], ...]:
        """Change one allowlisted value and explicitly retain the ordinary command."""
        if name not in ("SetPointTemp", "MenuEnableFu"):
            raise ProtocolError("Unsupported TelePool setting")
        if sum(key == name for key, _ in self.fields) != 1:
            raise ProtocolError("TelePool setting is not a unique successful control")
        return tuple((key, value if key == name else "Empty" if key == "btnId" else item) for key, item in self.fields)


def control_url(station: Station) -> str:
    """Build the observed GET URL from validated private routing context."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", station.id) or not station.user_id:
        raise ProtocolError("TelePool station has invalid control routing context")
    return f"https://telepool.ru/Control/Q3/Index/{station.id}?{urlencode({'mypool': 'True', 'user': station.user_id})}"


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation, ValueError:
        return None
    return number if number.is_finite() and (not number or -8 <= number.adjusted() <= 8) else None


def _disabled(element: Tag, form: Tag) -> bool:
    if element.has_attr("disabled"):
        return True
    for parent in element.parents:
        if parent is form:
            break
        if isinstance(parent, Tag) and parent.name == "fieldset" and parent.has_attr("disabled"):
            legend = parent.find("legend", recursive=False)
            # HTML exempts only descendants of the disabled fieldset's first legend.
            if legend is None or legend not in element.parents:
                return True
    return False


def _options(element: Tag) -> Iterable[Tag]:
    for option in element.find_all("option"):
        group = option.find_parent("optgroup")
        if not option.has_attr("disabled") and not (group is not None and group.has_attr("disabled")):
            yield option


def _successful(element: Tag, form: Tag) -> list[tuple[str, str]]:
    name = str(element.get("name", ""))
    if not name or _disabled(element, form):
        return []
    if element.name == "button":
        return []  # The site's confirmation invokes form.submit(), with no submitter.
    if element.name == "select":
        options = list(_options(element))
        selected = [option for option in options if option.has_attr("selected")]
        if not element.has_attr("multiple") and not selected and options:
            selected = options[:1]
        return [(name, str(option.get("value", option.get_text()))) for option in selected]
    if element.name == "textarea":
        value = element.get_text().replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        return [(name, value)]
    kind = str(element.get("type", "text")).lower()
    if kind in ("submit", "button", "reset", "image"):
        return []
    if kind == "file":
        raise ProtocolError("TelePool settings contain an unsupported file control")
    if kind in ("checkbox", "radio") and not element.has_attr("checked"):
        return []
    if kind == "password":
        raise ProtocolError("TelePool settings contain an unsupported sensitive control")
    return [(name, str(element.get("value", "on" if kind in ("checkbox", "radio") else "")))]


def has_form_errors(html: str) -> bool:
    """Recognize ASP.NET validation errors without reporting any response text."""
    soup = BeautifulSoup(html, "html.parser")
    return any(
        node.get_text(" ", strip=True) for node in soup.select(".validation-summary-errors, .field-validation-error")
    )


def validation_error_fields(html: str) -> tuple[str, ...]:
    """Return at most eight known field names, never server validation messages."""
    soup = BeautifulSoup(html, "html.parser")
    names: set[str] = set()
    for node in soup.select(".field-validation-error[data-valmsg-for], .input-validation-error[name]"):
        name = node.get("data-valmsg-for", node.get("name"))
        if isinstance(name, str) and name in _KNOWN_FORM_FIELDS:
            names.add(name)
    for node in soup.select(".validation-summary-errors"):
        message = node.get_text(" ", strip=True)
        for name in _KNOWN_FORM_FIELDS:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", message):
                names.add(name)
    return tuple(sorted(names))[:8]


def parse_controls_form(html: str, station: Station) -> _Q3Form:
    """Read the exact form and reject incomplete/ambiguous binding context."""
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form", id="forModalForm")
    if len(forms) != 1 or has_form_errors(html):
        raise ProtocolError("TelePool did not provide a valid settings form")
    form = forms[0]
    if (
        str(form.get("method", "get")).lower() != "post"
        or str(form.get("enctype", "application/x-www-form-urlencoded")).lower() != "application/x-www-form-urlencoded"
    ):
        raise ProtocolError("TelePool settings use an unsupported submission format")
    action = urljoin(control_url(station), str(form.get("action", "")))
    destination = urlsplit(action)
    query = parse_qs(destination.query, keep_blank_values=True)
    if (
        destination.scheme != "https"
        or destination.netloc.lower() not in ("telepool.ru", "telepool.ru:443")
        or destination.path != f"/Control/Q3/Index/{station.id}"
        or query.get("user") != [station.user_id]
        or set(query) - {"user", "pool", "mypool"}
        or not any(query.get(key) == ["True"] for key in ("pool", "mypool"))
    ):
        raise ProtocolError("TelePool settings form has unexpected routing context")
    elements = [
        element
        for element in soup.find_all(("input", "select", "textarea", "button"))
        if element.get("name")
        and (
            element.get("form") == "forModalForm"
            or (not element.has_attr("form") and element.find_parent("form") is form)
        )
    ]
    by_name: dict[str, list[Tag]] = {}
    for element in elements:
        by_name.setdefault(str(element["name"]), []).append(element)
    if any(len(by_name.get(name, ())) != 1 for name in _CONTEXT_FIELDS):
        raise ProtocolError("TelePool settings are missing unique binding context")
    for name in _CONTEXT_FIELDS:
        element = by_name[name][0]
        if element.name != "input" or element.get("type") != "hidden" or _disabled(element, form):
            raise ProtocolError("TelePool settings have unsupported binding context")
    for name, fields in by_name.items():
        if len(fields) <= 1:
            continue
        if name in (_CONTEXT_FIELDS | _GENERAL_FIELDS | _PH_FIELDS | _RX_FIELDS | _FU_FIELDS):
            raise ProtocolError("TelePool settings contain duplicate critical controls")
        if name == "__Invariant" and all(field.name == "input" and field.get("type") == "hidden" for field in fields):
            continue
        kinds = [field.get("type") for field in fields]
        if kinds == ["checkbox", "hidden"] and [str(field.get("value", "")).lower() for field in fields] == [
            "true",
            "false",
        ]:
            continue
        if all(kind == "radio" for kind in kinds) and sum(field.has_attr("checked") for field in fields) <= 1:
            continue
        raise ProtocolError("TelePool settings contain ambiguous duplicate controls")

    def value(name: str) -> str:
        return str(by_name[name][0].get("value", ""))

    if value("Id") != station.id or not value("__RequestVerificationToken") or value("btnId") != "Empty":
        raise ProtocolError("TelePool settings have invalid station or ordinary-command context")
    flags: dict[str, bool] = {}
    for name in ("PhPlug", "RxPlug", "FclPlug", "FuPlug", "Disable"):
        raw = value(name).lower()
        if raw not in ("true", "false"):
            raise ProtocolError("TelePool settings contain an invalid capability flag")
        flags[name] = raw == "true"
    required = _GENERAL_FIELDS | (_PH_FIELDS if flags["PhPlug"] else set()) | (_RX_FIELDS if flags["RxPlug"] else set())
    if flags["FuPlug"]:
        required |= _FU_FIELDS | {f"Hour{hour}" for hour in range(24)}
    if any(name not in by_name for name in required):
        raise ProtocolError("TelePool settings form is incomplete")
    # Disabled Wi-Fi is deliberately excluded; an unexpected editable/read-only
    # Wi-Fi field would require a new protocol review, not silent forwarding.
    if any(_successful(field, form) for field in by_name.get("WiFiName", ())):
        raise ProtocolError("TelePool settings unexpectedly expose sensitive network configuration")
    pairs = tuple(pair for element in elements for pair in _successful(element, form))
    if any(name not in _KNOWN_FORM_FIELDS for name, _ in pairs):
        raise ProtocolError("TelePool settings contain an unknown successful control")
    counts = Counter(name for name, _ in pairs)
    if any(
        count > 1 and name != "__Invariant" and not re.fullmatch(r"Hour(?:[0-9]|1[0-9]|2[0-3])", name)
        for name, count in counts.items()
    ):
        raise ProtocolError("TelePool settings contain duplicate successful critical values")
    target_fields = by_name.get("SetPointTemp", ())
    target = minimum = maximum = step = None
    target_editable = False
    if len(target_fields) == 1:
        field = target_fields[0]
        if field.name == "input" and str(field.get("type", "")).lower() == "number":
            target = _decimal(field.get("value"))
            minimum, maximum, step = (_decimal(field.get(name)) for name in ("min", "max", "step"))
            target_editable = (
                not _disabled(field, form) and not field.has_attr("readonly") and counts["SetPointTemp"] == 1
            )
    menu = None
    menu_editable = False
    menu_fields = by_name.get("MenuEnableFu", ())
    if len(menu_fields) == 1 and menu_fields[0].name == "select":
        field = menu_fields[0]
        options = list(_options(field))
        selected = [option for option in options if option.has_attr("selected")]
        if sorted(str(option.get("value", "")) for option in options) != ["0", "1"] or len(selected) != 1:
            raise ProtocolError("TelePool FU menu setting has unknown choices")
        menu = str(selected[0]["value"]) == "1"
        menu_editable = not _disabled(field, form) and not field.has_attr("readonly") and counts["MenuEnableFu"] == 1
    valid_range = (
        minimum is not None
        and maximum is not None
        and step is not None
        and _NATIVE_MIN <= minimum <= maximum <= _NATIVE_MAX
        and step > 0
    )
    valid_target = target is not None and _NATIVE_MIN <= target <= _NATIVE_MAX
    controls = Q3Controls(
        station_id=station.id,
        target_temperature=float(target) if valid_target else None,
        menu_enable_fu=menu,
        min_temperature=float(minimum) if minimum is not None else None,
        max_temperature=float(maximum) if maximum is not None else None,
        temperature_step=float(step) if step is not None else None,
        can_set_temperature=bool(
            flags["FuPlug"] and not flags["Disable"] and target_editable and valid_range and valid_target
        ),
        can_set_enabled=bool(flags["FuPlug"] and not flags["Disable"] and menu_editable and menu is not None),
    )
    return _Q3Form(controls, action, pairs)


def temperature_value(form: _Q3Form, requested: object) -> str:
    """Validate fresh live bounds and step without rounding or applying proxy caps."""
    controls = form.controls
    value = _decimal(requested)
    minimum, maximum, step = (
        _decimal(item) for item in (controls.min_temperature, controls.max_temperature, controls.temperature_step)
    )
    if not controls.can_set_temperature or value is None or minimum is None or maximum is None or step is None:
        raise ProtocolError("TelePool temperature setting is unavailable")
    if not _NATIVE_MIN <= minimum <= value <= maximum <= _NATIVE_MAX or (value - minimum) % step:
        raise ProtocolError("TelePool temperature must match the current form bounds and step")
    return format(value, "f")
