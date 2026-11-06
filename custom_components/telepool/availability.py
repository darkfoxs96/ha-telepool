"""Pure policy for expected power cuts and telemetry freshness."""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Availability:
    status: str
    fresh: bool
    display_available: bool


class PowerPolicy:
    """Keep historical display values only during an expected power pause."""

    def __init__(self, stale_after: int = 900, startup_grace: int = 180) -> None:
        self.stale_after = timedelta(seconds=stale_after)
        self.startup_grace = timedelta(seconds=startup_grace)
        self.power: str | None = None
        self.started_at: datetime | None = None
        self.boot_watermark: datetime | None = None

    def set_power(self, power: str | None, now: datetime, observed: datetime | None) -> bool:
        """Return whether a known external relay has just switched on."""
        was = self.power
        self.power = power
        if power == "on" and was != "on":
            self.started_at = now
            self.boot_watermark = observed
            return True
        if power != "on":
            self.started_at = None
        return False

    def evaluate(
        self,
        now: datetime,
        observed: datetime | None,
        *,
        server_stale: bool,
        cloud_ok: bool,
        has_values: bool,
    ) -> Availability:
        if self.power == "off":
            return Availability("paused", False, has_values)
        age = now - observed if observed else None
        fresh = bool(
            observed
            and age is not None
            and -timedelta(minutes=2) <= age <= self.stale_after
            and not server_stale
            and cloud_ok
        )
        if self.started_at and self.boot_watermark:
            fresh = fresh and observed is not None and observed > self.boot_watermark
        if fresh:
            return Availability("working", True, has_values)
        if self.started_at and now < self.started_at + self.startup_grace:
            return Availability("starting", False, has_values)
        return Availability("no_connection", False, False)
