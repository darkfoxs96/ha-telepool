"""Validate discovery, encrypted setup, reauthentication and source selection."""

from dataclasses import asdict, replace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.telepool.client import (
    AuthenticationError,
    ProtocolError,
    Station,
)
from custom_components.telepool.client import (
    ConnectionError as TelePoolConnectionError,
)
from custom_components.telepool.config_flow import DEFAULT_OPTIONS
from custom_components.telepool.credential_store import (
    async_decrypt_credentials,
    async_encrypt_credentials,
)

STATION = Station(
    id="123456789012345",
    pool_id="42",
    pool_name="Example pool",
    name="Q3",
    screen_type=7,
    firmware="3.3.0",
    user_id="00000000-0000-4000-8000-000000000001",
)
LOGIN = {"username": "tester@example.invalid", "password": "synthetic-secret"}


@pytest.fixture
def discover():
    """No config flow ever contacts the real service."""
    with patch("custom_components.telepool.config_flow.TelePoolClient", autospec=True) as client:
        client.return_value.async_discover = AsyncMock(return_value=[STATION])
        yield client


@pytest.fixture(autouse=True)
def skip_entry_setup():
    """Config-flow tests do not start the telemetry coordinator."""
    with patch("custom_components.telepool.async_setup_entry", return_value=True):
        yield


async def _user_flow(hass):
    return await hass.config_entries.flow.async_init("telepool", context={"source": config_entries.SOURCE_USER})


async def test_setup_selects_station_and_stores_only_ciphertext(hass, discover):
    """Authentication alone does not guess a station; selection creates its entry."""
    result = await _user_flow(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
    assert result["step_id"] == "station"
    session = discover.call_args.args[0]
    assert session.closed
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"station": STATION.id})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Q3"
    entry = result["result"]
    assert entry.unique_id == STATION.id
    assert entry.data["station"] == asdict(STATION)
    assert set(entry.data) == {"station", "credentials"}
    assert LOGIN["username"] not in repr(entry.data)
    assert LOGIN["password"] not in repr(entry.data)
    assert await async_decrypt_credentials(hass, entry.data["credentials"]) == (LOGIN["username"], LOGIN["password"])
    assert entry.options["thermostat_mode"] == entry.options["power_mode"] == "native_q3"
    saved_options = dict(entry.options)
    assert UUID(saved_options.pop("native_cache_generation")).version == 4
    assert saved_options == DEFAULT_OPTIONS


async def test_readding_station_does_not_reuse_native_pending_namespace(hass, discover):
    """Native defaults must not revive a removed entry's old temperature intention."""

    async def add_station():
        result = await _user_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"station": STATION.id})
        return result["result"]

    first = await add_station()
    generation = first.options["native_cache_generation"]
    assert await hass.config_entries.async_remove(first.entry_id)
    second = await add_station()
    assert second.options["thermostat_mode"] == second.options["power_mode"] == "native_q3"
    assert second.options["native_cache_generation"] != generation


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (AuthenticationError("Rejected"), "invalid_auth"),
        (TelePoolConnectionError("Offline"), "cannot_connect"),
        (ProtocolError("Unsupported response"), "invalid_response"),
        (TimeoutError(), "cannot_connect"),
    ],
)
async def test_authentication_errors_are_distinct(hass, discover, exception, error):
    discover.return_value.async_discover.side_effect = exception
    result = await _user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert not hass.config_entries.async_entries("telepool")


async def test_no_stations_and_duplicate_station_are_not_created(hass, discover):
    discover.return_value.async_discover.return_value = []
    result = await _user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
    assert result["reason"] == "no_stations"
    MockConfigEntry(domain="telepool", unique_id=STATION.id).add_to_hass(hass)
    discover.return_value.async_discover.return_value = [STATION]
    result = await _user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
    assert result["reason"] == "already_configured"


async def test_selection_only_allows_discovered_station(hass, discover):
    result = await _user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
    with pytest.raises(InvalidData) as err:
        await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"station": "not-in-this-account"})
    assert "station" in err.value.schema_errors
    assert not hass.config_entries.async_entries("telepool")


