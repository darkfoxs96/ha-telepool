"""Intentionally small diagnostics: never serialize entries or raw HTTP data."""

from .const import get_control_mode


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data.coordinator
    sample = coordinator.snapshot
    return {
        "station": {
            "model": "Q3",
            "firmware": entry.runtime_data.station.firmware,
            "screen_type": entry.runtime_data.station.screen_type,
        },
        "poll_interval": entry.options.get("poll_interval", 300),
        "status": coordinator.availability.status,
        "data_fresh": coordinator.availability.fresh,
        "cloud_error": coordinator.cloud_error,
        "has_snapshot": sample is not None,
        "server_stale": sample.stale if sample else None,
        "external_power": coordinator.policy.power,
        "external_climate_configured": get_control_mode(entry.options, "thermostat") == "external",
        "thermostat_mode": get_control_mode(entry.options, "thermostat"),
        "power_mode": get_control_mode(entry.options, "power"),
        "native_control_available": bool(getattr(coordinator, "native_available", False)),
        "native_temperature_pending": getattr(coordinator, "native_pending_target", None) is not None,
        "last_poll": coordinator.last_attempt.isoformat() if getattr(coordinator, "last_attempt", None) else None,
        "last_successful_read": (
            coordinator.last_success.isoformat() if getattr(coordinator, "last_success", None) else None
        ),
    }
