"""Persistence-free value types for identity backfill planning and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from qq_ai_bot.identity.inventory import (
    IDENTITY_PLATFORM,
    INVENTORY_VERSION,
    AccountClass,
    ConflictCategory,
    ConflictKind,
    SpaceClass,
)

BackfillMode = Literal["dry_run", "apply"]
BackfillStatus = Literal["succeeded", "conflicted", "failed"]


@dataclass(frozen=True, slots=True)
class BackfillSettingsInput:
    """Classification inputs from Settings, already parsed into sets."""

    superusers: frozenset[str]
    enabled_groups: frozenset[str]
    ignored_bot_users: frozenset[str]


@dataclass(frozen=True, slots=True)
class CanonicalPersonRow:
    id: str
    enabled: bool
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class IdentityBindingRow:
    id: str
    person_id: str
    platform: str
    external_account_id: str
    display_name: str
    status: str
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CanonicalSpaceRow:
    id: str
    name: str
    enabled: bool
    autonomous_enabled: bool
    require_mention: bool
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SpaceBindingRow:
    id: str
    space_id: str
    platform: str
    external_space_id: str
    display_name: str
    status: str
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PresenceRow:
    id: str
    platform: str
    external_account_id: str
    enabled: bool
    ingest_eligible: bool
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PeopleRow:
    user_id: str
    nickname: str
    enabled: bool
    is_bot: bool
    canonical_person_id: str | None


@dataclass(frozen=True, slots=True)
class GroupRow:
    group_id: str
    name: str
    enabled: bool
    require_mention: bool
    autonomous_enabled: bool
    canonical_space_id: str | None


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    external_id: str
    sources: frozenset[str]
    yuki_self: bool
    ignored_bot: bool
    legacy_is_bot: bool
    human_sender: bool
    private_peer: bool
    member: bool
    superuser: bool
    people_human: bool
    supporting_person: bool
    strong_person: bool
    nickname: str
    existing_person_id: str | None
    existing_binding_person_id: str | None
    existing_presence_id: str | None
    shadow_person_ids: frozenset[str]
    shadow_presence_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class SpaceEvidence:
    external_id: str
    sources: frozenset[str]
    name: str
    enabled: bool
    autonomous_enabled: bool
    require_mention: bool
    has_groups_row: bool
    existing_space_id: str | None
    existing_binding_space_id: str | None
    shadow_space_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class IdentityConflict:
    subject_kind: Literal["account", "space"]
    conflict_kind: ConflictKind
    error_category: ConflictCategory
    platform: str
    external_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class AccountDecision:
    external_id: str
    classification: AccountClass
    sources: frozenset[str]
    person_id: str | None
    binding_id: str | None
    presence_id: str | None
    display_name: str
    create_person: bool
    create_binding: bool
    create_presence: bool


@dataclass(frozen=True, slots=True)
class SpaceDecision:
    external_id: str
    classification: SpaceClass
    sources: frozenset[str]
    space_id: str
    binding_id: str
    name: str
    enabled: bool
    autonomous_enabled: bool
    require_mention: bool
    display_name: str
    create_space: bool
    create_binding: bool


@dataclass(frozen=True, slots=True)
class ShadowAssignment:
    table: str
    row_key: tuple[tuple[str, object], ...]
    column: str
    value: str
    current: str | None
    fillable: bool = True


@dataclass(frozen=True, slots=True)
class MemoryOwnerAssignment:
    table: str
    row_id: int
    values: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class MemoryOwnerCounts:
    jobs: int = 0
    receipts: int = 0
    reflection_states: int = 0
    reflection_runs: int = 0
    dream_clusters: int = 0
    facts_verified: int = 0
    automation_targets: int = 0
    plugin_targets: int = 0


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    accounts: tuple[AccountDecision, ...]
    spaces: tuple[SpaceDecision, ...]
    conflicts: tuple[IdentityConflict, ...]
    shadows: tuple[ShadowAssignment, ...]
    skipped_external_bots: int
    source_fingerprint: str
    processed_subjects: int
    memory_owners: tuple[MemoryOwnerAssignment, ...] = ()
    memory_owner_counts: MemoryOwnerCounts = MemoryOwnerCounts()
    automation_targets: tuple[MemoryOwnerAssignment, ...] = ()
    plugin_targets: tuple[ShadowAssignment, ...] = ()
    event_authors: tuple[MemoryOwnerAssignment, ...] = ()


@dataclass(frozen=True, slots=True)
class BackfillCounts:
    processed: int
    persons: int
    identity_bindings: int
    spaces: int
    space_bindings: int
    presences: int
    conflicts: int
    skipped: int
    shadows_filled: int
    person_class: int
    yuki_presence_class: int
    external_bot_class: int
    space_class: int
    memory_job_owners: int = 0
    memory_receipt_owners: int = 0
    memory_reflection_state_owners: int = 0
    memory_reflection_run_owners: int = 0
    memory_dream_cluster_owners: int = 0
    memory_facts_verified: int = 0
    automation_targets: int = 0
    plugin_targets: int = 0
    event_authors: int = 0


@dataclass(frozen=True, slots=True)
class ConflictReport:
    subject_kind: str
    conflict_kind: str
    error_category: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class BackfillReport:
    mode: BackfillMode
    status: BackfillStatus
    business_diff: int
    source_fingerprint: str
    inventory_version: str
    platform: str
    counts: BackfillCounts
    conflicts: tuple[ConflictReport, ...]
    run_recorded: bool = False
    error_category: str | None = None


def failed_report(mode: BackfillMode, category: str) -> BackfillReport:
    """Sanitized failed report. Never includes paths, urls, or raw ids."""

    return BackfillReport(
        mode=mode,
        status="failed",
        business_diff=0,
        source_fingerprint="",
        inventory_version=INVENTORY_VERSION,
        platform=IDENTITY_PLATFORM,
        counts=BackfillCounts(
            processed=0,
            persons=0,
            identity_bindings=0,
            spaces=0,
            space_bindings=0,
            presences=0,
            conflicts=0,
            skipped=0,
            shadows_filled=0,
            person_class=0,
            yuki_presence_class=0,
            external_bot_class=0,
            space_class=0,
        ),
        conflicts=(),
        run_recorded=False,
        error_category=category,
    )


@dataclass
class MutableAccountEvidence:
    sources: set[str] = field(default_factory=set)
    yuki_self: bool = False
    ignored_bot: bool = False
    legacy_is_bot: bool = False
    human_sender: bool = False
    private_peer: bool = False
    member: bool = False
    superuser: bool = False
    people_human: bool = False
    supporting_person: bool = False
    strong_person: bool = False
    nickname: str = ""
    existing_person_id: str | None = None
    existing_binding_person_id: str | None = None
    existing_presence_id: str | None = None
    shadow_person_ids: set[str] = field(default_factory=set)
    shadow_presence_ids: set[str] = field(default_factory=set)


@dataclass
class MutableSpaceEvidence:
    sources: set[str] = field(default_factory=set)
    name: str = ""
    enabled: bool = True
    autonomous_enabled: bool = True
    require_mention: bool = True
    has_groups_row: bool = False
    existing_space_id: str | None = None
    existing_binding_space_id: str | None = None
    shadow_space_ids: set[str] = field(default_factory=set)