async def test_multiple_pools_keep_station_ids_and_labels_distinct(hass, discover):
    """Configured stations are excluded while another pool remains selectable."""
    MockConfigEntry(domain="telepool", unique_id=STATION.id).add_to_hass(hass)
    second = replace(STATION, id="987654321098765", pool_id="43", pool_name="Other pool")
    discover.return_value.async_discover.return_value = [STATION, second]
    result = await _user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
    station_selector = next(iter(result["data_schema"].schema.values()))
    assert station_selector.config["options"] == [{"value": second.id, "label": "Other pool — Q3 (098765)"}]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"station": second.id})
    assert result["result"].unique_id == second.id


async def test_reauth_updates_existing_station_not_a_different_one(hass, discover):
    ciphertext = await async_encrypt_credentials(hass, "old-account", "old-secret")
    entry = MockConfigEntry(
        domain="telepool",
        unique_id=STATION.id,
        data={"credentials": ciphertext, "station": asdict(STATION)},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "telepool",
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    discover.return_value.async_discover.return_value = [replace(STATION, id="other")]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
    assert result["errors"] == {"base": "station_not_found"}
    assert entry.data["credentials"] == ciphertext
    discover.return_value.async_discover.return_value = [STATION]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=LOGIN)
    assert result["reason"] == "reauth_successful"
    assert len(hass.config_entries.async_entries("telepool")) == 1
    assert await async_decrypt_credentials(hass, entry.data["credentials"]) == (LOGIN["username"], LOGIN["password"])


async def test_options_store_external_sources_with_stable_registry_ids(hass):
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    climate = registry.async_get_or_create("climate", "demo", "pool-thermostat")
    power = registry.async_get_or_create("switch", "demo", "pool-power")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=DEFAULT_OPTIONS
        | {
            "thermostat_mode": "external",
            "power_mode": "external",
            "climate_source": climate.entity_id,
            "power_source": power.entity_id,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["climate_source"] == climate.entity_id
    assert entry.options["power_source"] == power.entity_id
    assert entry.options["climate_registry_id"] == climate.id
    assert entry.options["power_registry_id"] == power.id


@pytest.mark.parametrize("source_names", [(), ("climate_source",), ("power_source",)])
async def test_linked_climate_power_requires_both_external_sources(hass, source_names):
    """One relay cannot be linked to a missing or unspecified thermostat."""
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    candidates = {
        "climate_source": registry.async_get_or_create("climate", "demo", "pool-thermostat").entity_id,
        "power_source": registry.async_get_or_create("switch", "demo", "pool-power").entity_id,
    }
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=DEFAULT_OPTIONS
        | {name: candidates[name] for name in source_names}
        | {"thermostat_mode": "external", "power_mode": "external", "climate_power_linked": True},
    )
    assert result["errors"] == {
        **{name: "external_source_required" for name in candidates if name not in source_names},
        "climate_power_linked": "power_link_requires_sources",
    }
    assert not entry.options


async def test_linked_climate_power_option_is_saved_with_both_sources(hass):
    """The shared-power behavior must be explicitly selected by the user."""
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    climate = registry.async_get_or_create("climate", "demo", "pool-thermostat")
    power = registry.async_get_or_create("switch", "demo", "pool-power")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=DEFAULT_OPTIONS
        | {
            "thermostat_mode": "external",
            "power_mode": "external",
            "climate_source": climate.entity_id,
            "power_source": power.entity_id,
            "climate_power_linked": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["climate_power_linked"] is True
    assert entry.options["climate_registry_id"] == climate.id
    assert entry.options["power_registry_id"] == power.id


@pytest.mark.parametrize(("domain", "field"), [("climate", "climate_source"), ("switch", "power_source")])
async def test_options_reject_sources_from_any_telepool_entry(hass, domain, field):
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    other = MockConfigEntry(domain="telepool", unique_id="another-station")
    other.add_to_hass(hass)
    source = er.async_get(hass).async_get_or_create(domain, "telepool", "another-proxy", config_entry=other)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=DEFAULT_OPTIONS
        | {field: source.id, "thermostat_mode" if field == "climate_source" else "power_mode": "external"},
    )
    assert result["errors"] == {field: "telepool_source"}
    assert not entry.options


