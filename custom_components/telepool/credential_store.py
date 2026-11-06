"""Encrypt TelePool credentials using a separate, private Home Assistant Store."""

from __future__ import annotations

import asyncio
import json

from cryptography.fernet import Fernet, InvalidToken
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .storage import StrictStore as Store

KEY_STORAGE_KEY = f"{DOMAIN}.key"
KEY_STORAGE_VERSION = 1
_KEY_LOCK: HassKey[asyncio.Lock] = HassKey(f"{DOMAIN}.credentials_lock")


class CredentialStoreError(Exception):
    """A safe, non-secret description of a credential storage failure."""


def _has_existing_credentials(hass: HomeAssistant) -> bool:
    """Never replace a missing key while any entry still needs the old one."""
    return any(bool(entry.data.get("credentials")) for entry in hass.config_entries.async_entries(DOMAIN))


async def _async_fernet(hass: HomeAssistant, *, create: bool) -> Fernet:
    """Load the existing key, creating one only for a genuinely new store."""
    lock = hass.data.setdefault(_KEY_LOCK, asyncio.Lock())
    async with lock:
        store: Store[dict[str, str]] = Store(
            hass,
            KEY_STORAGE_VERSION,
            KEY_STORAGE_KEY,
            private=True,
            atomic_writes=True,
        )
        try:
            data = await store.async_load()
            if data is None:
                if not create or _has_existing_credentials(hass):
                    raise CredentialStoreError("encryption_key_missing")
                key = Fernet.generate_key().decode("ascii")
                # Commit the key before returning ciphertext to a config flow.
                await store.async_save({"key": key})
                return Fernet(key)
        except HomeAssistantError as err:
            raise CredentialStoreError("encryption_storage_error") from err
        except OSError as err:
            raise CredentialStoreError("encryption_storage_error") from err

        if not isinstance(data, dict) or not isinstance(data.get("key"), str):
            raise CredentialStoreError("encryption_key_invalid")
        try:
            return Fernet(data["key"])
        except (ValueError, TypeError) as err:
            raise CredentialStoreError("encryption_key_invalid") from err


async def async_encrypt_credentials(hass: HomeAssistant, username: str, password: str) -> str:
    """Return authenticated ciphertext; never persist the plaintext credentials."""
    if not isinstance(username, str) or not username:
        raise CredentialStoreError("credentials_invalid")
    if not isinstance(password, str) or not password:
        raise CredentialStoreError("credentials_invalid")
    fernet = await _async_fernet(hass, create=True)
    payload = json.dumps(
        {"version": 1, "username": username, "password": password},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return fernet.encrypt(payload).decode("ascii")


async def async_decrypt_credentials(hass: HomeAssistant, ciphertext: str) -> tuple[str, str]:
    """Decrypt credentials, refusing missing keys, tampering and wrong formats."""
    fernet = await _async_fernet(hass, create=False)
    try:
        if not isinstance(ciphertext, str):
            raise CredentialStoreError("credentials_invalid")
        data = json.loads(fernet.decrypt(ciphertext.encode("ascii")))
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("username"), str)
            or not data["username"]
            or not isinstance(data.get("password"), str)
            or not data["password"]
        ):
            raise CredentialStoreError("credentials_invalid")
        return data["username"], data["password"]
    except (InvalidToken, ValueError, UnicodeError, TypeError) as err:
        raise CredentialStoreError("credentials_invalid") from err
