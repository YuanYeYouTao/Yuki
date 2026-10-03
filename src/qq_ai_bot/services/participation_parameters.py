"""Host defaults for the pinned controller's existing tuning schema."""

from pydantic import Field
from yuki_participation.autonomy_parameters import AutonomyParameters as LibraryAutonomyParameters


class AutonomyParameters(LibraryAutonomyParameters):
    # Source opportunity rate is inversely proportional to this interval.
    # Explicit invitations already bypass sampling in the controller.
    source_interval_seconds: float = Field(default=30, ge=1, le=86400)


DEFAULT_AUTONOMY_PARAMETERS = AutonomyParameters()
