"""Credential encryption and key lifecycle must not lose existing access."""

import asyncio
import json
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.telepool.credential_store import (
    KEY_STORAGE_KEY,
    CredentialStoreError,
    async_decrypt_credentials,
    async_encrypt_credentials,
)
from custom_components.telepool.storage import StrictStore as Store


async def test_round_trip_is_encrypted_and_store_is_private(hass, hass_storage):
    """Only a ciphertext is returned, with a separate private atomic key store."""
    with patch("custom_components.telepool.credential_store.Store", wraps=Store) as store_factory:
        ciphertext = await async_encrypt_credentials(hass, "tester@example.invalid", "пароль-с-пробелом ")
        assert await async_decrypt_credentials(hass, ciphertext) == (
            "tester@example.invalid",
            "пароль-с-пробелом ",
        )
    assert "tester@example.invalid" not in ciphertext
    assert "пароль" not in ciphertext
    assert "tester@example.invalid" not in json.dumps(hass_storage)
    assert all(call.kwargs["private"] is True for call in store_factory.call_args_list)
    assert all(call.kwargs["atomic_writes"] is True for call in store_factory.call_args_list)
    assert hass_storage[KEY_STORAGE_KEY]["data"]["key"]


async def test_concurrent_first_use_keeps_one_working_key(hass, hass_storage):
    """Parallel config flows cannot replace each other's new key."""
    ciphertexts = await asyncio.gather(
        *(async_encrypt_credentials(hass, f"account-{i}", f"secret-{i}") for i in range(5))
    )
    key = hass_storage[KEY_STORAGE_KEY]["data"]["key"]
    for i, ciphertext in enumerate(ciphertexts):
        assert await async_decrypt_credentials(hass, ciphertext) == (
            f"account-{i}",
            f"secret-{i}",
        )
    assert hass_storage[KEY_STORAGE_KEY]["data"]["key"] == key


@pytest.mark.parametrize("failure", [WriteError("Disk write failed"), SerializationError("Serialization failed")])
async def test_no_ciphertext_when_key_commit_fails(hass, hass_storage, failure):
    """A failed durable key write must abort before credentials can be saved."""
    with patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        autospec=True,
        side_effect=failure,
    ):
        with pytest.raises(CredentialStoreError, match="encryption_storage_error"):
            await async_encrypt_credentials(hass, "account", "synthetic-secret")
    assert KEY_STORAGE_KEY not in hass_storage


async def test_decrypt_never_creates_a_missing_key(hass, hass_storage):
    """A missing key is reported without writing an unrelated new one."""
    with pytest.raises(CredentialStoreError, match="encryption_key_missing"):
        await async_decrypt_credentials(hass, "unreadable-ciphertext")
    assert KEY_STORAGE_KEY not in hass_storage


async def test_missing_key_with_existing_entry_is_not_regenerated(hass, hass_storage):
    """Adding or reauthenticating an entry must not strand other entries."""
    ciphertext = await async_encrypt_credentials(hass, "account", "secret")
    entry = MockConfigEntry(domain="telepool", unique_id="station-1", data={"credentials": ciphertext})
    entry.add_to_hass(hass)
    hass_storage.pop(KEY_STORAGE_KEY)
    with pytest.raises(CredentialStoreError, match="encryption_key_missing"):
        await async_encrypt_credentials(hass, "new-account", "new-secret")
    assert KEY_STORAGE_KEY not in hass_storage
    assert entry.data["credentials"] == ciphertext


async def test_invalid_key_is_preserved(hass, hass_storage):
    """Corrupt key data is not overwritten with a newly generated key."""
    hass_storage[KEY_STORAGE_KEY] = {
        "version": 1,
        "data": {"key": "not-a-fernet-key"},
    }
    with pytest.raises(CredentialStoreError, match="encryption_key_invalid"):
        await async_encrypt_credentials(hass, "account", "secret")
    assert hass_storage[KEY_STORAGE_KEY]["data"]["key"] == "not-a-fernet-key"


async def test_wrong_key_and_tampering_fail_without_disclosing_data(hass, hass_storage):
    """Authenticated encryption rejects alterations and unrelated keys."""
    ciphertext = await async_encrypt_credentials(hass, "account", "secret")
    with pytest.raises(CredentialStoreError, match="credentials_invalid"):
        await async_decrypt_credentials(hass, ciphertext[:-4] + "AAAA")
    replacement = Fernet.generate_key().decode("ascii")
    hass_storage[KEY_STORAGE_KEY]["data"]["key"] = replacement
    with pytest.raises(CredentialStoreError, match="credentials_invalid"):
        await async_decrypt_credentials(hass, ciphertext)
    assert hass_storage[KEY_STORAGE_KEY]["data"]["key"] == replacement


async def test_encrypted_unexpected_payload_is_not_accepted(hass, hass_storage):
    """A valid token is still required to contain our versioned credentials."""
    await async_encrypt_credentials(hass, "account", "secret")
    fernet = Fernet(hass_storage[KEY_STORAGE_KEY]["data"]["key"])
    bad_token = fernet.encrypt(b'{"version":1,"username":"account"}').decode()
    with pytest.raises(CredentialStoreError, match="credentials_invalid"):
        await async_decrypt_credentials(hass, bad_token)
