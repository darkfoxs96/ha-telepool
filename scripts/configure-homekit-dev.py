"""Prepare a private, allowlisted HomeKit bridge for the local Mac test stand."""

import argparse
import ipaddress
import json
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True, type=ipaddress.IPv4Address, help="Mac's Wi-Fi/LAN IPv4 address")
    parser.add_argument("--entry-id", help="TelePool config entry, required only for multiple configured stations")
    args = parser.parse_args()
    if args.address.is_loopback or args.address.is_unspecified or args.address.is_multicast:
        parser.error("Use the Mac's LAN address")

    config = ROOT / "dev/config"
    registry = json.loads((config / ".storage/core.entity_registry").read_text())["data"]["entities"]
    groups: dict[str, dict[str, str]] = {}
    suffixes = {"_climate": "climate", "_power": "power", "_ph_export": "ph", "_rx_export": "rx"}
    for entity in registry:
        if entity.get("platform") != "telepool" or entity.get("disabled_by"):
            continue
        for suffix, key in suffixes.items():
            if entity.get("unique_id", "").endswith(suffix):
                groups.setdefault(entity["config_entry_id"], {})[key] = entity["entity_id"]
    if args.entry_id:
        sources = groups.get(args.entry_id, {})
    elif len(groups) == 1:
        sources = next(iter(groups.values()))
    else:
        parser.error("Select exactly one configured TelePool station with --entry-id")
    if set(sources) != {"climate", "power", "ph", "rx"}:
        parser.error("Configure the thermostat/power proxies and enable export aliases in TelePool first")

    homekit_file = config / "homekit-telepool.yaml"
    homekit = [
        {
            "name": "TelePool Test",
            "port": 21063,
            "ip_address": "0.0.0.0",
            "advertise_ip": [str(args.address)],
            "filter": {"include_entities": [sources[key] for key in ("climate", "power", "ph", "rx")]},
            "entity_config": {
                sources["climate"]: {"name": "Water", "type": "thermostat"},
                sources["power"]: {"name": "Pool power"},
                sources["ph"]: {"name": "pH"},
                sources["rx"]: {"name": "Rx ÷10"},
            },
        }
    ]
    main_file = config / "configuration.yaml"
    main_config = main_file.read_text()
    include = "homekit: !include homekit-telepool.yaml"
    if any(line.startswith("homekit:") and line != include for line in main_config.splitlines()):
        parser.error("Existing HomeKit YAML needs a manual merge; it was not replaced")
    homekit_file.write_text(yaml.safe_dump(homekit, allow_unicode=True, sort_keys=False))
    os.chmod(homekit_file, 0o600)
    if include not in main_config.splitlines():
        main_file.write_text(main_config.rstrip() + "\n\n" + include + "\n")

    env_file = ROOT / ".env"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    lines = [line for line in lines if not line.startswith("TELEPOOL_HOMEKIT_BIND_IP=")]
    env_file.write_text("\n".join([*lines, f"TELEPOOL_HOMEKIT_BIND_IP={args.address}"]) + "\n")
    os.chmod(env_file, 0o600)
    print("Prepared TelePool Test: thermostat, power switch, pH and Rx/10; private configuration saved.")


if __name__ == "__main__":
    main()
