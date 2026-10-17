"""Models for trusted UTC and user-local time data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TimeContext:
    """A backend-produced time snapshot that model text cannot override."""

    utc: datetime
    local: datetime
    timezone: str

    def to_model_dict(self) -> dict[str, str]:
        """Return a compact JSON-safe representation for system context."""

        return {
            "utc": self.utc.isoformat().replace("+00:00", "Z"),
            "local": self.local.isoformat(),
            "timezone": self.timezone,
            "date": self.local.date().isoformat(),
            "weekday": self.local.strftime("%A"),
        }
