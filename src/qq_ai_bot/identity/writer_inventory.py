"""Frozen inventory of legacy identity writers.

AST coverage walks production modules for people/groups/memberships/
person_aliases/chat_events/conversation_scopes constructors, insert/update/
delete helpers, and raw SQL. A writer fixture missing from this inventory
fails. Epochs c8 and c20-c24 are wired. Complete-v2 runtime must not call
people/groups/conversation_scopes writers; helpers fail closed.
This module is persistence-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

WriterEpoch = Literal[
    "c8",
    "c7_offline",
    "c17",
    "c20",
    "c21",
    "c22",
    "c23",
    "c24",
    "tool",
]
WriterOp = Literal["insert", "update", "delete", "upsert"]

LEGACY_IDENTITY_TABLES: Final[frozenset[str]] = frozenset(
    {
        "people",
        "groups",
        "memberships",
        "person_aliases",
        "chat_events",
        "conversation_scopes",
    }
)
LEGACY_IDENTITY_MODELS: Final[dict[str, str]] = {
    "PersonModel": "people",
    "GroupModel": "groups",
    "MembershipModel": "memberships",
    "PersonAliasModel": "person_aliases",
    "ChatEventModel": "chat_events",
    "ConversationScopeModel": "conversation_scopes",
}
WRITER_INVENTORY_VERSION: Final[str] = "c8-dual-write-v1"


@dataclass(frozen=True, slots=True)
class WriterPoint:
    """Immutable inventory row."""

    module: str
    function: str
    tables: frozenset[str]
    ops: frozenset[str]
    epoch: WriterEpoch
    reason: str

    def __init__(
        self,
        module: str,
        function: str,
        tables: frozenset[str] | set[str] | tuple[str, ...],
        ops: frozenset[str] | set[str] | tuple[str, ...],
        epoch: WriterEpoch,
        reason: str,
    ) -> None:
        object.__setattr__(self, "module", module)
        object.__setattr__(self, "function", function)
        object.__setattr__(self, "tables", frozenset(tables))
        object.__setattr__(self, "ops", frozenset(ops))
        object.__setattr__(self, "epoch", epoch)
        object.__setattr__(self, "reason", reason)


_C8_ACTIVE_WRITERS: Final[tuple[WriterPoint, ...]] = (
    WriterPoint(
        "qq_ai_bot.persistence.repository_helpers",
        "_ensure_person",
        {"people"},
        {"insert", "update"},
        "c8",
        "first-seen people carrier; dual-write Person/Binding or keep bot shadow NULL",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.repository_helpers",
        "_ensure_group",
        {"groups"},
        {"insert", "update"},
        "c8",
        "first-seen groups carrier; dual-write Space/SpaceBinding",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "observe",
        {"people", "groups", "memberships", "person_aliases"},
        {"insert", "update", "upsert"},
        "c8",
        "observe person/group/membership/alias in one transaction; fill proven C5 shadows",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "_upsert_alias",
        {"person_aliases"},
        {"upsert"},
        "c8",
        "alias upsert; fill Person/Space shadows only when this write proves owners",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "set_enabled",
        {"people"},
        {"insert", "update"},
        "c8",
        "private enabled flag on people; identity foundation via _ensure_person; "
        "Person.enabled dual-write is C20",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "delete_person",
        {"people", "conversation_scopes", "chat_events", "memberships", "person_aliases"},
        {"delete", "update"},
        "c8",
        "forgetme: clear C4/C5/C6 Person FKs then application-cascade legacy rows",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "upsert",
        {"people", "groups", "memberships", "person_aliases"},
        {"insert", "update", "upsert"},
        "c8",
        "UserProfileRepository.upsert delegates to observe",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "set_enabled",
        {"groups"},
        {"insert", "update"},
        "c8",
        "group access switch; Space.enabled dual-write is C20",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "set_autonomous_enabled",
        {"groups"},
        {"insert", "update"},
        "c8",
        "autonomous switch on groups; Space.autonomous_enabled dual-write is C20",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "observe",
        {"groups"},
        {"insert", "update"},
        "c8",
        "first-seen group observation",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.scoped_event_uow",
        "append",
        {"chat_events", "people", "groups"},
        {"insert", "update"},
        "c8",
        "only runtime ledger insert; identity shadows + first-seen carriers",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.scoped_event_uow",
        "append_inbound",
        {"chat_events", "people", "groups"},
        {"insert", "update"},
        "c8",
        "inbound facade over append",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.scoped_event_uow",
        "append_external",
        {"chat_events", "people", "groups"},
        {"insert", "update"},
        "c8",
        "plugin/automation external event; author_kind stays yuki/person/external_bot/system",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.scoped_event_uow",
        "_append_complete_v2",
        {"chat_events"},
        {"insert", "update"},
        "c8",
        "complete-v2 canonical receipt+event; does not write people/groups/scopes",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.scoped_event_uow",
        "_append_legacy_v1_on_session",
        {"chat_events"},
        {"insert"},
        "c8",
        "v1-only extracted ledger insert; identity shadows via "
        "apply_event_identity_shadows; people/groups remain _ensure_identities; "
        "complete-v2 must never call it",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.scoped_event_uow",
        "append_new_generation_command",
        {"chat_events", "people", "groups"},
        {"insert", "update"},
        "c8",
        "/ai new ledger row; generation change is unchanged",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.scoped_event_uow",
        "set_visual_summary",
        {"chat_events"},
        {"update"},
        "c8",
        "visual_summary only; identity shadows stay untouched",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.scoped_event_uow",
        "_ensure_identities",
        {"people", "groups"},
        {"insert", "update"},
        "c8",
        "sender/bot/peer/group first-seen inside the ledger transaction",
    ),
    WriterPoint(
        "qq_ai_bot.conversation.rollup.repository",
        "get_or_create_scope_row",
        {"conversation_scopes"},
        {"upsert"},
        "c8",
        "v1-only legacy scope upsert; complete-v2 uses canonical conversations",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.event_repository",
        "append",
        {"chat_events"},
        {"insert"},
        "c8",
        "facade; delegates to ScopedEventLedgerUnitOfWork.append",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.event_repository",
        "append_inbound",
        {"chat_events"},
        {"insert"},
        "c8",
        "facade; delegates to ScopedEventLedgerUnitOfWork.append_inbound",
    ),
    WriterPoint(
        "qq_ai_bot.identity.dual_write",
        "sync_account",
        {"people"},
        {"update"},
        "c8",
        "fills people.canonical_person_id; never inserts people",
    ),
    WriterPoint(
        "qq_ai_bot.identity.dual_write",
        "sync_space",
        {"groups"},
        {"update"},
        "c8",
        "fills groups.canonical_space_id; never inserts groups",
    ),
    WriterPoint(
        "qq_ai_bot.identity.dual_write",
        "fill_membership_shadows",
        {"memberships"},
        {"update"},
        "c8",
        "C5 membership shadows when this write proves Person and Space",
    ),
    WriterPoint(
        "qq_ai_bot.identity.dual_write",
        "fill_alias_shadows",
        {"person_aliases"},
        {"update"},
        "c8",
        "C5 alias shadows when this write proves owners",
    ),
    WriterPoint(
        "qq_ai_bot.identity.dual_write",
        "apply_event_identity_shadows",
        {"chat_events"},
        {"update"},
        "c8",
        "author_kind person/yuki/external_bot/system; conversation/event/receipt stay NULL",
    ),
    WriterPoint(
        "qq_ai_bot.identity.dual_write",
        "forget_canonical_for_external_account",
        {"people", "chat_events", "conversation_scopes"},
        {"update", "delete"},
        "c8",
        "Person-level forget; extra Bindings with leftover legacy data fail-close",
    ),
    WriterPoint(
        "qq_ai_bot.identity.canonical_uow",
        "append_inbound",
        {"chat_events"},
        {"insert"},
        "c17",
        "v2 fence+receipt+canonical append; v1 ledger path unchanged",
    ),
    WriterPoint(
        "qq_ai_bot.automation.repository",
        "create",
        {"people"},
        {"insert"},
        "c8",
        "automation creator people row is an identity carrier; automation shadows are C22",
    ),
    WriterPoint(
        "qq_ai_bot.memory.repository",
        "create_fact",
        {"people", "groups", "memberships"},
        {"insert", "update"},
        "c8",
        "memory may first-see people/groups/membership; fact canonical columns are C21",
    ),
    WriterPoint(
        "qq_ai_bot.memory.rebuild.repository",
        "create_run",
        {"people"},
        {"insert", "update"},
        "c8",
        "rebuild ensure person carrier; memory ownership shadows are C21",
    ),
    WriterPoint(
        "qq_ai_bot.memory.rebuild.repository",
        "set_review",
        {"people"},
        {"insert", "update"},
        "c8",
        "rebuild review actor is a people carrier; memory ownership shadows are C21",
    ),
    WriterPoint(
        "qq_ai_bot.identity.dual_write",
        "sync_presence",
        frozenset(),
        {"insert"},
        "c8",
        "explicit Yuki bot_user_id/self_id creates Presence only; never people",
    ),
    WriterPoint(
        "qq_ai_bot.plugin_host.notification_repository",
        "grant_target",
        frozenset(),
        {"insert"},
        "c8",
        "explicit Yuki bot_user_id syncs Presence on the caller session; no fake people row",
    ),
    WriterPoint(
        "qq_ai_bot.plugin_host.session_repository",
        "create",
        {"people"},
        {"insert", "update"},
        "c8",
        "ensures owner people row; session/message shadows are C23",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.relationship_repository",
        "_ensure_row",
        {"people"},
        {"insert", "update"},
        "c8",
        "relationship ensure person carrier; relationship shadows are C20",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.relationship_repository",
        "get_or_create",
        {"people"},
        {"insert", "update"},
        "c8",
        "relationship get_or_create goes through _ensure_row",
    ),
    WriterPoint(
        "qq_ai_bot.identity.backfill_repository",
        "apply_plan",
        {"people", "groups", "memberships", "person_aliases"},
        {"update"},
        "c7_offline",
        "offline C7 sqlite3 backfill; C8 runtime must not open this connection",
    ),
    WriterPoint(
        "qq_ai_bot.memory.quality.performance",
        "_populate_core",
        {"people", "groups", "memberships", "chat_events"},
        {"insert"},
        "tool",
        "quality-lab raw SQL fixture; not a production writer",
    ),
    WriterPoint(
        "qq_ai_bot.identity.cutover_repository",
        "apply_plan",
        {"chat_events"},
        {"update"},
        "tool",
        "offline C27 sqlite3 event mapping; runtime must not open this connection",
    ),
    WriterPoint(
        "qq_ai_bot.identity.cutover_repository",
        "_retire_legacy_conversation_scope_carriers",
        {"conversation_scopes"},
        {"delete"},
        "tool",
        "offline C27 retires leftover v1 scope carriers after aliases/canonical rollups",
    ),
)

DEFERRED_IDENTITY_WRITERS: Final[tuple[WriterPoint, ...]] = ()

_C20_C24_ACTIVE_WRITERS: Final[tuple[WriterPoint, ...]] = (
    WriterPoint(
        "qq_ai_bot.persistence.repository_helpers",
        "_ensure_relationship",
        frozenset(),
        {"insert"},
        "c20",
        "person_relationships dual-writes canonical_person_id",
    ),
    WriterPoint(
        "qq_ai_bot.speech.preference_repository",
        "set",
        frozenset(),
        {"insert", "update"},
        "c20",
        "person_speech_preferences canonical_person_id",
    ),
    WriterPoint(
        "qq_ai_bot.time.service",
        "set_timezone",
        frozenset(),
        {"upsert"},
        "c20",
        "person_time_settings canonical_person_id",
    ),
    WriterPoint(
        "qq_ai_bot.memory.repository",
        "create_fact",
        frozenset(),
        {"insert"},
        "c21",
        "memory_facts canonical subject/visibility columns",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "_forget_c21_person_memory",
        frozenset(),
        {"delete"},
        "c21",
        "complete-v2 forgetme deletes Person-owned Memory live rows",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "_forget_c22_person_automations",
        frozenset(),
        {"delete"},
        "c22",
        "complete-v2 forgetme deletes Person-target and Person-created automations",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "_forget_c23_person_plugin",
        frozenset(),
        {"delete"},
        "c23",
        "complete-v2 forgetme deletes Person-owned plugin state/config/sessions/grants",
    ),
    WriterPoint(
        "qq_ai_bot.automation.repository",
        "create",
        frozenset(),
        {"insert"},
        "c22",
        "automations canonical creator/target/presence columns",
    ),
    WriterPoint(
        "qq_ai_bot.plugin_host.session_repository",
        "create",
        frozenset(),
        {"insert"},
        "c23",
        "plugin session/message canonical owners",
    ),
    WriterPoint(
        "qq_ai_bot.plugin_host.notification_repository",
        "grant_target",
        frozenset(),
        {"insert"},
        "c23",
        "grant/outbox/background canonical targets",
    ),
    WriterPoint(
        "qq_ai_bot.emoji.repository",
        "mark_used",
        frozenset(),
        {"insert"},
        "c24",
        "emoji first-seen/actor canonical columns",
    ),
    WriterPoint(
        "qq_ai_bot.admin.config_service",
        "save_with_audit",
        frozenset(),
        {"upsert"},
        "c24",
        "runtime_config_overrides canonical scope",
    ),
    WriterPoint(
        "qq_ai_bot.identity.canonical_projections",
        "resolve_canonical_time_setting",
        frozenset(),
        {"upsert"},
        "c20",
        "complete-v2 person_time_settings keyed by Person; no people row",
    ),
    WriterPoint(
        "qq_ai_bot.identity.canonical_projections",
        "resolve_canonical_speech_preference",
        frozenset(),
        {"upsert"},
        "c20",
        "complete-v2 person_speech_preferences keyed by Person; no people row",
    ),
    WriterPoint(
        "qq_ai_bot.identity.canonical_projections",
        "observe_canonical_person",
        {"memberships"},
        {"insert", "update"},
        "c20",
        "complete-v2 membership projection keyed by Person/Space; no people row",
    ),
    WriterPoint(
        "qq_ai_bot.identity.canonical_projections",
        "_upsert_alias",
        {"person_aliases"},
        {"upsert"},
        "c20",
        "complete-v2 alias projection keyed by canonical_person_id; no people row",
    ),
    WriterPoint(
        "qq_ai_bot.persistence.people_repository",
        "_delete_person_complete_v2",
        {"chat_events", "memberships", "person_aliases"},
        {"delete"},
        "c20",
        "complete-v2 forgetme deletes Person-owned projection rows; no conversation_scopes",
    ),
    WriterPoint(
        "qq_ai_bot.identity.shadows",
        "fill_relationship_event_shadows",
        frozenset(),
        {"update"},
        "c20",
        "complete-v2 relationship_events.canonical_person_id after insert",
    ),
    WriterPoint(
        "qq_ai_bot.identity.shadows",
        "fill_turn_observation_shadows",
        frozenset(),
        {"update"},
        "c24",
        "complete-v2 runtime_turn_observations canonical owners after insert",
    ),
)

C8_WRITER_INVENTORY: Final[tuple[WriterPoint, ...]] = (
    _C8_ACTIVE_WRITERS + _C20_C24_ACTIVE_WRITERS + DEFERRED_IDENTITY_WRITERS
)


def writer_keys(
    points: tuple[WriterPoint, ...] = C8_WRITER_INVENTORY,
) -> frozenset[tuple[str, str, str]]:
    """(module, function, table) keys that AST coverage must find or accept."""

    return frozenset(
        (item.module, item.function, table) for item in points for table in item.tables
    )
