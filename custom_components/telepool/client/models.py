"""Transport models independent of Home Assistant."""

from dataclasses import dataclass, field
from datetime import datetime

Number = int | float


@dataclass(frozen=True, slots=True)
class Station:
    """A Q3 station found in the authenticated user's own pools.

    ``screen_type`` is the channel bit mask (Fu=1, Rx=2, Ph=4, Fcl=8),
    not DevicesMy.Type, whose value for Q3 is 9. ``user_id`` is private
    routing context and must not be copied to entity attributes/diagnostics.
    """

    id: str
    pool_id: str
    pool_name: str
    name: str
    screen_type: int
    firmware: str | None = None
    user_id: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Latest available chart point and the server's quality indicators.

    ``observed_at`` comes from the selected point's ``visit`` and is the
    freshness source. It is not guaranteed to be a raw physical measurement.
    ``received_at`` comes from the chart envelope's distinct ``last`` value;
    its exact server meaning is unverified. Neither is the local poll time.
    ``stale`` combines the server flags and missing measurement timestamps;
    the caller applies its own age and expected-power policy.
    """

    station_id: str
    values: dict[str, Number | None]
    modes: dict[str, int]
    observed_at: datetime | None
    received_at: datetime | None
    stale: bool
    error: bool
    rssi: int | None
    quality: int | None


@dataclass(frozen=True, slots=True)
class Q3Controls:
    """Validated web-form settings, not proof of physical actuator state.

    ``menu_enable_fu`` is the raw MenuEnableFu setting labeled FU menu
    control. Its relationship to physical filtering/heating remains unverified.
    ``can_set_*`` describe the current form, not installed hardware or a
    successful prior write. Missing/invalid temperature metadata stays None.
    """

    station_id: str
    target_temperature: float | None
    menu_enable_fu: bool | None
    min_temperature: float | None
    max_temperature: float | None
    temperature_step: float | None
    can_set_temperature: bool
    can_set_enabled: bool
