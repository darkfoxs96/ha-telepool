"""Shared integration constants."""

import math
from collections.abc import Mapping
from typing import Any

DOMAIN = "telepool"
DEFAULT_POLL_INTERVAL = 300
DEFAULT_STALE_AFTER = 900
DEFAULT_STARTUP_GRACE = 180
PLATFORMS = ["sensor", "binary_sensor", "climate", "switch"]
CONF_MAX_TARGET_TEMPERATURE = "max_target_temperature"
DEFAULT_MAX_TARGET_TEMPERATURE = 35.0
MIN_MAX_TARGET_TEMPERATURE = 5.0
MAX_MAX_TARGET_TEMPERATURE = 40.0
CONTROL_MODES = ("disabled", "native_q3", "external")


def get_max_target_temperature(options: Mapping[str, Any]) -> float:
    """Use the configured Celsius limit, falling back on invalid stored options."""
    value = options.get(CONF_MAX_TARGET_TEMPERATURE, DEFAULT_MAX_TARGET_TEMPERATURE)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_MAX_TARGET_TEMPERATURE
    try:
        value = float(value)
    except OverflowError:
        return DEFAULT_MAX_TARGET_TEMPERATURE
    if (
        not math.isfinite(value)
        or not MIN_MAX_TARGET_TEMPERATURE <= value <= MAX_MAX_TARGET_TEMPERATURE
        or not (value * 2).is_integer()
    ):
        return DEFAULT_MAX_TARGET_TEMPERATURE
    return value


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