async def test_options_can_remove_existing_sources(hass):
    entry = MockConfigEntry(
        domain="telepool",
        unique_id=STATION.id,
        options=DEFAULT_OPTIONS
        | {"power_mode": "external", "power_source": "switch.previous", "power_registry_id": "old-registry-id"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input=DEFAULT_OPTIONS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "power_source" not in entry.options
    assert "power_registry_id" not in entry.options


async def test_options_follow_a_source_rename(hass):
    registry = er.async_get(hass)
    source = registry.async_get_or_create("climate", "demo", "thermostat")
    old_entity_id = source.entity_id
    entry = MockConfigEntry(
        domain="telepool",
        unique_id=STATION.id,
        options=DEFAULT_OPTIONS
        | {"thermostat_mode": "external", "climate_source": old_entity_id, "climate_registry_id": source.id},
    )
    entry.add_to_hass(hass)
    source = registry.async_update_entity(old_entity_id, new_entity_id="climate.renamed")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    marker = next(key for key in result["data_schema"].schema if str(key) == "climate_source")
    assert marker.description["suggested_value"] == source.entity_id


@pytest.mark.parametrize(
    ("kind", "domain", "source_key", "registry_key"),
    [
        ("thermostat", "climate", "climate_source", "climate_registry_id"),
        ("power", "switch", "power_source", "power_registry_id"),
    ],
)
async def test_legacy_external_source_is_inferred_without_automatic_changes(
    hass, kind, domain, source_key, registry_key
):
    """Opening old options preserves external controls and does not enable Q3."""
    registry = er.async_get(hass)
    source = registry.async_get_or_create(domain, "demo", "legacy-pool-control")
    saved = {source_key: source.entity_id, registry_key: source.id}
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id, options=saved)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})
    assert defaults[f"{kind}_mode"] == "external"
    assert defaults["power_mode" if kind == "thermostat" else "thermostat_mode"] == "disabled"
    assert dict(entry.options) == saved
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={source_key: source.entity_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[f"{kind}_mode"] == "external"
    assert entry.options[registry_key] == source.id


async def test_legacy_entry_without_sources_keeps_controls_disabled(hass):
    """Neither missing sources nor an empty field opts into native writes."""
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})
    assert defaults["thermostat_mode"] == defaults["power_mode"] == "disabled"
    assert not entry.options
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["thermostat_mode"] == entry.options["power_mode"] == "disabled"


async def test_native_controls_require_explicit_selection_and_clear_external_identity(hass):
    """Changing mode removes source identity even if the form submits old fields."""
    registry = er.async_get(hass)
    climate = registry.async_get_or_create("climate", "demo", "old-thermostat")
    power = registry.async_get_or_create("switch", "demo", "old-power")
    previous = {
        "thermostat_mode": "external",
        "power_mode": "external",
        "climate_source": climate.entity_id,
        "climate_registry_id": climate.id,
        "power_source": power.entity_id,
        "power_registry_id": power.id,
    }
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id, options=previous)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=DEFAULT_OPTIONS
        | {
            "thermostat_mode": "native_q3",
            "power_mode": "native_q3",
            "climate_source": climate.entity_id,
            "power_source": power.entity_id,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["thermostat_mode"] == entry.options["power_mode"] == "native_q3"
    for key in ("climate_source", "climate_registry_id", "power_source", "power_registry_id"):
        assert key not in entry.options
    # Selecting external again cannot recover or silently reuse the old device.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=DEFAULT_OPTIONS | {"thermostat_mode": "external"}
    )
    assert result["errors"] == {"climate_source": "external_source_required"}
    assert entry.options["thermostat_mode"] == "native_q3"


@pytest.mark.parametrize(
    ("mode_key", "source_key"), [("thermostat_mode", "climate_source"), ("power_mode", "power_source")]
)
async def test_external_mode_requires_a_source(hass, mode_key, source_key):
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=DEFAULT_OPTIONS | {mode_key: "external"}
    )
    assert result["errors"] == {source_key: "external_source_required"}
    assert not entry.options


