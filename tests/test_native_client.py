"""Native settings transport tests; every write is synthetic, never a real Q3 POST."""

import asyncio
import html
import json
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from custom_components.telepool.client import (
    AuthenticationError,
    CommandNotSent,
    ProtocolError,
    SettingsRejected,
    Station,
    TelePoolClient,
    TemperatureRangeRejected,
    WriteOutcomeUnknown,
)
from custom_components.telepool.client.controls import parse_controls_form
from tests.test_client import STATION_ID, USER_ID, Response, Session

PATH = f"/Control/Q3/Index/{STATION_ID}"
STATION = Station(STATION_ID, "pool-a", "Бассейн", "Q3", 7, user_id=USER_ID)
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/q3-controls-schema.json").read_text())


def form_html(target="25.0", enabled="1", token="native-csrf"):
    """Synthetic complete form built from the public observed field schema."""
    nodes = []
    hidden_checkboxes = []
    for field in SCHEMA["fields"]:
        name, kind = field["name"], field["type"]
        disabled = " disabled" if field["disabled_in_html"] else ""
        readonly = " readonly" if field["readonly"] else ""
        if kind == "select":
            selected = enabled if name == "MenuEnableFu" else field["options"][0]["value"]
            options = "".join(
                f'<option value="{option["value"]}"{" selected" if option["value"] == selected else ""}>{html.escape(option["label"])}</option>'
                for option in field["options"]
            )
            nodes.append(f'<select name="{name}"{disabled}{readonly}>{options}</select>')
        elif kind == "checkbox":
            checked = " checked" if int(name.removeprefix("Hour")) % 2 == 0 else ""
            nodes.append(f'<input name="{name}" type="checkbox" value="true"{checked}{disabled}>')
            hidden_checkboxes.append(f'<input name="{name}" type="hidden" value="false">')
        else:
            value = (
                target
                if name == "SetPointTemp"
                else "synthetic-private-wifi"
                if name == "WiFiName"
                else "9.99"
                if name == "SenMaxPh"
                else field.get("min", "0")
            )
            bounds = " ".join(f'{key}="{field[key]}"' for key in ("min", "max", "step") if key in field)
            nodes.append(f'<input name="{name}" type="{kind}" value="{value}" {bounds}{disabled}{readonly}>')
            if kind == "number":
                nodes.append(f'<input name="__Invariant" type="hidden" value="{name}">')
    context = {
        "Id": STATION_ID,
        "Visit": "2026-01-02T10:10:00",
        "back": "/",
        "btnId": "Empty",
        "PhPlug": "True",
        "RxPlug": "True",
        "FclPlug": "False",
        "FuPlug": "True",
        "Disable": "False",
        "ModePh": "NRM",
        "ModeRx": "NRM",
        "ModeFcl": "NSP",
        "ModeFu": "JOB",
        "ModeTemp": "NTS",
        "__RequestVerificationToken": token,
    }
    nodes.extend(
        f'<input name="{key}" type="hidden" value="{html.escape(value, quote=True)}">' for key, value in context.items()
    )
    nodes.extend(hidden_checkboxes)
    return f'<form id="forModalForm" method="post" action="{PATH}?pool=True&amp;user={USER_ID}" novalidate>{"".join(nodes)}<button type="submit">Сохранить</button></form>'


def mutate(source, callback):
    soup = BeautifulSoup(source, "html.parser")
    callback(soup)
    return str(soup)


def range_form(*, target="25.0", minimum="5", maximum="35", step="0.5", errors=(), message=None):
    """Synthetic response and range wording, not a captured server error format."""

    def bounds(soup):
        field = soup.find(attrs={"name": "SetPointTemp"})
        field["min"], field["max"], field["step"] = minimum, maximum, step

    source = mutate(form_html(target=target), bounds)
    messages = "".join(
        f'<span class="field-validation-error" data-valmsg-for="{html.escape(name, quote=True)}">'
        + html.escape(
            message
            if message is not None
            else f"The field SetPointTemp must be between {minimum} and {maximum}."
            if name == "SetPointTemp"
            else "private provider detail"
        )
        + "</span>"
        for name in errors
    )
    return source.replace("</form>", messages + "</form>")


