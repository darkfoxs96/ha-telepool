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

    def test_disabled_readonly_and_missing_or_wider_ranges_do_not_enable_temperature(self):
        for attribute, value in (("disabled", ""), ("readonly", ""), ("step", "any"), ("max", "38")):

            def change(soup):
                soup.find(attrs={"name": "SetPointTemp"})[attribute] = value

            controls = parse_controls_form(mutate(form_html(), change), STATION).controls
            self.assertEqual(controls.target_temperature, 25)
            self.assertFalse(controls.can_set_temperature)

        def remove_bound(soup):
            del soup.find(attrs={"name": "SetPointTemp"})["min"]

        controls = parse_controls_form(mutate(form_html(), remove_bound), STATION).controls
        self.assertIsNone(controls.min_temperature)
        self.assertFalse(controls.can_set_temperature)

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
        for requested in (True, float("nan"), float("inf"), 4.5, 35.5, 38, 27.15):
            with self.subTest(requested=requested):
                self.session.add("GET", PATH, Response(form_html()))
                with self.assertRaises(ProtocolError):
                    await self.client.async_set_temperature(STATION, requested)
        self.assertEqual(self.posts(), [])

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
