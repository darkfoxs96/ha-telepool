"""TelePool telemetry with explicitly selected native or external controls."""

from dataclasses import dataclass

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .client import Station, TelePoolClient
from .const import PLATFORMS
from .coordinator import TelePoolCoordinator
from .credential_store import CredentialStoreError, async_decrypt_credentials


@dataclass
class TelePoolRuntime:
    coordinator: TelePoolCoordinator
    station: Station
    client: TelePoolClient
    session: ClientSession


type TelePoolConfigEntry = ConfigEntry[TelePoolRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: TelePoolConfigEntry) -> bool:
    """Register selected controls even when the cloud is temporarily unreachable."""
    try:
        username, password = await async_decrypt_credentials(hass, entry.data["credentials"])
    except CredentialStoreError as err:
        raise ConfigEntryAuthFailed("Restore the TelePool encryption key or configure access again") from err
    station = Station(**entry.data["station"])
    session = async_create_clientsession(hass, auto_cleanup=False)
    entry.async_on_unload(session.detach)
    client = TelePoolClient(session, username, password)
    coordinator = TelePoolCoordinator(hass, entry, client, station)
    entry.runtime_data = TelePoolRuntime(coordinator, station, client, session)
    await coordinator.async_initialize()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: TelePoolConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TelePoolConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
