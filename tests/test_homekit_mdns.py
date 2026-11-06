"""Pure metadata validation; never advertise, pair with, or control a device."""

import base64
import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load_script(filename):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOST = load_script("mac-homekit-mdns.py")
PROBE = load_script("homekit_mdns_probe.py")


def event(**changes):
    return {
        "event": "add",
        "name": "TelePool Test A1B2C3",
        "port": 21063,
        "properties": {"id": "AA:BB:CC:DD:EE:FF", "c#": "2", "sf": "1"},
        **changes,
    }


def test_probe_copies_only_public_hap_txt_and_preserves_binary_bytes():
    original = {b"id": b"AA:BB", b"md": "Тест".encode(), b"sh": b"\xff\x00\\", b"pv": None, b"pin": b"123-45-678"}
    public = PROBE.public_properties(original)
    assert public == {
        "id": "AA:BB",
        "md": "Тест",
        "sh": {"base64": base64.b64encode(b"\xff\x00\\").decode()},
        "pv": None,
    }
    arguments = HOST.txt_arguments(public)
    assert "sh=\\xff\\x00\\x5c" in arguments
    assert "md=" + "".join(f"\\x{byte:02x}" for byte in "Тест".encode()) in arguments
    assert "pv" in arguments


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Another HomeKit Bridge"},
        {"name": "TelePool Test\nforged log"},
        {"port": 21064},
        {"event": "pair"},
        {"properties": {"pin": "123-45-678"}},
        {"properties": {"md": "x" * 253}},
        {"properties": {"sh": {"base64": "not base64"}}},
        {"properties": {"id": {"secret": "never forward"}}},
    ],
)
def test_host_rejects_other_bridges_secret_keys_or_invalid_txt(changes):
    with pytest.raises(ValueError):
        HOST.validate_event(event(**changes))


def test_native_proxy_command_uses_explicit_lan_address_and_byte_escaped_txt():
    command = HOST.publisher_command(event(), "192.0.2.95", "telepool-homekit.local.", "en0")
    assert command[:10] == [
        "/usr/bin/dns-sd",
        "-i",
        "en0",
        "-P",
        "TelePool Test A1B2C3",
        "_hap._tcp",
        "local.",
        "21063",
        "telepool-homekit.local.",
        "192.0.2.95",
    ]
    assert "c#=\\x32" in command
    assert "sf=\\x31" in command
    with pytest.raises(ValueError):
        HOST.publisher_command(event(event="remove"), "192.0.2.95", "telepool-homekit.local.", "en0")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("TelePool Test A1B2C3._hap._tcp.local.", "TelePool Test A1B2C3"),
        ("Other._hap._tcp.local.", None),
        ("TelePool Test._http._tcp.local.", None),
        ("TelePool Test\n._hap._tcp.local.", None),
    ],
)
def test_probe_filters_name_and_service_type(name, expected):
    assert PROBE.service_instance(name) == expected
