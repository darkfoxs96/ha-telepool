"""Shared integration constants."""

from collections.abc import Mapping
from typing import Any

DOMAIN = "telepool"
DEFAULT_POLL_INTERVAL = 300
DEFAULT_STALE_AFTER = 900
DEFAULT_STARTUP_GRACE = 180
PLATFORMS = ["sensor", "binary_sensor", "climate", "switch"]
POOL_MAX_TEMP_CELSIUS = 38.0
CONTROL_MODES = ("disabled", "native_q3", "external")


def get_control_mode(options: Mapping[str, Any], kind: str) -> str:
    """Preserve legacy external sources without silently enabling native writes."""
    if kind not in ("thermostat", "power"):
        raise ValueError("Unknown pool control kind")
    key = f"{kind}_mode"
    if key in options:
        mode = options[key]
        return mode if isinstance(mode, str) and mode in CONTROL_MODES else "disabled"
    source, registry = (
        ("climate_source", "climate_registry_id") if kind == "thermostat" else ("power_source", "power_registry_id")
    )
    return "external" if options.get(source) or options.get(registry) else "disabled"
