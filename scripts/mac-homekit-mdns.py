#!/usr/bin/env python3
"""Mirror this development bridge's public HAP announcement using macOS Bonjour.

Docker Desktop isolates multicast from the Mac LAN. This script does not run a
HomeKit server: HA still serves the mapped HAP TCP port and handles pairing.
The container probe discovers HA's actual public TXT data; dns-sd -P advertises
that same identity and configuration at the explicit Mac LAN IPv4 address.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
from pathlib import Path

NAME_PREFIX = "TelePool Test"
PORT = 21063
TXT_KEYS = frozenset(("id", "c#", "s#", "ff", "ci", "md", "pv", "sf", "sh"))


def txt_arguments(properties: object) -> list[str]:
    r"""Encode public TXT byte-for-byte for dns-sd's documented \xHH parser."""
    if not isinstance(properties, dict) or not properties or set(properties) - TXT_KEYS:
        raise ValueError("Unexpected HAP TXT keys")
    result = []
    for key, value in sorted(properties.items()):
        if value is None:
            result.append(key)
            continue
        if isinstance(value, str):
            raw = value.encode("utf-8")
        elif isinstance(value, dict) and set(value) == {"base64"} and isinstance(value["base64"], str):
            try:
                raw = base64.b64decode(value["base64"], validate=True)
            except (ValueError, binascii.Error) as error:
                raise ValueError("Invalid HAP TXT encoding") from error
        else:
            raise ValueError("Invalid HAP TXT value")
        if len(key.encode("ascii")) + 1 + len(raw) > 255:
            raise ValueError("HAP TXT field exceeds DNS-SD's 255-byte limit")
        # dns-sd decodes hex escapes itself. Passing argv directly, without a
        # shell, preserves NUL/non-UTF8/backslash bytes inside the TXT payload.
        result.append(key + "=" + "".join(f"\\x{byte:02x}" for byte in raw))
    return result


def validate_event(payload: object) -> dict:
    """Accept only the one explicitly named development bridge and HAP port."""
    if not isinstance(payload, dict) or set(payload) - {"event", "name", "port", "properties"}:
        raise ValueError("Unexpected probe event")
    name = payload.get("name")
    if (
        not isinstance(name, str)
        or not name.startswith(NAME_PREFIX)
        or len(name.encode("utf-8")) > 63
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or payload.get("port") != PORT
        or payload.get("event") not in ("add", "update", "remove")
    ):
        raise ValueError("Unexpected HAP bridge")
    if payload["event"] != "remove":
        txt_arguments(payload.get("properties"))
    return payload


def publisher_command(payload: dict, address: str, host: str, interface: str) -> list[str]:
    """Build a native Bonjour proxy registration, restricted to the LAN interface."""
    validate_event(payload)
    if payload["event"] == "remove":
        raise ValueError("A removal cannot be published")
    return [
        "/usr/bin/dns-sd",
        "-i",
        interface,
        "-P",
        payload["name"],
        "_hap._tcp",
        "local.",
        str(PORT),
        host,
        address,
        *txt_arguments(payload["properties"]),
    ]


def terminate(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.stdin:
        process.stdin.close()
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def forward_once(args, stop: threading.Event) -> None:
    command = [
        args.docker,
        "compose",
        "exec",
        "-T",
        "homeassistant",
        "python",
        args.probe_path,
        "--watch-stdin",
    ]
    probe = subprocess.Popen(
        command,
        cwd=args.project_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    publisher = None
    published_command = None
    current_name = None
    selector = selectors.DefaultSelector()
    selector.register(probe.stdout, selectors.EVENT_READ)
    buffered = b""
    try:
        while not stop.is_set():
            if publisher is not None and publisher.poll() is not None:
                raise RuntimeError("Bonjour publication ended unexpectedly")
            if not selector.select(timeout=1):
                if probe.poll() is not None:
                    raise RuntimeError("Container discovery ended; waiting to reconnect")
                continue
            chunk = os.read(probe.stdout.fileno(), 16384)
            if not chunk:
                raise RuntimeError("Container discovery ended; waiting to reconnect")
            buffered += chunk
            if len(buffered) > 65536:
                raise RuntimeError("Container discovery exceeded its public-metadata limit")
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                try:
                    event = validate_event(json.loads(line))
                except ValueError, UnicodeError, TypeError:
                    print("Ignored an invalid container discovery event", file=sys.stderr, flush=True)
                    continue
                if event["event"] == "remove":
                    if event["name"] == current_name:
                        terminate(publisher)
                        publisher = None
                        published_command = None
                        current_name = None
                        print("Development bridge announcement removed", flush=True)
                    continue
                next_command = publisher_command(event, args.address, args.host, args.interface)
                if next_command == published_command:
                    continue
                terminate(publisher)
                publisher = subprocess.Popen(next_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                published_command = next_command
                current_name = event["name"]
                print(f"Bonjour proxy active: {current_name} at {args.address}:{PORT}; PID {publisher.pid}", flush=True)
    finally:
        selector.close()
        terminate(publisher)
        terminate(probe)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default=os.environ.get("TELEPOOL_HOMEKIT_BIND_IP"), help="Mac LAN IPv4 address")
    parser.add_argument("--host", default="telepool-homekit.local.", help="Bonjour proxy's local hostname")
    parser.add_argument("--interface", default="en0", help="LAN interface; avoids advertising on VPN interfaces")
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--probe-path", default="/opt/telepool-dev/homekit_mdns_probe.py")
    parser.add_argument(
        "--retries", type=int, default=6, help="Bounded retries after Docker/discovery process restarts"
    )
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This forwarder requires macOS Bonjour (/usr/bin/dns-sd)")
    try:
        address = ipaddress.IPv4Address(args.address)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError
    except ValueError, ipaddress.AddressValueError:
        parser.error("--address (or TELEPOOL_HOMEKIT_BIND_IP) must be the Mac's LAN IPv4 address")
    if (
        not args.host.endswith(".local.")
        or len(args.host.encode("utf-8")) > 255
        or any(character.isspace() or ord(character) < 32 for character in args.host)
        or not args.interface
        or not 0 <= args.retries <= 20
    ):
        parser.error("Use a .local. hostname, a LAN interface, and 0–20 retries")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    for attempt in range(args.retries + 1):
        try:
            forward_once(args, stop)
            return 0
        except (OSError, RuntimeError) as error:
            if stop.is_set():
                return 0
            print(f"{error} (attempt {attempt + 1}/{args.retries + 1})", file=sys.stderr, flush=True)
            if attempt == args.retries:
                return 1
            if stop.wait(min(2**attempt, 15)):
                return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
