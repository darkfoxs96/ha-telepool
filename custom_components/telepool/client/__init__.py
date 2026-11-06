"""Packaged TelePool client with no Home Assistant dependency."""

from .api import TelePoolClient
from .exceptions import (
    AuthenticationError,
    CommandNotSent,
    ConnectionError,
    ProtocolError,
    SettingsRejected,
    TelePoolError,
    TemperatureRangeRejected,
    WriteOutcomeUnknown,
)
from .models import Q3Controls, Snapshot, Station

__all__ = [
    "AuthenticationError",
    "CommandNotSent",
    "ConnectionError",
    "ProtocolError",
    "Q3Controls",
    "SettingsRejected",
    "Snapshot",
    "Station",
    "TelePoolClient",
    "TelePoolError",
    "TemperatureRangeRejected",
    "WriteOutcomeUnknown",
]
