"""Config and options flows for a single TelePool station."""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

import aiohttp
import probatio as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.entity import entity_sources

from .client import (
    AuthenticationError,
    ProtocolError,
    Station,
    TelePoolClient,
)
from .client import (
    ConnectionError as TelePoolConnectionError,
)
from .const import (
    CONTROL_MODES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_STALE_AFTER,
    DEFAULT_STARTUP_GRACE,
    DOMAIN,
    get_control_mode,
)
from .credential_store import CredentialStoreError, async_encrypt_credentials

CONF_STATION = "station"
DEFAULT_OPTIONS: dict[str, Any] = {
    "poll_interval": DEFAULT_POLL_INTERVAL,
    "stale_after": DEFAULT_STALE_AFTER,
    "startup_grace": DEFAULT_STARTUP_GRACE,
    "export_aliases": False,
    "climate_power_linked": False,
    "thermostat_mode": "native_q3",
    "power_mode": "native_q3",
}
_INTERVALS = {
    "poll_interval": (60, 3600),
    "stale_after": (120, 7200),
    "startup_grace": (0, 900),
}
_SOURCES = {
    "climate_source": ("climate", "climate_registry_id", "thermostat_mode"),
    "power_source": ("switch", "power_registry_id", "power_mode"),
}


def _credentials_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): selector.TextSelector({"autocomplete": "username"}),
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                {
                    "type": selector.TextSelectorType.PASSWORD,
                    "autocomplete": "current-password",
                }
            ),
        }
    )


def _safe_title(station: Station, username: str, password: str) -> str:
    """Use a device label, never a credential, as the persisted entry title."""
    title = " ".join(station.name.split())[:128]
    if not title or username.casefold() in title.casefold() or password in title:
        return "TelePool Q3"
    return title


class TelePoolConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Authenticate, discover and select an existing Q3 station."""

    VERSION = 1

    def __init__(self) -> None:
        self._stations: dict[str, Station] = {}
        self._encrypted_credentials: str | None = None
        self._titles: dict[str, str] = {}
        self._labels: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TelePoolOptionsFlow:
        """Offer options without keeping credentials in options."""
        return TelePoolOptionsFlow()

    async def _async_discover(self, username: str, password: str) -> list[Station]:
        # Use an isolated cookie jar, so a flow cannot alter another account's
        # runtime session. Home Assistant's verified transport defaults apply.
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            async with asyncio.timeout(45):
                client = TelePoolClient(session, username, password)
                return await client.async_discover()
        finally:
            # The connector belongs to HA; detach this private cookie session
            # without closing transport used by unrelated integrations.
            session.detach()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Accept credentials, then select a station from the real account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = str(user_input.get(CONF_USERNAME, "")).strip()
            password = user_input.get(CONF_PASSWORD, "")
            if not username or not isinstance(password, str) or not password:
                errors["base"] = "invalid_auth"
            else:
                try:
                    stations = await self._async_discover(username, password)
                    if not stations:
                        return self.async_abort(reason="no_stations")
                    configured = {entry.unique_id for entry in self._async_current_entries()}
                    self._stations = {
                        str(station.id): station for station in stations if str(station.id) not in configured
                    }
                    if not self._stations:
                        return self.async_abort(reason="already_configured")
                    self._titles = {
                        station_id: _safe_title(station, username, password)
                        for station_id, station in self._stations.items()
                    }
                    self._labels = {
                        station_id: _safe_title(
                            replace(
                                station,
                                name=(f"{station.pool_name} — {station.name} ({station_id[-6:]})"),
                            ),
                            username,
                            password,
                        )
                        for station_id, station in self._stations.items()
                    }
                    self._encrypted_credentials = await async_encrypt_credentials(self.hass, username, password)
                    return await self.async_step_station()
                except AuthenticationError:
                    errors["base"] = "invalid_auth"
                except TelePoolConnectionError, aiohttp.ClientError, TimeoutError:
                    errors["base"] = "cannot_connect"
                except ProtocolError:
                    errors["base"] = "invalid_response"
                except CredentialStoreError:
                    errors["base"] = "encryption_error"
        return self.async_show_form(step_id="user", data_schema=_credentials_schema(), errors=errors)

    async def async_step_station(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Create a separate entry with stable identity for each station."""
        errors: dict[str, str] = {}
        if not self._stations or not self._encrypted_credentials:
            return self.async_abort(reason="no_stations")
        if user_input is not None:
            station_id = str(user_input.get(CONF_STATION, ""))
            if station_id not in self._stations:
                errors[CONF_STATION] = "invalid_station"
            else:
                station = self._stations[station_id]
                await self.async_set_unique_id(station_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._titles[station_id],
                    data={
                        "credentials": self._encrypted_credentials,
                        "station": asdict(station),
                    },
                    options=DEFAULT_OPTIONS | {"native_cache_generation": uuid4().hex},
                )
        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATION): selector.SelectSelector(
                        {
                            "options": [
                                {
                                    "value": station_id,
                                    "label": self._labels[station_id],
                                }
                                for station_id in self._stations
                            ],
                            "mode": selector.SelectSelectorMode.DROPDOWN,
                        }
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> config_entries.ConfigFlowResult:
        """Ask for fresh credentials, without exposing the saved password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate access to the same station and replace encrypted access."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = str(user_input.get(CONF_USERNAME, "")).strip()
            password = user_input.get(CONF_PASSWORD, "")
            if not username or not isinstance(password, str) or not password:
                errors["base"] = "invalid_auth"
            else:
                try:
                    stations = await self._async_discover(username, password)
                    entry = self._get_reauth_entry()
                    station = next(
                        (station for station in stations if str(station.id) == entry.unique_id),
                        None,
                    )
                    if station is None:
                        errors["base"] = "station_not_found"
                    else:
                        ciphertext = await async_encrypt_credentials(self.hass, username, password)
                        updates = {
                            "credentials": ciphertext,
                            "station": asdict(station),
                        }
                        if entry.update_listeners:
                            return self.async_update_and_abort(entry, data_updates=updates)
                        return self.async_update_reload_and_abort(entry, data_updates=updates)
                except AuthenticationError:
                    errors["base"] = "invalid_auth"
                except TelePoolConnectionError, aiohttp.ClientError, TimeoutError:
                    errors["base"] = "cannot_connect"
                except ProtocolError:
                    errors["base"] = "invalid_response"
                except CredentialStoreError:
                    errors["base"] = "encryption_error"
        return self.async_show_form(step_id="reauth_confirm", data_schema=_credentials_schema(), errors=errors)


class TelePoolOptionsFlow(config_entries.OptionsFlow):
    """Select native or external controls without changing devices in the flow."""

    def _schema(self, suggested: dict[str, Any]) -> vol.Schema:
        registry = er.async_get(self.hass)
        excluded = [entry.entity_id for entry in registry.entities.values() if entry.platform == DOMAIN]
        fields: dict[Any, Any] = {}
        for key, (minimum, maximum) in _INTERVALS.items():
            fields[vol.Required(key, default=suggested[key])] = selector.NumberSelector(
                {
                    "min": minimum,
                    "max": maximum,
                    "step": 1,
                    "mode": selector.NumberSelectorMode.BOX,
                    "unit_of_measurement": "s",
                }
            )
        for key, (domain, _, mode_key) in _SOURCES.items():
            fields[vol.Required(mode_key, default=suggested[mode_key])] = selector.SelectSelector(
                {
                    "options": list(CONTROL_MODES),
                    "translation_key": mode_key,
                    "mode": selector.SelectSelectorMode.DROPDOWN,
                }
            )
            fields[
                vol.Optional(
                    key,
                    description={"suggested_value": suggested.get(key)},
                )
            ] = selector.EntitySelector({"filter": {"domain": domain}, "exclude_entities": excluded})
        fields[vol.Required("export_aliases", default=suggested["export_aliases"])] = selector.BooleanSelector()
        fields[vol.Required("climate_power_linked", default=suggested["climate_power_linked"])] = (
            selector.BooleanSelector()
        )
        return vol.Schema(fields)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Validate sources independently of their current availability."""
        registry = er.async_get(self.hass)
        saved_options = dict(self.config_entry.options)
        suggested = DEFAULT_OPTIONS | saved_options
        for kind in ("thermostat", "power"):
            suggested[f"{kind}_mode"] = get_control_mode(saved_options, kind)
        for key, (_, registry_key, mode_key) in _SOURCES.items():
            if suggested[mode_key] != "external":
                suggested.pop(key, None)
                suggested.pop(registry_key, None)
                continue
            if registry_id := suggested.get(registry_key):
                if entity_id := er.async_resolve_entity_id(registry, registry_id):
                    suggested[key] = entity_id
                else:
                    # A different device may have reused the old entity_id.
                    suggested.pop(key, None)
        errors: dict[str, str] = {}
        if user_input is not None:
            options: dict[str, Any] = {}
            for key, (minimum, maximum) in _INTERVALS.items():
                try:
                    raw = user_input.get(key, suggested[key])
                    value = int(raw)
                    if isinstance(raw, bool) or value != float(raw):
                        raise ValueError
                    if not minimum <= value <= maximum:
                        raise ValueError
                    options[key] = value
                except ValueError, TypeError, OverflowError:
                    errors[key] = "invalid_interval"
            aliases = user_input.get("export_aliases", False)
            if not isinstance(aliases, bool):
                errors["export_aliases"] = "invalid_option"
            else:
                options["export_aliases"] = aliases
            power_linked = user_input.get("climate_power_linked", False)
            if not isinstance(power_linked, bool):
                errors["climate_power_linked"] = "invalid_option"
            else:
                options["climate_power_linked"] = power_linked

            for kind in ("thermostat", "power"):
                mode_key = f"{kind}_mode"
                mode = user_input.get(mode_key, suggested[mode_key])
                if not isinstance(mode, str) or mode not in CONTROL_MODES:
                    errors[mode_key] = "invalid_option"
                else:
                    options[mode_key] = mode

            sources = entity_sources(self.hass)
            for key, (domain, registry_key, mode_key) in _SOURCES.items():
                # Inactive selections must not survive a mode change, even if
                # the browser submits a source left over from the same form.
                if options.get(mode_key) != "external":
                    continue
                selected = user_input.get(key)
                if selected in (None, ""):
                    errors[key] = "external_source_required"
                    continue
                if not isinstance(selected, str):
                    errors[key] = "invalid_source"
                    continue
                entity_id = er.async_resolve_entity_id(registry, selected)
                if not entity_id or entity_id.partition(".")[0] != domain:
                    errors[key] = "invalid_source"
                    continue
                source_entry = registry.async_get(entity_id)
                if source_entry is None and self.hass.states.get(entity_id) is None:
                    errors[key] = "invalid_source"
                    continue
                if (source_entry is not None and source_entry.platform == DOMAIN) or sources.get(entity_id, {}).get(
                    "domain"
                ) == DOMAIN:
                    errors[key] = "telepool_source"
                    continue
                options[key] = entity_id
                if source_entry is not None:
                    options[registry_key] = source_entry.id
            if power_linked is True:
                if (
                    options.get("thermostat_mode") != "external"
                    or options.get("power_mode") != "external"
                    or not all(key in options for key in _SOURCES)
                ):
                    errors["climate_power_linked"] = "power_link_requires_sources"
            if not errors:
                previous_mode = get_control_mode(saved_options, "thermostat")
                requested_mode = options["thermostat_mode"]
                generation = saved_options.get("native_cache_generation")
                if previous_mode != requested_mode and "native_q3" in (previous_mode, requested_mode):
                    # A later return to native mode must not revive a queued
                    # target from a previously selected backend lifetime.
                    options["native_cache_generation"] = uuid4().hex
                elif isinstance(generation, str) and re.fullmatch(r"[a-f0-9]{32}", generation):
                    options["native_cache_generation"] = generation
                return self.async_create_entry(title="", data=options)
            suggested |= user_input
        return self.async_show_form(step_id="init", data_schema=self._schema(suggested), errors=errors)
