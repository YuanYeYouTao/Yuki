"""Shared route identity and reference classification."""

from __future__ import annotations

from enum import StrEnum
from typing import final


@final
class RouteKind(StrEnum):
    PERSON_ACTIVE = "person_active"
    SPACE_BINDING_INGEST = "space_binding_ingest"
    SPACE_ACTIVE = "space_active"


@final
class RouteReferenceState(StrEnum):
    CONSISTENT = "consistent"
    STATE_MISMATCH = "state_mismatch"


def classify_route_reference(
    *,
    expected_owner_id: str | None,
    actual_owner_id: str | None,
    binding_platform: str | None,
    presence_platform: str | None,
) -> RouteReferenceState:
    """Mark incomplete or cross-platform route joins. Do not invent a match."""

    if expected_owner_id is not None and type(expected_owner_id) is not str:
        raise TypeError("expected_owner_id must be a str or None")
    if actual_owner_id is None or binding_platform is None or presence_platform is None:
        return RouteReferenceState.STATE_MISMATCH
    if type(actual_owner_id) is not str or type(binding_platform) is not str:
        raise TypeError("route reference tokens must be str")
    if type(presence_platform) is not str:
        raise TypeError("presence_platform must be a str")
    if expected_owner_id is not None and actual_owner_id != expected_owner_id:
        return RouteReferenceState.STATE_MISMATCH
    if binding_platform != presence_platform:
        return RouteReferenceState.STATE_MISMATCH
    return RouteReferenceState.CONSISTENT