class NativeParserTests(unittest.TestCase):
    def test_complete_successful_form_preserves_order_and_repeated_aspnet_values(self):
        form = parse_controls_form(form_html(), STATION)
        controls = form.controls
        self.assertEqual(controls.target_temperature, 25)
        self.assertEqual((controls.min_temperature, controls.max_temperature, controls.temperature_step), (5, 35, 0.5))
        self.assertTrue(controls.can_set_temperature)
        self.assertTrue(controls.can_set_enabled)
        self.assertTrue(controls.menu_enable_fu)
        counts = Counter(name for name, _ in form.fields)
        self.assertEqual(counts["__Invariant"], 34)
        self.assertEqual([value for name, value in form.fields if name == "Hour0"], ["true", "false"])
        self.assertEqual([value for name, value in form.fields if name == "Hour1"], ["false"])
        self.assertNotIn("WiFiName", counts)
        self.assertNotIn("ClockGmt", counts)
        self.assertNotIn("synthetic-private-wifi", repr(form))
        self.assertNotIn("native-csrf", repr(form))
        changed = form.with_setting("SetPointTemp", "28.5")
        self.assertEqual(
            [(a, b) for a, b in changed if a != "SetPointTemp"], [(a, b) for a, b in form.fields if a != "SetPointTemp"]
        )
        self.assertIn(("SenMaxPh", "9.99"), changed)  # Do not clamp unrelated existing values.

    def test_unknown_successful_controls_are_rejected_even_hidden_or_readonly(self):
        for attrs in ('type="hidden" value="Reboot"', 'type="text" readonly value="Reboot"'):
            source = form_html().replace("</form>", f'<input name="newCommand" {attrs}></form>')
            with self.subTest(attrs=attrs), self.assertRaises(ProtocolError):
                parse_controls_form(source, STATION)
        disabled = form_html().replace("</form>", '<input name="unknownDisabled" disabled value="private"></form>')
        self.assertNotIn("unknownDisabled", dict(parse_controls_form(disabled, STATION).fields))

    def test_duplicate_missing_context_and_changed_action_fail_closed(self):
        cases = [
            form_html().replace("</form>", '<input name="btnId" type="hidden" value="Pump"></form>'),
            form_html().replace('name="SetPointTemp"', 'name="unexpectedTarget"'),
            form_html().replace('value="Empty"', 'value="Pump"'),
            form_html().replace(f'action="{PATH}', 'action="https://attacker.invalid/Control'),
            form_html().replace('name="Id"', 'name="missingId"'),
            form_html().replace("</form>", '<input name="SetPointTemp" type="hidden" value="30"></form>'),
        ]
        for source in cases:
            with self.subTest(source=source[:50]), self.assertRaises(ProtocolError):
                parse_controls_form(source, STATION)

    def test_disabled_readonly_and_invalid_ranges_do_not_enable_temperature(self):
        for attribute, value in (
            ("disabled", ""),
            ("readonly", ""),
            ("min", "36"),
            ("min", "-Infinity"),
            ("max", "NaN"),
            ("step", "any"),
            ("step", "0"),
            ("step", "-0.5"),
        ):

            def change(soup):
                soup.find(attrs={"name": "SetPointTemp"})[attribute] = value

            with self.subTest(attribute=attribute, value=value):
                controls = parse_controls_form(mutate(form_html(), change), STATION).controls
                self.assertEqual(controls.target_temperature, 25)
                self.assertFalse(controls.can_set_temperature)

        for attribute in ("min", "max", "step"):

            def remove_bound(soup):
                del soup.find(attrs={"name": "SetPointTemp"})[attribute]

            with self.subTest(missing=attribute):
                controls = parse_controls_form(mutate(form_html(), remove_bound), STATION).controls
                self.assertFalse(controls.can_set_temperature)

    def test_temperature_capability_uses_live_form_bounds_without_fixed_envelope(self):
        for minimum, maximum, target in (("5", "40", "38"), ("0", "40", "0"), ("5", "5", "5")):

            def change(soup):
                field = soup.find(attrs={"name": "SetPointTemp"})
                field["min"], field["max"] = minimum, maximum

            with self.subTest(minimum=minimum, maximum=maximum, target=target):
                controls = parse_controls_form(mutate(form_html(target=target), change), STATION).controls
                self.assertEqual(controls.target_temperature, float(target))
                self.assertEqual((controls.min_temperature, controls.max_temperature), (float(minimum), float(maximum)))
                self.assertTrue(controls.can_set_temperature)

    def test_unknown_menu_choices_and_enabled_wifi_fail_closed(self):
        def unknown_menu(soup):
            soup.find(attrs={"name": "MenuEnableFu"}).find("option")["value"] = "2"

        with self.assertRaises(ProtocolError):
            parse_controls_form(mutate(form_html(), unknown_menu), STATION)

        def enabled_wifi(soup):
            node = soup.find(attrs={"name": "WiFiName"})
            del node["disabled"]
            node["readonly"] = ""

        with self.assertRaises(ProtocolError):
            parse_controls_form(mutate(form_html(), enabled_wifi), STATION)

    def test_temperature_rejection_metadata_rejects_invalid_numbers(self):
        for bounds in ((5, 35, 0), (36, 35, 0.5), (True, 35, 0.5), (5, float("inf"), 0.5), (5, 35, float("nan"))):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                TemperatureRangeRejected(*bounds)


class NativeClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = Session()
        self.session.login()
        self.client = TelePoolClient(self.session, "synthetic-login", "synthetic-password")

    def posts(self):
        return [call for call in self.session.calls if call[0] == "POST" and urlsplit(call[1]).path == PATH]

    async def test_get_controls_is_read_only(self):
        self.session.add("GET", PATH, Response(form_html()))
        controls = await self.client.async_get_controls(STATION)
        self.assertEqual(controls.target_temperature, 25)
        self.assertEqual(self.posts(), [])
        query = parse_qs(urlsplit(self.session.calls[-1][1]).query)
        self.assertEqual(query, {"mypool": ["True"], "user": [USER_ID]})

    async def test_set_temperature_uses_fresh_form_and_changes_only_one_setting(self):
        self.session.add(
            "GET",
            PATH,
            Response(form_html(token="old-token")),
            Response(form_html(target="26.0", token="fresh-token")),
            Response(form_html(target="28.5")),
        )
        await self.client.async_get_controls(STATION)
        self.session.add("POST", PATH, Response(status=302, headers={"Location": "/"}))
        result = await self.client.async_set_temperature(STATION, 28.5)
        self.assertEqual(result.target_temperature, 28.5)
        [post] = self.posts()
        self.assertFalse(post[2]["allow_redirects"])
        pairs = post[2]["data"]
        self.assertIn(("__RequestVerificationToken", "fresh-token"), pairs)
        self.assertIn(("SetPointTemp", "28.5"), pairs)
        original = parse_controls_form(form_html(target="26.0", token="fresh-token"), STATION).fields
        self.assertEqual(
            [(key, val) for key, val in pairs if key != "SetPointTemp"],
            [(key, val) for key, val in original if key != "SetPointTemp"],
        )
        self.assertEqual([val for key, val in pairs if key == "btnId"], ["Empty"])

    async def test_set_enabled_only_changes_raw_menu_flag(self):
        self.session.add("GET", PATH, Response(form_html()), Response(form_html(enabled="0")))
        self.session.add("POST", PATH, Response(form_html(enabled="0")))
        result = await self.client.async_set_enabled(STATION, False)
        self.assertFalse(result.menu_enable_fu)
        [post] = self.posts()
        self.assertIn(("MenuEnableFu", "0"), post[2]["data"])
        self.assertIn(("SetPointTemp", "25.0"), post[2]["data"])

    async def test_invalid_temperature_never_reaches_a_control_post(self):
        for requested in (True, float("nan"), float("inf"), 27.15):
            with self.subTest(requested=requested):
                self.session.add("GET", PATH, Response(form_html()))
                with self.assertRaises(ProtocolError):
                    await self.client.async_set_temperature(STATION, requested)
        self.assertEqual(self.posts(), [])

    async def test_server_rejects_38_once_with_its_response_range(self):
        for maximum in ("35", "36"):
            with self.subTest(maximum=maximum):
                session = Session()
                session.login()
                session.add("GET", PATH, Response(form_html()))
                session.add("POST", PATH, Response(range_form(target="38", maximum=maximum, errors=("SetPointTemp",))))
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")

                with self.assertRaises(TemperatureRangeRejected) as raised:
                    await client.async_set_temperature(STATION, 38)

                rejection = raised.exception
                self.assertEqual((rejection.minimum, rejection.maximum, rejection.step), (5, float(maximum), 0.5))
                self.assertEqual(rejection.reason, "validation_error")
                self.assertEqual(rejection.invalid_fields, ("SetPointTemp",))
                self.assertNotIn("private provider detail", str(rejection))
                self.assertFalse(hasattr(rejection, "target_temperature"))
                posts = [data for method, url, data in session.calls if method == "POST" and urlsplit(url).path == PATH]
                self.assertEqual(len(posts), 1)
                self.assertIn(("SetPointTemp", "38"), posts[0]["data"])

    async def test_initial_request_does_not_treat_html_maximum_as_server_rejection(self):
        self.session.add("GET", PATH, Response(form_html()), Response(range_form(target="38")))
        self.session.add("POST", PATH, Response(status=302, headers={"Location": "/"}))
        result = await self.client.async_set_temperature(STATION, 38)
        self.assertEqual(result.target_temperature, 38)
        self.assertEqual(len(self.posts()), 1)

    async def test_server_range_fallback_uses_rejection_bounds_and_grid_over_older_html(self):
        self.session.add("GET", PATH, Response(form_html()), Response(form_html()), Response(form_html(target="35.8")))
        self.session.add(
            "POST",
            PATH,
            Response(range_form(target="38", maximum="36", step="0.7", errors=("SetPointTemp",))),
            Response(status=302, headers={"Location": "/"}),
        )
        with self.assertRaises(TemperatureRangeRejected) as raised:
            await self.client.async_set_temperature(STATION, 38)
        self.assertEqual((raised.exception.maximum, raised.exception.step), (36, 0.7))

        result = await self.client.async_set_temperature(STATION, 35.8, range_rejection=raised.exception)

        self.assertEqual(result.target_temperature, 35.8)
        self.assertEqual(len(self.posts()), 2)
        self.assertIn(("SetPointTemp", "35.8"), self.posts()[1][2]["data"])

    async def test_range_rejection_override_rejects_invalid_metadata_before_post(self):
        for invalid in (SettingsRejected("validation_error"), TemperatureRangeRejected(5, 35, 0.5)):
            if type(invalid) is TemperatureRangeRejected:
                invalid.step = float("nan")
            self.session.add("GET", PATH, Response(form_html()))
            with self.subTest(error_type=type(invalid).__name__), self.assertRaises(ProtocolError):
                await self.client.async_set_temperature(STATION, 35, range_rejection=invalid)
        self.assertEqual(self.posts(), [])

    async def test_only_explicit_matching_range_wording_supplies_range_metadata(self):
        cases = (
            ("The field SetPointTemp must be between 5 and 35.", True),
            ("Значение должно быть в диапазоне от 5 до 35.", True),
            ("Значение должно быть между 5,0 и 35,0.", True),
            ("SetPointTemp cannot be changed while the controller is busy. private provider detail", False),
            ("The field SetPointTemp must be between 5 and 36.", False),
            ("The field SetPointTemp must be between 5 and 35.5.", False),
            ("The field SetPointTemp must be between 5 and 35e0.", False),
            ("The field SetPointTemp must be between 5 and 35 000.", False),
            ("Значение не должно быть между 5 и 35.", False),
            ("must be between 5 and 35; must be between 5 and 36", False),
            ("The field SetPointTemp must be between 5 and 35." + "x" * 512, False),
        )
        for message, typed in cases:
            with self.subTest(case=cases.index((message, typed))):
                session = Session()
                session.login()
                session.add("GET", PATH, Response(form_html()))
                session.add("POST", PATH, Response(range_form(target="38", errors=("SetPointTemp",), message=message)))
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(SettingsRejected) as raised:
                    await client.async_set_temperature(STATION, 38)
                self.assertEqual(type(raised.exception) is TemperatureRangeRejected, typed)
                self.assertNotIn("private provider detail", str(raised.exception))
                self.assertEqual(
                    sum(method == "POST" and urlsplit(url).path == PATH for method, url, _ in session.calls), 1
                )

    async def test_observed_short_russian_range_error_is_typed_for_200_and_400(self):
        """Use the observed message shape with synthetic routing and future bounds."""
        for status in (200, 400):
            for maximum in (35, 36):
                for message in (
                    f"Только от 5 до {maximum}",
                    f"ТОЛЬКО\u00a0ОТ  5\tДО\u00a0{maximum}.",
                    f"Только от 5,0 до {maximum},0.",
                ):
                    with self.subTest(status=status, maximum=maximum, message=message):
                        session = Session()
                        session.login()
                        session.add("GET", PATH, Response(form_html()))
                        session.add(
                            "POST",
                            PATH,
                            Response(
                                range_form(
                                    target="38", maximum=str(maximum), errors=("SetPointTemp",), message=message
                                ),
                                status=status,
                            ),
                        )
                        client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                        with self.assertRaises(TemperatureRangeRejected) as raised:
                            await client.async_set_temperature(STATION, 38)
                        self.assertIs(type(raised.exception), TemperatureRangeRejected)
                        self.assertEqual(
                            (raised.exception.minimum, raised.exception.maximum, raised.exception.step),
                            (5, maximum, 0.5),
                        )
                        self.assertEqual(
                            sum(method == "POST" and urlsplit(url).path == PATH for method, url, _ in session.calls),
                            1,
                        )

    async def test_short_russian_range_requires_exact_message_bounds_and_only_target_error(self):
        cases = (
            ("от 5 до 35", ("SetPointTemp",)),
            ("Не только от 5 до 35", ("SetPointTemp",)),
            ("Не вводите от 5 до 35", ("SetPointTemp",)),
            ("Укажите от 5 до 35", ("SetPointTemp",)),
            ("Только от 5 до 35 после включения", ("SetPointTemp",)),
            ("После включения только от 5 до 35", ("SetPointTemp",)),
            ("Только от 5 до 36", ("SetPointTemp",)),
            ("Только от 4 до 35", ("SetPointTemp",)),
            ("Только от 35 до 5", ("SetPointTemp",)),
            ("Только от 5 до 35 000", ("SetPointTemp",)),
            ("Только от 5 до 35e0", ("SetPointTemp",)),
            ("Только от 5 до 35", ("SetPointTemp", "SenMaxPh")),
            ("Только от 5 до 35", ("SetPointTemp", "unknownField")),
            ("Только от 5 до 35", ("SetPointTemp", "SetPointTemp")),
        )
        for message, errors in cases:
            with self.subTest(message=message, errors=errors):
                session = Session()
                session.login()
                session.add("GET", PATH, Response(form_html()))
                session.add("POST", PATH, Response(range_form(target="38", errors=errors, message=message)))
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(SettingsRejected) as raised:
                    await client.async_set_temperature(STATION, 38)
                self.assertIs(type(raised.exception), SettingsRejected)
                self.assertEqual(
                    sum(method == "POST" and urlsplit(url).path == PATH for method, url, _ in session.calls), 1
                )

    async def test_wider_live_form_allows_38_and_preserves_all_unrelated_fields(self):
        def wider_form(target):
            return mutate(
                form_html(target=target), lambda soup: soup.find(attrs={"name": "SetPointTemp"}).attrs.update(max="40")
            )

        preflight = wider_form("37.5")
        self.session.add("GET", PATH, Response(preflight), Response(wider_form("38")))
        self.session.add("POST", PATH, Response(status=302, headers={"Location": "/"}))
        controls = await self.client.async_set_temperature(STATION, 38)

        self.assertEqual(controls.target_temperature, 38)
        self.assertEqual(controls.max_temperature, 40)
        [post] = self.posts()
        pairs = post[2]["data"]
        self.assertEqual([value for name, value in pairs if name == "SetPointTemp"], ["38"])
        original = parse_controls_form(preflight, STATION).fields
        self.assertEqual(
            [(name, value) for name, value in pairs if name != "SetPointTemp"],
            [(name, value) for name, value in original if name != "SetPointTemp"],
        )

    async def test_strict_fallback_rejects_38_before_post_without_changing_other_fields(self):
        preflight = form_html()
        original = parse_controls_form(preflight, STATION).fields
        self.session.add("GET", PATH, Response(preflight))

        with self.assertRaisesRegex(ProtocolError, "current form bounds and step"):
            await self.client.async_set_temperature(STATION, 38, enforce_form_range=True)

        self.assertEqual(self.posts(), [])
        self.assertEqual(parse_controls_form(preflight, STATION).fields, original)
        self.assertIn(("SenMaxPh", "9.99"), original)

    async def test_only_trustworthy_target_range_errors_have_range_metadata(self):
        responses = (
            range_form(target="38", minimum="36", maximum="35", errors=("SetPointTemp",)),
            range_form(target="38", maximum="NaN", errors=("SetPointTemp",)),
            range_form(target="38", step="0", errors=("SetPointTemp",)),
            range_form(target="38", errors=("SetPointTemp", "SenMaxPh")),
            range_form(target="38", errors=("SetPointTemp", "unknownField")),
            range_form(target="38", errors=("SenMaxPh",)),
            range_form(target="38", maximum="40", errors=("SetPointTemp",)),
            range_form(target="38", errors=("SetPointTemp",)).replace('name="Id"', 'name="missingId"'),
            range_form(target="38", errors=("SetPointTemp",)).replace(
                "</form>", '<input name="unknownCommand" type="hidden" value="private"></form>'
            ),
            range_form(target="38", errors=("SetPointTemp",)).replace(
                f'action="{PATH}', 'action="https://attacker.invalid/Control'
            ),
            range_form(target="38", errors=("SetPointTemp",)).replace(
                'name="SetPointTemp"', 'name="SetPointTemp" data-val-range-max="40"'
            ),
            range_form(target="38", errors=("SetPointTemp",)).replace(
                'name="SetPointTemp"', 'name="SetPointTemp" data-val-range-min="NaN"'
            ),
            form_html().replace(
                "</form>", '<div class="validation-summary-errors">SetPointTemp private provider detail</div></form>'
            ),
        )
        for response in responses:
            with self.subTest(response=responses.index(response)):
                session = Session()
                session.login()
                session.add("GET", PATH, Response(form_html()))
                session.add("POST", PATH, Response(response))
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(SettingsRejected) as raised:
                    await client.async_set_temperature(STATION, 38)
                self.assertNotIsInstance(raised.exception, TemperatureRangeRejected)
                self.assertNotIn("private provider detail", str(raised.exception))
                self.assertEqual(
                    sum(method == "POST" and urlsplit(url).path == PATH for method, url, _ in session.calls), 1
                )

    async def test_http400_requires_validated_range_but_403_never_supplies_fallback_metadata(self):
        for status, expected_type in ((400, TemperatureRangeRejected), (403, SettingsRejected)):
            with self.subTest(status=status):
                session = Session()
                session.login()
                session.add("GET", PATH, Response(form_html()))
                session.add("POST", PATH, Response(range_form(target="38", errors=("SetPointTemp",)), status=status))
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(expected_type) as raised:
                    await client.async_set_temperature(STATION, 38)
                self.assertIs(type(raised.exception), expected_type)
                self.assertEqual(
                    sum(method == "POST" and urlsplit(url).path == PATH for method, url, _ in session.calls), 1
                )

    async def test_menu_write_cannot_produce_temperature_fallback_metadata(self):
        self.session.add("GET", PATH, Response(form_html(target="38")))
        self.session.add("POST", PATH, Response(range_form(target="38", errors=("SetPointTemp",))))
        with self.assertRaises(SettingsRejected) as raised:
            await self.client.async_set_enabled(STATION, False)
        self.assertNotIsInstance(raised.exception, TemperatureRangeRejected)
        self.assertEqual(len(self.posts()), 1)

    async def test_already_equal_setting_does_not_send_post(self):
        self.session.add("GET", PATH, Response(form_html()))
        controls = await self.client.async_set_temperature(STATION, 25)
        self.assertEqual(controls.target_temperature, 25)
        self.assertEqual(self.posts(), [])

    async def test_ambiguous_network_http_and_response_outcomes_are_never_retried(self):
        for response in (
            Response(error=asyncio.TimeoutError("private response")),
            Response(status=503),
            Response(status=307, headers={"Location": PATH}),
            Response(status=302, headers={"Location": "https://attacker.invalid/"}),
            Response("unexpected private response"),
        ):
            with self.subTest(status=response.status):
                session = Session()
                session.login()
                session.add("GET", PATH, Response(form_html()))
                session.add("POST", PATH, response)
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(WriteOutcomeUnknown) as raised:
                    await client.async_set_temperature(STATION, 28.5)
                self.assertNotIn("private response", str(raised.exception))
                self.assertEqual(sum(call[0] == "POST" and urlsplit(call[1]).path == PATH for call in session.calls), 1)

    async def test_rejected_or_expired_write_does_not_automatically_login_and_replay(self):
        for response, error in (
            (Response(status=400), ProtocolError),
            (Response(status=403), ProtocolError),
            (Response(status=401), AuthenticationError),
            (Response(status=302, headers={"Location": "/Account/Login"}), AuthenticationError),
        ):
            with self.subTest(status=response.status):
                session = Session()
                session.login()
                session.add("GET", PATH, Response(form_html()))
                session.add("POST", PATH, response)
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(error):
                    await client.async_set_temperature(STATION, 28.5)
                self.assertEqual(sum(call[0] == "POST" and urlsplit(call[1]).path == PATH for call in session.calls), 1)
                self.assertEqual(
                    sum(call[0] == "POST" and call[1].endswith("/Account/Login") for call in session.calls), 1
                )

    async def test_readback_mismatch_is_uncertain_not_permission_to_retry(self):
        self.session.add("GET", PATH, Response(form_html()), Response(form_html()))
        self.session.add("POST", PATH, Response(status=302, headers={"Location": "/"}))
        with self.assertRaises(WriteOutcomeUnknown):
            await self.client.async_set_temperature(STATION, 28.5)
        self.assertEqual(len(self.posts()), 1)

    async def test_explicit_validation_errors_are_rejected_without_response_text(self):
        self.session.add("GET", PATH, Response(form_html()))
        self.session.add(
            "POST",
            PATH,
            Response(
                form_html().replace(
                    "</form>",
                    '<span class="field-validation-error" data-valmsg-for="SetPointTemp">private provider detail</span></form>',
                )
            ),
        )
        with self.assertRaises(ProtocolError) as raised:
            await self.client.async_set_temperature(STATION, 28.5)
        self.assertNotIsInstance(raised.exception, WriteOutcomeUnknown)
        self.assertNotIn("private provider detail", str(raised.exception))
        self.assertEqual(len(self.posts()), 1)

    async def test_settings_rejections_have_bounded_structured_reasons(self):
        for response, reason in (
            (Response(status=400), "http_400"),
            (Response(status=403), "http_403"),
            (Response(status=302, headers={"Location": "/Account/AccessDenied"}), "access_denied"),
            (
                Response(
                    '<span class="field-validation-error" data-valmsg-for="SetPointTemp">private provider detail</span>'
                ),
                "validation_error",
            ),
        ):
            with self.subTest(reason=reason):
                session = Session()
                session.login()
                session.add("GET", PATH, Response(form_html()))
                session.add("POST", PATH, response)
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                with self.assertRaises(SettingsRejected) as raised:
                    await client.async_set_temperature(STATION, 12)
                self.assertEqual(raised.exception.reason, reason)
                self.assertNotIn("private provider detail", str(raised.exception))
                self.assertEqual(
                    sum(method == "POST" and urlsplit(url).path == PATH for method, url, _ in session.calls), 1
                )

    async def test_validation_fields_include_only_bounded_known_identifiers(self):
        error_html = """
          <div class="validation-summary-errors">SenMaxRx must be in range; synthetic-private-value.</div>
          <span class="field-validation-error" data-valmsg-for="SenMaxPh">synthetic-private-value</span>
          <span class="field-validation-error" data-valmsg-for="synthetic-private-field">do not leak</span>
        """
        self.session.add("GET", PATH, Response(form_html()))
        self.session.add("POST", PATH, Response(error_html))
        with self.assertRaises(SettingsRejected) as raised:
            await self.client.async_set_temperature(STATION, 12)
        self.assertEqual(raised.exception.invalid_fields, ("SenMaxPh", "SenMaxRx"))
        self.assertNotIn("synthetic-private", str(raised.exception))
        direct = SettingsRejected("validation_error", invalid_fields=("SenMaxPh", "synthetic-private-field"))
        self.assertEqual(direct.invalid_fields, ("SenMaxPh",))

    async def test_summary_only_queue_notice_is_unknown_and_never_replayed(self):
        self.session.add("GET", PATH, Response(form_html()))
        self.session.add(
            "POST",
            PATH,
            Response(
                '<div class="validation-summary-errors"><ul><li>Настройки переданы; исполнение может занять 2–10 минут.</li></ul></div>'
            ),
        )
        with self.assertRaises(WriteOutcomeUnknown):
            await self.client.async_set_temperature(STATION, 12)
        self.assertEqual(len(self.posts()), 1)

    async def test_shared_lock_serializes_fresh_get_post_readback_sequences(self):
        self.session.add(
            "GET",
            PATH,
            Response(form_html()),
            Response(form_html(target="28.5")),
            Response(form_html(target="28.5")),
            Response(form_html(target="28.5", enabled="0")),
        )
        self.session.add(
            "POST",
            PATH,
            Response(status=302, headers={"Location": "/"}),
            Response(status=302, headers={"Location": "/"}),
        )
        await asyncio.gather(
            self.client.async_set_temperature(STATION, 28.5), self.client.async_set_enabled(STATION, False)
        )
        calls = [(method, urlsplit(url).path) for method, url, _ in self.session.calls if urlsplit(url).path == PATH]
        self.assertEqual([method for method, _ in calls], ["GET", "POST", "GET", "GET", "POST", "GET"])

    async def test_dispatch_guard_rechecks_changes_during_preflight_for_both_setters(self):
        for setting in ("temperature", "enabled"):
            with self.subTest(setting=setting):
                session = Session()
                session.login()
                client = TelePoolClient(session, "synthetic-login", "synthetic-password")
                approved = True
                entered = asyncio.Event()
                resume = asyncio.Event()

                async def preflight():
                    entered.set()
                    await resume.wait()

                session.add("GET", PATH, Response(form_html(), wait=preflight))
                operation = (
                    client.async_set_temperature(STATION, 28.5, dispatch_guard=lambda: approved)
                    if setting == "temperature"
                    else client.async_set_enabled(STATION, False, dispatch_guard=lambda: approved)
                )
                task = asyncio.create_task(operation)
                await entered.wait()
                approved = False
                resume.set()
                with self.assertRaises(CommandNotSent):
                    await task
                self.assertFalse(
                    any(method == "POST" and urlsplit(url).path == PATH for method, url, _ in session.calls)
                )

    async def test_dispatch_guard_rechecks_after_waiting_for_auth_lock(self):
        await self.client._ensure_authenticated()
        approved = True
        preflight_entered = asyncio.Event()

        async def preflight():
            preflight_entered.set()

        self.session.add("GET", PATH, Response(form_html(), wait=preflight))
        await self.client._auth_lock.acquire()
        task = asyncio.create_task(self.client.async_set_temperature(STATION, 28.5, dispatch_guard=lambda: approved))
        try:
            await preflight_entered.wait()
            self.assertFalse(task.done())
            approved = False
        finally:
            self.client._auth_lock.release()
        with self.assertRaises(CommandNotSent):
            await task
        self.assertEqual(self.posts(), [])

    async def test_guard_exception_also_proves_no_dispatch(self):
        self.session.add("GET", PATH, Response(form_html()))

        def failed_guard():
            raise RuntimeError("private guard details")

        with self.assertRaises(CommandNotSent) as raised:
            await self.client.async_set_temperature(STATION, 28.5, dispatch_guard=failed_guard)
        self.assertNotIn("private guard details", str(raised.exception))
        self.assertEqual(self.posts(), [])