async def test_native_thermostat_with_external_power_does_not_need_shared_power_flag(hass):
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    source = er.async_get(hass).async_get_or_create("switch", "demo", "station-relay")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=DEFAULT_OPTIONS
        | {"thermostat_mode": "native_q3", "power_mode": "external", "power_source": source.entity_id},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["climate_power_linked"] is False
    assert entry.options["thermostat_mode"] == "native_q3"
    assert entry.options["power_mode"] == "external"


@pytest.mark.parametrize(
    ("thermostat_mode", "power_mode"),
    [("native_q3", "external"), ("external", "native_q3"), ("native_q3", "native_q3"), ("disabled", "disabled")],
)
async def test_shared_power_flag_requires_both_external_modes(hass, thermostat_mode, power_mode):
    """Supplying stale source fields cannot make a non-external pair linked."""
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    climate = registry.async_get_or_create("climate", "demo", "thermostat")
    power = registry.async_get_or_create("switch", "demo", "relay")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=DEFAULT_OPTIONS
        | {
            "thermostat_mode": thermostat_mode,
            "power_mode": power_mode,
            "climate_source": climate.entity_id,
            "power_source": power.entity_id,
            "climate_power_linked": True,
        },
    )
    assert result["errors"] == {"climate_power_linked": "power_link_requires_sources"}
    assert not entry.options


@pytest.mark.parametrize("mode_key", ["thermostat_mode", "power_mode"])
async def test_options_reject_unknown_control_modes(hass, mode_key):
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData) as err:
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=DEFAULT_OPTIONS | {mode_key: "auto"}
        )
    assert mode_key in err.value.schema_errors
    assert not entry.options


@pytest.mark.parametrize(
    ("previous_mode", "requested_mode"),
    [
        ("native_q3", "native_q3"),
        ("external", "external"),
        ("disabled", "disabled"),
        ("external", "disabled"),
        ("disabled", "external"),
    ],
)
async def test_native_cache_generation_is_preserved_without_native_backend_transition(
    hass, previous_mode, requested_mode
):
    """Changing intervals must keep a legitimate pending target's namespace."""
    source = er.async_get(hass).async_get_or_create("climate", "demo", "thermostat")
    generation = "a" * 32
    saved = DEFAULT_OPTIONS | {"thermostat_mode": previous_mode, "native_cache_generation": generation}
    if previous_mode == "external":
        saved |= {"climate_source": source.entity_id, "climate_registry_id": source.id}
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id, options=saved)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    selected = DEFAULT_OPTIONS | {"thermostat_mode": requested_mode, "poll_interval": 600}
    if requested_mode == "external":
        selected["climate_source"] = source.entity_id
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input=selected)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["native_cache_generation"] == generation


@pytest.mark.parametrize(
    ("previous_mode", "requested_mode"),
    [("native_q3", "external"), ("native_q3", "disabled"), ("external", "native_q3"), ("disabled", "native_q3")],
)
async def test_native_cache_generation_rotates_on_native_backend_transition(hass, previous_mode, requested_mode):
    """Leaving and returning to native control must never revive old intent."""
    source = er.async_get(hass).async_get_or_create("climate", "demo", "thermostat")
    generation = "a" * 32
    saved = DEFAULT_OPTIONS | {"thermostat_mode": previous_mode, "native_cache_generation": generation}
    if previous_mode == "external":
        saved |= {"climate_source": source.entity_id, "climate_registry_id": source.id}
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id, options=saved)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    selected = DEFAULT_OPTIONS | {"thermostat_mode": requested_mode}
    if requested_mode == "external":
        selected["climate_source"] = source.entity_id
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input=selected)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    replacement = entry.options["native_cache_generation"]
    assert replacement != generation
    assert UUID(replacement).hex == replacement


@pytest.mark.parametrize(
    ("field", "value"),
    [("poll_interval", 59), ("stale_after", 7201), ("startup_grace", 901)],
)
async def test_options_enforce_interval_bounds(hass, field, value):
    entry = MockConfigEntry(domain="telepool", unique_id=STATION.id)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData) as err:
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=DEFAULT_OPTIONS | {field: value}
        )
    assert field in err.value.schema_errors
    assert not entry.options
