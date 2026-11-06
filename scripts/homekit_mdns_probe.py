#!/usr/bin/env python3
"""Discover only the development HAP bridge inside its Docker network.

Run with the HA container's Python/zeroconf, not as a custom integration.
Stdout is JSON lines containing public DNS-SD metadata only. No HA storage,
HomeKit pairing data, HTTP endpoints, or device commands are accessed.
"""

from __future__ import annotations

import argparse
import base64
import json
import signal
import sys
import threading

SERVICE_TYPE = "_hap._tcp.local."
NAME_PREFIX = "TelePool Test"
PORT = 21063
TXT_KEYS = frozenset(("id", "c#", "s#", "ff", "ci", "md", "pv", "sf", "sh"))


def service_instance(name: str) -> str | None:
    """Extract the expected instance without accepting another service type."""
    if not name.endswith(f".{SERVICE_TYPE}"):
        return None
    instance = name[: -len(f".{SERVICE_TYPE}")]
    if (
        not instance.startswith(NAME_PREFIX)
        or len(instance.encode("utf-8")) > 63
        or any(ord(character) < 32 or ord(character) == 127 for character in instance)
    ):
        return None
    return instance


def public_properties(properties: dict[bytes, bytes | None]) -> dict:
    """Copy only public HAP TXT keys, preserving bytes and valueless fields."""
    result = {}
    for raw_key, value in properties.items():
        try:
            key = raw_key.decode("ascii")
        except UnicodeDecodeError:
            continue
        if key not in TXT_KEYS:
            continue
        if value is None:
            result[key] = None
        else:
            try:
                result[key] = value.decode("utf-8")
            except UnicodeDecodeError:
                result[key] = {"base64": base64.b64encode(value).decode("ascii")}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch-stdin", action="store_true", help="Exit when the host forwarder's input pipe closes")
    args = parser.parse_args()
    # Import only at runtime so validation helpers can be tested without opening
    # multicast sockets or depending on the container's zeroconf installation.
    from zeroconf import IPVersion, ServiceBrowser, Zeroconf

    stop = threading.Event()
    lock = threading.Lock()
    known: set[str] = set()

    def emit(event: str, name: str, properties=None) -> None:
        payload = {"event": event, "name": name, "port": PORT}
        if properties is not None:
            payload["properties"] = properties
        with lock:
            print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)

    class Listener:
        def add_service(self, zeroconf, service_type, name):
            self._resolve(zeroconf, service_type, name, "add")

        def update_service(self, zeroconf, service_type, name):
            self._resolve(zeroconf, service_type, name, "update")

        def remove_service(self, zeroconf, service_type, name):
            if name in known:
                known.remove(name)
                emit("remove", service_instance(name))

        def _resolve(self, zeroconf, service_type, name, event):
            instance = service_instance(name)
            if instance is None:
                return
            info = zeroconf.get_service_info(service_type, name, timeout=3000)
            if info is None:
                return
            if info.port != PORT:
                self.remove_service(zeroconf, service_type, name)
                return
            known.add(name)
            emit(event, instance, public_properties(info.properties))

    def shutdown(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if args.watch_stdin:

        def watch_input():
            while sys.stdin.buffer.read(1):
                pass
            stop.set()

        threading.Thread(target=watch_input, daemon=True).start()
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    browser = ServiceBrowser(zeroconf, SERVICE_TYPE, Listener())
    try:
        stop.wait()
    finally:
        browser.cancel()
        zeroconf.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
