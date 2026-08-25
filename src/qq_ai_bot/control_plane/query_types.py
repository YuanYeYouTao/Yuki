"""Pure control-query DTOs. No I/O and no catalog objects."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal, final

from qq_ai_bot.control_plane.operations import OperationRef, StateEpoch
from qq_ai_bot.control_plane.problems import Problem
from qq_ai_bot.control_plane.tokens import require_aware_datetime, require_opaque_token
from qq_ai_bot.domain.identity import (
    ConversationGeneration,
    ConversationId,
    IdentityBindingId,
    PersonId,
    PresenceId,
    RouteGeneration,
    SpaceBindingId,
    SpaceId,
)

QUERY_CURSOR_VERSION: Final[str] = "c10v1"
LAST4_MIN_SOURCE_LENGTH: Final[int] = 8
REDACTED_DISPLAY: Final[str] = "redacted"


@final
class ControlQueryError(Exception):
    """Controlled query failure. Callers must not treat this as an empty page."""

    def __init__(self, problem: Problem) -> None:
        if type(problem) is not Problem:
            raise TypeError("problem must be Problem")
        self.problem = problem
        super().__init__(problem.code.value)


@final
class IdentityResolution(StrEnum):
    """Canonical projection state; unresolved means canonical data is unusable."""

    CANONICAL = "canonical"
    UNRESOLVED = "unresolved"


@final
class ExternalIdVisibility(StrEnum):
    MASKED = "masked"
    REVEALED = "revealed"


@final
class PresenceConnectionState(StrEnum):
    UNAVAILABLE = "unavailable"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    AMBIGUOUS = "ambiguous"


@final
class RouteKind(StrEnum):
    PERSON_ACTIVE = "person_active"
    SPACE_BINDING_INGEST = "space_binding_ingest"
    SPACE_ACTIVE = "space_active"


@final
class RouteReferenceState(StrEnum):
    CONSISTENT = "consistent"
    STATE_MISMATCH = "state_mismatch"


@final
class QueryResourceKind(StrEnum):
    PERSON = "person"
    BINDING = "binding"
    SPACE = "space"
    SPACE_BINDING = "space_binding"
    PRESENCE = "presence"
    CONVERSATION = "conversation"
    PERSON_ROUTE = "person_route"
    INGEST_ROUTE = "ingest_route"
    SPACE_ROUTE = "space_route"
    AUDIT = "audit"
    OPERATION = "operation"
    CONFIG = "config"
    MEMORY_FACT = "memory_fact"
    MEMORY_JOB = "memory_job"
    AUTOMATION = "automation"
    PLUGIN = "plugin"
    MCP = "mcp"
    EMOJI = "emoji"
    SPEECH = "speech"


@final
class ConfigOwnerKind(StrEnum):
    GLOBAL = "global"
    PERSON = "person"
    SPACE = "space"
    UNAVAILABLE = "unavailable"


@final
class QueryCursorPhase(StrEnum):
    CANONICAL = "c"
    TIME_ID = "t"


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


def mask_external_id(value: str, *, reveal: bool) -> ExternalIdView:
    """Project an external account or space id. IDs of length <= 8 never emit last4."""

    if type(value) is not str:
        raise TypeError("external id must be a str")
    if type(reveal) is not bool:
        raise TypeError("reveal must be a bool")
    if not value:
        return ExternalIdView(
            visibility=ExternalIdVisibility.MASKED,
            configured=False,
            last4=None,
            value=None,
        )
    if reveal:
        return ExternalIdView(
            visibility=ExternalIdVisibility.REVEALED,
            configured=True,
            last4=None,
            value=value,
        )
    last4 = value[-4:] if len(value) > LAST4_MIN_SOURCE_LENGTH else None
    return ExternalIdView(
        visibility=ExternalIdVisibility.MASKED,
        configured=True,
        last4=last4,
        value=None,
    )


def sanitize_projected_display(
    value: str,
    *,
    external_ids: Sequence[str],
    reveal: bool,
) -> str:
    """Hide display metadata that embeds a full external id unless reveal is granted."""

    if type(value) is not str:
        raise TypeError("display must be a str")
    if type(reveal) is not bool:
        raise TypeError("reveal must be a bool")
    if isinstance(external_ids, (str, bytes)):
        raise TypeError("external_ids must be a sequence of tokens")
    if reveal:
        return value
    for token in external_ids:
        if type(token) is not str:
            raise TypeError("external id must be a str")
        if token and token in value:
            return REDACTED_DISPLAY
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a bool")
    return value


def _require_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or type(value) is bool:
        raise TypeError(f"{name} must be an int")
    if value < minimum:
        raise ValueError(f"{name} is out of range")
    return value


def _validate_config_owner(
    *,
    owner_kind: ConfigOwnerKind | None,
    person_id: PersonId | None,
    space_id: SpaceId | None,
    resolution: IdentityResolution | None,
    required: bool = False,
) -> None:
    if owner_kind is None:
        if required:
            raise TypeError("owner_kind must be ConfigOwnerKind")
        if person_id is not None or space_id is not None or resolution is not None:
            raise ValueError("owner fields require owner_kind")
        return
    if type(owner_kind) is not ConfigOwnerKind:
        raise TypeError("owner_kind must be ConfigOwnerKind")
    if person_id is not None and type(person_id) is not PersonId:
        raise TypeError("person_id must be PersonId or None")
    if space_id is not None and type(space_id) is not SpaceId:
        raise TypeError("space_id must be SpaceId or None")
    if resolution is not None and type(resolution) is not IdentityResolution:
        raise TypeError("resolution must be IdentityResolution or None")
    if owner_kind is ConfigOwnerKind.GLOBAL:
        if person_id is not None or space_id is not None:
            raise ValueError("global config owner cannot carry person or space")
        return
    if owner_kind is ConfigOwnerKind.UNAVAILABLE:
        if person_id is not None or space_id is not None:
            raise ValueError("unavailable config owner cannot carry canonical ids")
        if resolution is IdentityResolution.CANONICAL:
            raise ValueError("unavailable config owner cannot be canonical")
        return
    if resolution is None:
        raise ValueError("person or space config owner requires resolution")
    if owner_kind is ConfigOwnerKind.PERSON:
        if space_id is not None:
            raise ValueError("person config owner cannot carry space")
        if resolution is IdentityResolution.CANONICAL and person_id is None:
            raise ValueError("canonical person owner requires person_id")
        if resolution is not IdentityResolution.CANONICAL and person_id is not None:
            raise ValueError("non-canonical person owner cannot carry person_id")
        return
    if person_id is not None:
        raise ValueError("space config owner cannot carry person")
    if resolution is IdentityResolution.CANONICAL and space_id is None:
        raise ValueError("canonical space owner requires space_id")
    if resolution is not IdentityResolution.CANONICAL and space_id is not None:
        raise ValueError("non-canonical space owner cannot carry space_id")


@final
@dataclass(frozen=True, slots=True)
class ExternalIdView:
    visibility: ExternalIdVisibility
    configured: bool
    last4: str | None
    value: str | None

    def __post_init__(self) -> None:
        if type(self.visibility) is not ExternalIdVisibility:
            raise TypeError("visibility must be ExternalIdVisibility")
        _require_bool(self.configured, "configured")
        if self.last4 is not None:
            token = require_opaque_token(self.last4, name="last4", max_length=4)
            if len(token) != 4:
                raise ValueError("last4 must be exactly 4 characters")
            object.__setattr__(self, "last4", token)
        if self.value is not None:
            if type(self.value) is not str:
                raise TypeError("value must be a str or None")
            if not self.value:
                raise ValueError("revealed external id cannot be empty")
        if not self.configured:
            if self.last4 is not None or self.value is not None:
                raise ValueError("unconfigured external id cannot carry last4 or value")
            if self.visibility is ExternalIdVisibility.REVEALED:
                raise ValueError("unconfigured external id cannot be revealed")
            return
        if self.visibility is ExternalIdVisibility.REVEALED:
            if self.last4 is not None:
                raise ValueError("revealed external id cannot carry last4")
            if self.value is None:
                raise ValueError("revealed external id requires a nonempty value")
            return
        if self.value is not None:
            raise ValueError("masked external id cannot carry a value")
        if self.last4 is not None and (
            self.last4 == self.value
            or (self.value is not None and (self.last4 in self.value or self.value in self.last4))
        ):
            raise ValueError("last4 must be a genuine partial suffix")


@final
@dataclass(frozen=True, slots=True)
class CountSnapshot:
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "count", _require_int(self.count, "count"))


@final
@dataclass(frozen=True, slots=True)
class QueueSummary:
    memory_jobs_pending: int
    memory_jobs_processing: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "memory_jobs_pending",
            _require_int(self.memory_jobs_pending, "memory_jobs_pending"),
        )
        object.__setattr__(
            self,
            "memory_jobs_processing",
            _require_int(self.memory_jobs_processing, "memory_jobs_processing"),
        )


@final
@dataclass(frozen=True, slots=True)
class PendingRestartView:
    keys: tuple[str, ...]
    count: int

    def __init__(self, keys: tuple[str, ...] | list[str], count: int) -> None:
        if isinstance(keys, (str, bytes)):
            raise TypeError("keys must be a sequence of tokens")
        normalized = tuple(
            require_opaque_token(item, name="restart_key", max_length=128) for item in keys
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("restart keys must be unique")
        total = _require_int(count, "count")
        if total != len(normalized):
            raise ValueError("restart count must match unique keys")
        object.__setattr__(self, "keys", normalized)
        object.__setattr__(self, "count", total)


@final
@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    version: str
    identity_state: StateEpoch
    identity_revision: int
    persons: CountSnapshot
    identity_bindings: CountSnapshot
    spaces: CountSnapshot
    space_bindings: CountSnapshot
    presences: CountSnapshot
    conversations: CountSnapshot
    queue: QueueSummary
    pending_restart: PendingRestartView

    def __post_init__(self) -> None:
        require_opaque_token(self.version, name="version", max_length=32)
        if type(self.identity_state) is not StateEpoch:
            raise TypeError("identity_state must be StateEpoch")
        object.__setattr__(
            self,
            "identity_revision",
            _require_int(self.identity_revision, "identity_revision", minimum=1),
        )
        for name in (
            "persons",
            "identity_bindings",
            "spaces",
            "space_bindings",
            "presences",
            "conversations",
        ):
            if type(getattr(self, name)) is not CountSnapshot:
                raise TypeError(f"{name} must be CountSnapshot")
        if type(self.queue) is not QueueSummary:
            raise TypeError("queue must be QueueSummary")
        if type(self.pending_restart) is not PendingRestartView:
            raise TypeError("pending_restart must be PendingRestartView")


@final
@dataclass(frozen=True, slots=True)
class YukiSummaryView:
    """One database is one Yuki. This is not a persisted subject."""

    yuki_count: Literal[1]
    presence_count: int
    identity_state: StateEpoch

    def __post_init__(self) -> None:
        if self.yuki_count != 1:
            raise ValueError("a database has exactly one synthetic Yuki")
        object.__setattr__(
            self, "presence_count", _require_int(self.presence_count, "presence_count")
        )
        if type(self.identity_state) is not StateEpoch:
            raise TypeError("identity_state must be StateEpoch")


@final
@dataclass(frozen=True, slots=True)
class ManagementHealthView:
    identity_state: StateEpoch
    identity_revision: int
    database: Literal["ok", "unavailable"]
    queue: QueueSummary

    def __post_init__(self) -> None:
        if type(self.identity_state) is not StateEpoch:
            raise TypeError("identity_state must be StateEpoch")
        object.__setattr__(
            self,
            "identity_revision",
            _require_int(self.identity_revision, "identity_revision", minimum=1),
        )
        if self.database not in {"ok", "unavailable"}:
            raise ValueError("database health must be a safe token")
        if type(self.queue) is not QueueSummary:
            raise TypeError("queue must be QueueSummary")


@final
@dataclass(frozen=True, slots=True)
class PersonView:
    person_id: PersonId | None
    resolution: IdentityResolution
    enabled: bool | None
    revision: int | None
    created_at: datetime | None
    updated_at: datetime | None
    binding_count: int

    def __post_init__(self) -> None:
        if self.person_id is not None and type(self.person_id) is not PersonId:
            raise TypeError("person_id must be PersonId or None")
        if type(self.resolution) is not IdentityResolution:
            raise TypeError("resolution must be IdentityResolution")
        if self.resolution is IdentityResolution.CANONICAL and self.person_id is None:
            raise ValueError("canonical person requires person_id")
        if self.resolution is not IdentityResolution.CANONICAL and self.person_id is not None:
            raise ValueError("non-canonical person cannot carry a canonical id")
        if self.enabled is not None:
            _require_bool(self.enabled, "enabled")
        if self.revision is not None:
            object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))
        if self.created_at is not None:
            object.__setattr__(
                self, "created_at", require_aware_datetime(self.created_at, name="created_at")
            )
        if self.updated_at is not None:
            object.__setattr__(
                self, "updated_at", require_aware_datetime(self.updated_at, name="updated_at")
            )
        object.__setattr__(self, "binding_count", _require_int(self.binding_count, "binding_count"))


@final
@dataclass(frozen=True, slots=True)
class IdentityBindingView:
    binding_id: IdentityBindingId | None
    person_id: PersonId | None
    resolution: IdentityResolution
    platform: str
    external: ExternalIdView
    display_name: str
    status: str
    revision: int | None

    def __post_init__(self) -> None:
        if self.binding_id is not None and type(self.binding_id) is not IdentityBindingId:
            raise TypeError("binding_id must be IdentityBindingId or None")
        if self.person_id is not None and type(self.person_id) is not PersonId:
            raise TypeError("person_id must be PersonId or None")
        if type(self.resolution) is not IdentityResolution:
            raise TypeError("resolution must be IdentityResolution")
        if self.resolution is IdentityResolution.CANONICAL:
            if self.binding_id is None or self.person_id is None:
                raise ValueError("canonical binding requires canonical ids")
        elif self.binding_id is not None or self.person_id is not None:
            raise ValueError("non-canonical binding cannot carry canonical ids")
        require_opaque_token(self.platform, name="platform", max_length=32)
        if type(self.external) is not ExternalIdView:
            raise TypeError("external must be ExternalIdView")
        if type(self.display_name) is not str:
            raise TypeError("display_name must be a str")
        require_opaque_token(self.status, name="status", max_length=16)
        if self.revision is not None:
            object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))


@final
@dataclass(frozen=True, slots=True)
class SpaceView:
    space_id: SpaceId | None
    resolution: IdentityResolution
    name: str
    enabled: bool | None
    autonomous_enabled: bool | None
    require_mention: bool | None
    revision: int | None
    binding_count: int

    def __post_init__(self) -> None:
        if self.space_id is not None and type(self.space_id) is not SpaceId:
            raise TypeError("space_id must be SpaceId or None")
        if type(self.resolution) is not IdentityResolution:
            raise TypeError("resolution must be IdentityResolution")
        if self.resolution is IdentityResolution.CANONICAL and self.space_id is None:
            raise ValueError("canonical space requires space_id")
        if self.resolution is not IdentityResolution.CANONICAL and self.space_id is not None:
            raise ValueError("non-canonical space cannot carry a canonical id")
        if type(self.name) is not str:
            raise TypeError("name must be a str")
        for field_name in ("enabled", "autonomous_enabled", "require_mention"):
            value = getattr(self, field_name)
            if value is not None:
                _require_bool(value, field_name)
        if self.revision is not None:
            object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))
        object.__setattr__(self, "binding_count", _require_int(self.binding_count, "binding_count"))


@final
@dataclass(frozen=True, slots=True)
class SpaceBindingView:
    binding_id: SpaceBindingId | None
    space_id: SpaceId | None
    resolution: IdentityResolution
    platform: str
    external: ExternalIdView
    display_name: str
    status: str
    revision: int | None

    def __post_init__(self) -> None:
        if self.binding_id is not None and type(self.binding_id) is not SpaceBindingId:
            raise TypeError("binding_id must be SpaceBindingId or None")
        if self.space_id is not None and type(self.space_id) is not SpaceId:
            raise TypeError("space_id must be SpaceId or None")
        if type(self.resolution) is not IdentityResolution:
            raise TypeError("resolution must be IdentityResolution")
        if self.resolution is IdentityResolution.CANONICAL:
            if self.binding_id is None or self.space_id is None:
                raise ValueError("canonical space binding requires canonical ids")
        elif self.binding_id is not None or self.space_id is not None:
            raise ValueError("non-canonical space binding cannot carry canonical ids")
        require_opaque_token(self.platform, name="platform", max_length=32)
        if type(self.external) is not ExternalIdView:
            raise TypeError("external must be ExternalIdView")
        if type(self.display_name) is not str:
            raise TypeError("display_name must be a str")
        require_opaque_token(self.status, name="status", max_length=16)
        if self.revision is not None:
            object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))


@final
@dataclass(frozen=True, slots=True)
class PresenceView:
    presence_id: PresenceId
    platform: str
    external: ExternalIdView
    enabled: bool
    ingest_eligible: bool
    revision: int
    connection_state: PresenceConnectionState
    connection_provider: str | None
    connection_generation: int | None
    connection_capabilities: tuple[str, ...]
    connection_problem: Problem

    def __post_init__(self) -> None:
        if type(self.presence_id) is not PresenceId:
            raise TypeError("presence_id must be PresenceId")
        require_opaque_token(self.platform, name="platform", max_length=32)
        if type(self.external) is not ExternalIdView:
            raise TypeError("external must be ExternalIdView")
        _require_bool(self.enabled, "enabled")
        _require_bool(self.ingest_eligible, "ingest_eligible")
        object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))
        if type(self.connection_state) is not PresenceConnectionState:
            raise TypeError("connection_state must be PresenceConnectionState")
        if self.connection_provider is not None:
            require_opaque_token(
                self.connection_provider,
                name="connection_provider",
                max_length=32,
            )
        if self.connection_generation is not None:
            object.__setattr__(
                self,
                "connection_generation",
                _require_int(self.connection_generation, "connection_generation", minimum=1),
            )
        if type(self.connection_capabilities) is not tuple:
            raise TypeError("connection_capabilities must be a tuple")
        normalized_capabilities = tuple(
            require_opaque_token(item, name="connection_capability", max_length=64)
            for item in self.connection_capabilities
        )
        if normalized_capabilities != tuple(sorted(set(normalized_capabilities))):
            raise ValueError("connection_capabilities must be sorted and unique")
        if type(self.connection_problem) is not Problem:
            raise TypeError("connection_problem must be Problem")


@final
@dataclass(frozen=True, slots=True)
class ConversationView:
    conversation_id: ConversationId | None
    resolution: IdentityResolution
    kind: str
    person_id: PersonId | None
    space_id: SpaceId | None
    generation: ConversationGeneration | None
    last_event_id: int
    starts_after_event_id: int
    covered_through_event_id: int | None
    last_generation_change_event_id: int
    uncovered_event_count: int
    revision: int | None

    def __post_init__(self) -> None:
        if self.conversation_id is not None and type(self.conversation_id) is not ConversationId:
            raise TypeError("conversation_id must be ConversationId or None")
        if type(self.resolution) is not IdentityResolution:
            raise TypeError("resolution must be IdentityResolution")
        kind = require_opaque_token(self.kind, name="kind", max_length=16)
        if kind not in {"private", "space", "group"}:
            raise ValueError("conversation kind is not recognized")
        object.__setattr__(self, "kind", "space" if kind == "group" else kind)
        if self.person_id is not None and type(self.person_id) is not PersonId:
            raise TypeError("person_id must be PersonId or None")
        if self.space_id is not None and type(self.space_id) is not SpaceId:
            raise TypeError("space_id must be SpaceId or None")
        if self.resolution is IdentityResolution.CANONICAL and self.conversation_id is None:
            raise ValueError("canonical conversation requires conversation_id")
        if self.resolution is not IdentityResolution.CANONICAL:
            if self.conversation_id is not None or self.person_id is not None:
                raise ValueError("non-canonical conversation cannot carry canonical ids")
            if self.space_id is not None:
                raise ValueError("non-canonical conversation cannot carry canonical ids")
        if self.generation is not None and type(self.generation) is not ConversationGeneration:
            raise TypeError("generation must be ConversationGeneration or None")
        for name in (
            "last_event_id",
            "starts_after_event_id",
            "last_generation_change_event_id",
            "uncovered_event_count",
        ):
            object.__setattr__(self, name, _require_int(getattr(self, name), name))
        if self.covered_through_event_id is not None:
            object.__setattr__(
                self,
                "covered_through_event_id",
                _require_int(self.covered_through_event_id, "covered_through_event_id"),
            )
        if self.revision is not None:
            object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))


@final
@dataclass(frozen=True, slots=True)
class PersonActiveRouteView:
    kind: RouteKind
    person_id: PersonId
    identity_binding_id: IdentityBindingId
    presence_id: PresenceId
    route_generation: RouteGeneration
    paused: bool
    revision: int
    reference_state: RouteReferenceState

    def __post_init__(self) -> None:
        if self.kind is not RouteKind.PERSON_ACTIVE:
            raise ValueError("person active route kind mismatch")
        if type(self.person_id) is not PersonId:
            raise TypeError("person_id must be PersonId")
        if type(self.identity_binding_id) is not IdentityBindingId:
            raise TypeError("identity_binding_id must be IdentityBindingId")
        if type(self.presence_id) is not PresenceId:
            raise TypeError("presence_id must be PresenceId")
        if type(self.route_generation) is not RouteGeneration:
            raise TypeError("route_generation must be RouteGeneration")
        _require_bool(self.paused, "paused")
        object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))
        if type(self.reference_state) is not RouteReferenceState:
            raise TypeError("reference_state must be RouteReferenceState")


@final
@dataclass(frozen=True, slots=True)
class SpaceBindingIngestRouteView:
    kind: RouteKind
    space_binding_id: SpaceBindingId
    ingest_presence_id: PresenceId
    route_generation: RouteGeneration
    paused: bool
    revision: int
    reference_state: RouteReferenceState

    def __post_init__(self) -> None:
        if self.kind is not RouteKind.SPACE_BINDING_INGEST:
            raise ValueError("ingest route kind mismatch")
        if type(self.space_binding_id) is not SpaceBindingId:
            raise TypeError("space_binding_id must be SpaceBindingId")
        if type(self.ingest_presence_id) is not PresenceId:
            raise TypeError("ingest_presence_id must be PresenceId")
        if type(self.route_generation) is not RouteGeneration:
            raise TypeError("route_generation must be RouteGeneration")
        _require_bool(self.paused, "paused")
        object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))
        if type(self.reference_state) is not RouteReferenceState:
            raise TypeError("reference_state must be RouteReferenceState")


@final
@dataclass(frozen=True, slots=True)
class SpaceActiveRouteView:
    kind: RouteKind
    space_id: SpaceId
    space_binding_id: SpaceBindingId
    presence_id: PresenceId
    route_generation: RouteGeneration
    paused: bool
    revision: int
    reference_state: RouteReferenceState

    def __post_init__(self) -> None:
        if self.kind is not RouteKind.SPACE_ACTIVE:
            raise ValueError("space active route kind mismatch")
        if type(self.space_id) is not SpaceId:
            raise TypeError("space_id must be SpaceId")
        if type(self.space_binding_id) is not SpaceBindingId:
            raise TypeError("space_binding_id must be SpaceBindingId")
        if type(self.presence_id) is not PresenceId:
            raise TypeError("presence_id must be PresenceId")
        if type(self.route_generation) is not RouteGeneration:
            raise TypeError("route_generation must be RouteGeneration")
        _require_bool(self.paused, "paused")
        object.__setattr__(self, "revision", _require_int(self.revision, "revision", minimum=1))
        if type(self.reference_state) is not RouteReferenceState:
            raise TypeError("reference_state must be RouteReferenceState")


@final
@dataclass(frozen=True, slots=True)
class AuditEventView:
    audit_id: int
    capability: str
    operation: str
    target_type: str
    success: bool
    error_category: str | None
    duration_seconds: float
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "audit_id", _require_int(self.audit_id, "audit_id", minimum=1))
        require_opaque_token(self.capability, name="capability", max_length=64)
        require_opaque_token(self.operation, name="operation", max_length=128)
        require_opaque_token(self.target_type, name="target_type", max_length=64)
        _require_bool(self.success, "success")
        if self.error_category is not None:
            require_opaque_token(self.error_category, name="error_category", max_length=64)
        if type(self.duration_seconds) is bool or type(self.duration_seconds) not in (int, float):
            raise TypeError("duration_seconds must be a real number")
        duration = float(self.duration_seconds)
        if not math.isfinite(duration) or duration < 0.0:
            raise ValueError("duration_seconds must be a finite number >= 0")
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(
            self, "created_at", require_aware_datetime(self.created_at, name="created_at")
        )


@final
@dataclass(frozen=True, slots=True)
class ConfigSpecView:
    key: str
    category: str
    apply_mode: str
    value_type: str
    mutable: bool
    sensitive: bool
    configured: bool

    def __post_init__(self) -> None:
        require_opaque_token(self.key, name="key", max_length=128)
        category = self.category.strip() or "general"
        require_opaque_token(category, name="category", max_length=64)
        object.__setattr__(self, "category", category)
        require_opaque_token(self.apply_mode, name="apply_mode", max_length=32)
        require_opaque_token(self.value_type, name="value_type", max_length=16)
        _require_bool(self.mutable, "mutable")
        _require_bool(self.sensitive, "sensitive")
        _require_bool(self.configured, "configured")


@final
@dataclass(frozen=True, slots=True)
class EffectiveConfigView:
    key: str
    source: str
    scope_type: str
    apply_mode: str
    configured: bool
    pending_restart: bool
    version: int | None
    value: str | int | float | bool | None
    owner_kind: ConfigOwnerKind | None = None
    person_id: PersonId | None = None
    space_id: SpaceId | None = None
    owner_resolution: IdentityResolution | None = None

    def __post_init__(self) -> None:
        require_opaque_token(self.key, name="key", max_length=128)
        require_opaque_token(self.source, name="source", max_length=32)
        require_opaque_token(self.scope_type, name="scope_type", max_length=16)
        require_opaque_token(self.apply_mode, name="apply_mode", max_length=32)
        _require_bool(self.configured, "configured")
        _require_bool(self.pending_restart, "pending_restart")
        if self.version is not None:
            object.__setattr__(self, "version", _require_int(self.version, "version", minimum=1))
        if self.apply_mode == "secret" and self.value is not None:
            raise ValueError("secret config cannot carry a value")
        _validate_config_owner(
            owner_kind=self.owner_kind,
            person_id=self.person_id,
            space_id=self.space_id,
            resolution=self.owner_resolution,
        )


@final
@dataclass(frozen=True, slots=True)
class ConfigOverrideView:
    override_id: int
    key: str
    scope_type: str
    owner_kind: ConfigOwnerKind
    person_id: PersonId | None
    space_id: SpaceId | None
    resolution: IdentityResolution
    apply_mode: str
    configured: bool
    version: int
    value: str | int | float | bool | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "override_id", _require_int(self.override_id, "override_id", minimum=1)
        )
        require_opaque_token(self.key, name="key", max_length=128)
        require_opaque_token(self.scope_type, name="scope_type", max_length=16)
        require_opaque_token(self.apply_mode, name="apply_mode", max_length=32)
        _require_bool(self.configured, "configured")
        object.__setattr__(self, "version", _require_int(self.version, "version", minimum=1))
        if self.apply_mode == "secret" and self.value is not None:
            raise ValueError("secret config cannot carry a value")
        _validate_config_owner(
            owner_kind=self.owner_kind,
            person_id=self.person_id,
            space_id=self.space_id,
            resolution=self.resolution,
            required=True,
        )


@final
@dataclass(frozen=True, slots=True)
class MemoryFactView:
    fact_id: int
    scope_type: str
    kind: str
    category: str
    status: str
    content: str | None
    excerpt: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_id", _require_int(self.fact_id, "fact_id", minimum=1))
        require_opaque_token(self.scope_type, name="scope_type", max_length=16)
        require_opaque_token(self.kind, name="kind", max_length=16)
        if type(self.category) is not str or not self.category or len(self.category) > 64:
            raise ValueError("category must be a nonempty display token")
        require_opaque_token(self.status, name="status", max_length=16)
        if self.content is not None and type(self.content) is not str:
            raise TypeError("content must be a str or None")
        if self.excerpt is not None and type(self.excerpt) is not str:
            raise TypeError("excerpt must be a str or None")


@final
@dataclass(frozen=True, slots=True)
class MemoryEvidenceView:
    evidence_id: int
    fact_id: int
    relation: str
    excerpt: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_id", _require_int(self.evidence_id, "evidence_id", minimum=1)
        )
        object.__setattr__(self, "fact_id", _require_int(self.fact_id, "fact_id", minimum=1))
        require_opaque_token(self.relation, name="relation", max_length=32)
        if self.excerpt is not None and type(self.excerpt) is not str:
            raise TypeError("excerpt must be a str or None")


@final
@dataclass(frozen=True, slots=True)
class MemoryJobView:
    job_id: str
    kind: str
    status: str
    operation: OperationRef

    def __post_init__(self) -> None:
        require_opaque_token(self.job_id, name="job_id", max_length=128)
        require_opaque_token(self.kind, name="kind", max_length=32)
        require_opaque_token(self.status, name="status", max_length=16)
        if type(self.operation) is not OperationRef:
            raise TypeError("operation must be OperationRef")


@final
@dataclass(frozen=True, slots=True)
class MemoryHealthView:
    index: str
    embedding: str
    consistency: str

    def __post_init__(self) -> None:
        require_opaque_token(self.index, name="index", max_length=32)
        require_opaque_token(self.embedding, name="embedding", max_length=32)
        require_opaque_token(self.consistency, name="consistency", max_length=32)


@final
@dataclass(frozen=True, slots=True)
class AutomationView:
    automation_id: int
    name: str
    status: str
    run_count: int
    script_hash: str
    target_kind: str
    target_id: str
    route_state: str  # missing | paused | configured (not live health)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "automation_id", _require_int(self.automation_id, "automation_id", minimum=1)
        )
        if type(self.name) is not str or not self.name or len(self.name) > 128:
            raise ValueError("name must be a nonempty display token")
        require_opaque_token(self.status, name="status", max_length=16)
        object.__setattr__(self, "run_count", _require_int(self.run_count, "run_count"))
        require_opaque_token(self.script_hash, name="script_hash", max_length=64)
        require_opaque_token(self.target_kind, name="target_kind", max_length=16)
        require_opaque_token(self.target_id, name="target_id", max_length=64)
        require_opaque_token(self.route_state, name="route_state", max_length=16)


@final
@dataclass(frozen=True, slots=True)
class PluginView:
    plugin_id: str
    name: str
    version: str
    status: str
    enabled: bool

    def __post_init__(self) -> None:
        require_opaque_token(self.plugin_id, name="plugin_id", max_length=128)
        if type(self.name) is not str or not self.name or len(self.name) > 128:
            raise ValueError("name must be a nonempty display token")
        require_opaque_token(self.version, name="version", max_length=64)
        require_opaque_token(self.status, name="status", max_length=32)
        _require_bool(self.enabled, "enabled")


@final
@dataclass(frozen=True, slots=True)
class McpServerView:
    server_id: str
    enabled: bool
    healthy: bool
    tool_count: int

    def __post_init__(self) -> None:
        require_opaque_token(self.server_id, name="server_id", max_length=128)
        _require_bool(self.enabled, "enabled")
        _require_bool(self.healthy, "healthy")
        object.__setattr__(self, "tool_count", _require_int(self.tool_count, "tool_count"))


@final
@dataclass(frozen=True, slots=True)
class EmojiSpaceEnablementView:
    space_id: SpaceId | None
    resolution: IdentityResolution
    enabled: bool

    def __post_init__(self) -> None:
        if self.space_id is not None and type(self.space_id) is not SpaceId:
            raise TypeError("space_id must be SpaceId or None")
        if type(self.resolution) is not IdentityResolution:
            raise TypeError("resolution must be IdentityResolution")
        _require_bool(self.enabled, "enabled")
        if self.resolution is IdentityResolution.CANONICAL and self.space_id is None:
            raise ValueError("canonical space enablement requires space_id")
        if self.resolution is not IdentityResolution.CANONICAL and self.space_id is not None:
            raise ValueError("non-canonical space enablement cannot carry space_id")


@final
@dataclass(frozen=True, slots=True)
class EmojiAssetView:
    asset_id: str
    status: str
    enabled: bool
    global_enabled: bool | None = None
    space_enablements: tuple[EmojiSpaceEnablementView, ...] = ()
    first_seen_person_id: PersonId | None = None
    first_seen_space_id: SpaceId | None = None

    def __post_init__(self) -> None:
        require_opaque_token(self.asset_id, name="asset_id", max_length=128)
        require_opaque_token(self.status, name="status", max_length=32)
        _require_bool(self.enabled, "enabled")
        if self.global_enabled is not None:
            _require_bool(self.global_enabled, "global_enabled")
        if isinstance(self.space_enablements, (str, bytes)):
            raise TypeError("space_enablements must be a sequence")
        enablements = tuple(self.space_enablements)
        for item in enablements:
            if type(item) is not EmojiSpaceEnablementView:
                raise TypeError("space_enablements must contain EmojiSpaceEnablementView")
        object.__setattr__(self, "space_enablements", enablements)
        if self.first_seen_person_id is not None and (
            type(self.first_seen_person_id) is not PersonId
        ):
            raise TypeError("first_seen_person_id must be PersonId or None")
        if self.first_seen_space_id is not None and type(self.first_seen_space_id) is not SpaceId:
            raise TypeError("first_seen_space_id must be SpaceId or None")


@final
@dataclass(frozen=True, slots=True)
class SpeechProfileView:
    profile_id: str
    status: str
    enabled: bool

    def __post_init__(self) -> None:
        require_opaque_token(self.profile_id, name="profile_id", max_length=128)
        require_opaque_token(self.status, name="status", max_length=32)
        _require_bool(self.enabled, "enabled")
