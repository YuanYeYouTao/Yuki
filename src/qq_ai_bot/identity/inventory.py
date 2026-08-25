"""Frozen C7 identity-backfill source inventory and shadow policy.

This module is persistence-free. It names sources, evidence, and fill/NULL
rules so later commits do not rediscover identity by guessing. Platform is
the project's already-frozen lowercase identity token, never a gateway,
provider, connection, or plugin id.
"""

from __future__ import annotations

from typing import Final, Literal

IDENTITY_PLATFORM: Final[str] = "qq"
INVENTORY_VERSION: Final[str] = "c7-preflight-v1"

# Strong person evidence can collide with Yuki/bot roles. Weak evidence cannot:
# third-party bots send messages and sit in groups; Yuki may have membership rows.
STRONG_PERSON_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "settings.superusers",
        "people.user_id.human",
        "automations.creator_user_id",
        "plugin_background_target_grants.created_by_user_id",
        "plugin_background_target_grants.target_id",
        "plugin_notification_outbox.target_id",
        "plugin_background_turn_jobs.target_id",
        "plugin_agent_sessions.owner_user_id",
        "plugin_config_values.scope_id",
        "runtime_config_overrides.scope_id",
        "plugin_agent_messages.sender_user_id",
    }
)
WEAK_PERSON_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "chat_events.sender_user_id",
        "chat_events.private_peer_user_id",
        "conversation_scopes.private_peer_user_id",
        "memberships.user_id",
        "person_aliases.user_id",
        "plugin_state.subject_user_id",
        "emoji_assets.first_seen_user_id",
        "emoji_usage_events.actor_user_id",
    }
)
HUMAN_PLUGIN_MESSAGE_ROLES: Final[frozenset[str]] = frozenset({"user"})

AccountClass = Literal["person", "yuki_presence", "external_bot"]
SpaceClass = Literal["space"]
ConflictKind = Literal["ambiguous_identity", "unclassified"]
ConflictCategory = Literal[
    "yuki_and_person",
    "yuki_and_external_bot",
    "person_and_external_bot",
    "canonical_kind_mismatch",
    "canonical_owner_mismatch",
    "populated_merge_forbidden",
    "unclassified",
]


ACCOUNT_SOURCE_INVENTORY: tuple[tuple[str, str, str], ...] = (
    (
        "settings.superusers",
        "configured SUPERUSERS external account ids",
        "person_evidence; may create Person+Binding without a people row",
    ),
    (
        "settings.ignored_bot_users",
        "configured IGNORED_BOT_USERS",
        "external_bot; never Person/relationship/person-memory",
    ),
    (
        "people.user_id",
        "legacy person carrier",
        "is_bot=0 is person_evidence; is_bot=1 is third-party bot unless the "
        "same id is only a Yuki self",
    ),
    (
        "chat_events.bot_user_id",
        "persisted Yuki self_id for a ledger event",
        "yuki_self; Presence only when no person/external-bot evidence",
    ),
    (
        "chat_events.sender_user_id",
        "event sender",
        "weak person evidence when sender != that event's bot_user_id; bots may send",
    ),
    (
        "chat_events.private_peer_user_id",
        "private conversation peer",
        "weak person evidence; a strong human claim vs Yuki/bot is a role conflict",
    ),
    (
        "conversation_scopes.bot_user_id",
        "scope-owned Yuki self_id",
        "yuki_self",
    ),
    (
        "conversation_scopes.private_peer_user_id",
        "scope private peer",
        "person_evidence",
    ),
    (
        "memberships.user_id",
        "group member",
        "weak person evidence; ignored when the account is already external_bot/yuki",
    ),
    (
        "person_aliases.user_id",
        "alias owner",
        "supporting person row; classification still follows people/settings",
    ),
    (
        "automations.bot_user_id",
        "automation execution presence",
        "yuki_self; never an author/Person",
    ),
    (
        "automations.creator_user_id",
        "automation creator",
        "person_evidence when classified Person",
    ),
    (
        "plugin_background_target_grants.bot_user_id",
        "grant delivery presence",
        "yuki_self",
    ),
    (
        "plugin_background_target_grants.created_by_user_id",
        "grant creator",
        "person_evidence",
    ),
    (
        "plugin_background_target_grants.target_id",
        "private grant target",
        "person_evidence when target_type=private",
    ),
    (
        "plugin_notification_outbox.bot_user_id",
        "outbox delivery presence",
        "yuki_self",
    ),
    (
        "plugin_notification_outbox.target_id",
        "private outbox target",
        "person_evidence when target_type=private",
    ),
    (
        "plugin_background_turn_jobs.bot_user_id",
        "background-turn presence",
        "yuki_self",
    ),
    (
        "plugin_background_turn_jobs.target_id",
        "private turn target",
        "person_evidence when target_type=private",
    ),
    (
        "plugin_state.subject_user_id",
        "optional plugin subject",
        "person_evidence when present",
    ),
    (
        "plugin_agent_sessions.owner_user_id",
        "session owner",
        "person_evidence when present",
    ),
    (
        "plugin_agent_messages.sender_user_id",
        "human session author",
        "strong person evidence only for reliable human roles; assistant/tool never Person/shadow",
    ),
    (
        "identity_bindings.external_account_id",
        "canonical Person binding natural key",
        "Binding-only is Person preconfiguration; Binding+Presence is canonical_kind_mismatch",
    ),
    (
        "presences.external_account_id",
        "canonical Yuki presence natural key",
        "Presence-only is Yuki preconfiguration; Binding+Presence is canonical_kind_mismatch",
    ),
    (
        "plugin_config_values.scope_id",
        "user-scoped plugin config",
        "person_evidence when scope_type=user",
    ),
    (
        "runtime_config_overrides.scope_id",
        "user-scoped runtime config",
        "person_evidence when scope_type=user; updated_by is origin, not Person",
    ),
    (
        "emoji_assets.first_seen_user_id",
        "discovery provenance",
        "person_evidence when present",
    ),
    (
        "emoji_usage_events.actor_user_id",
        "usage actor",
        "person_evidence when present",
    ),
    (
        "memory_self_reflection_states.bot_user_id",
        "reflection cursor presence",
        "yuki_self; C21 owns later memory shadows",
    ),
    (
        "memory_self_reflection_runs.bot_user_id",
        "reflection run presence",
        "yuki_self",
    ),
)

SPACE_SOURCE_INVENTORY: tuple[tuple[str, str, str], ...] = (
    (
        "settings.enabled_groups",
        "configured ENABLED_GROUPS",
        "Space+Binding; must not insert groups/conversation_scopes",
    ),
    ("groups.group_id", "legacy group carrier", "Space+Binding; copy flags only on first create"),
    ("memberships.group_id", "membership space", "must already exist as groups due to FK"),
    ("chat_events.group_id", "event space", "supporting space evidence"),
    ("conversation_scopes.group_id", "scope space", "supporting space evidence"),
    (
        "plugin_config_values.scope_id",
        "group-scoped plugin config",
        "space evidence when scope_type=group",
    ),
    (
        "runtime_config_overrides.scope_id",
        "group-scoped runtime config",
        "space evidence when scope_type=group",
    ),
    (
        "plugin_agent_sessions.scope_id",
        "group session scope",
        "space evidence when scope_type=group",
    ),
    (
        "plugin_background_target_grants.target_id",
        "group grant target",
        "space evidence when target_type=group",
    ),
    (
        "plugin_notification_outbox.target_id",
        "group outbox target",
        "space evidence when target_type=group",
    ),
    (
        "plugin_background_turn_jobs.target_id",
        "group turn target",
        "space evidence when target_type=group",
    ),
    (
        "emoji_assets.first_seen_group_id",
        "discovery space",
        "space evidence when present",
    ),
    (
        "emoji_scope_states.scope_id",
        "group emoji enablement",
        "space evidence when scope_type=group",
    ),
    ("emoji_usage_events.group_id", "usage space", "space evidence when present"),
    ("person_aliases.group_scope", "alias space", "space evidence when group_scope != ''"),
    (
        "space_bindings.external_space_id",
        "canonical Space binding natural key",
        "SpaceBinding-only is Space preconfiguration; no fictional legacy group is inserted",
    ),
)

EXCLUDED_IDENTITY_SOURCES: tuple[tuple[str, str], ...] = (
    ("gateway/provider/connection/plugin_id", "not a platform identity"),
    ("runtime_config_overrides.updated_by", "mutation origin, not Person"),
    ("automation_versions.updated_by", "mutation origin, not Person"),
    ("relationship_events.actor_user_id", "manual-mutation origin; C5 already excludes it"),
    ("plugin_audit_events.actor_user_id", "operator origin; later control-plane audit"),
    ("admin_operation_events/agent_actions.actor_user_id", "operator audit, not Person"),
    ("chat_events.origin / source_plugin_id / ingress_provider", "origin/provider, not author"),
    ("speech/tool/model/web_search provider_id", "provider catalog, not Person"),
    ("container get_bots()/runtime self_id", "offline backfill reads persisted bot_user_id only"),
)

FILLABLE_SHADOWS: tuple[tuple[str, str, str], ...] = (
    ("people.canonical_person_id", "person", "only when account class is person"),
    ("groups.canonical_space_id", "space", "when a Space exists for the group"),
    ("person_aliases.canonical_person_id", "person", "alias owner classified person"),
    (
        "person_aliases.canonical_space_id",
        "space",
        "only when group_scope is nonempty and Space exists",
    ),
    ("memberships.canonical_person_id", "person", "member classified person"),
    ("memberships.canonical_space_id", "space", "group has Space"),
    (
        "person_relationships.canonical_person_id",
        "person",
        "target classified person; never external_bot/yuki",
    ),
    ("relationship_events.canonical_person_id", "person", "target classified person"),
    ("relationship_jobs.canonical_person_id", "person", "target classified person"),
    ("person_time_settings.canonical_person_id", "person", "owner classified person"),
    ("person_speech_preferences.canonical_person_id", "person", "owner classified person"),
    (
        "memory_facts.canonical_subject_person_id",
        "person",
        "scope person/person_group and subject classified person",
    ),
    (
        "memory_facts.canonical_subject_space_id",
        "space",
        "scope group/person_group and group has Space",
    ),
    (
        "memory_facts.canonical_visibility_person_id",
        "person",
        "self+private visibility owner classified person",
    ),
    (
        "memory_facts.canonical_visibility_space_id",
        "space",
        "self+group visibility owner has Space",
    ),
    (
        "automations.canonical_creator_person_id",
        "person",
        "creator classified person",
    ),
    (
        "automations.canonical_presence_id",
        "presence",
        "bot_user_id classified yuki_presence",
    ),
    (
        "plugin_config_values.canonical_person_id",
        "person",
        "scope_type=user and scope classified person",
    ),
    (
        "plugin_config_values.canonical_space_id",
        "space",
        "scope_type=group and scope has Space",
    ),
    (
        "plugin_state.canonical_person_id",
        "person",
        "subject classified person",
    ),
    (
        "plugin_agent_sessions.canonical_owner_person_id",
        "person",
        "owner classified person",
    ),
    (
        "plugin_agent_sessions.canonical_space_id",
        "space",
        "scope_type=group and scope has Space",
    ),
    (
        "plugin_agent_messages.canonical_sender_person_id",
        "person",
        "sender classified person",
    ),
    (
        "plugin_background_target_grants.canonical_target_person_id",
        "person",
        "target_type=private and target classified person",
    ),
    (
        "plugin_background_target_grants.canonical_target_space_id",
        "space",
        "target_type=group and target has Space",
    ),
    (
        "plugin_background_target_grants.canonical_created_by_person_id",
        "person",
        "creator classified person",
    ),
    (
        "plugin_background_target_grants.canonical_presence_id",
        "presence",
        "bot_user_id classified yuki_presence",
    ),
    (
        "plugin_notification_outbox.canonical_target_person_id",
        "person",
        "target_type=private and target classified person",
    ),
    (
        "plugin_notification_outbox.canonical_target_space_id",
        "space",
        "target_type=group and target has Space",
    ),
    (
        "plugin_notification_outbox.canonical_presence_id",
        "presence",
        "bot_user_id classified yuki_presence",
    ),
    (
        "plugin_background_turn_jobs.canonical_target_person_id",
        "person",
        "target_type=private and target classified person",
    ),
    (
        "plugin_background_turn_jobs.canonical_target_space_id",
        "space",
        "target_type=group and target has Space",
    ),
    (
        "plugin_background_turn_jobs.canonical_presence_id",
        "presence",
        "bot_user_id classified yuki_presence",
    ),
    (
        "runtime_config_overrides.canonical_person_id",
        "person",
        "scope_type=user and scope classified person",
    ),
    (
        "runtime_config_overrides.canonical_space_id",
        "space",
        "scope_type=group and scope has Space",
    ),
    (
        "emoji_assets.canonical_first_seen_person_id",
        "person",
        "first_seen user classified person",
    ),
    (
        "emoji_assets.canonical_first_seen_space_id",
        "space",
        "first_seen group has Space",
    ),
    (
        "emoji_scope_states.canonical_space_id",
        "space",
        "scope_type=group and scope has Space",
    ),
    (
        "emoji_usage_events.canonical_actor_person_id",
        "person",
        "actor classified person",
    ),
    ("emoji_usage_events.canonical_space_id", "space", "group has Space"),
)

DEFERRED_SHADOWS: tuple[tuple[str, str], ...] = (
    (
        "automations.canonical_target_person_id/space_id",
        "no typed target columns; script_json is not a reliable identity map (later commit)",
    ),
    (
        "runtime_turn_observations.canonical_person_id/space_id",
        "row has scope_type but no user/group key; hashing conversation_key is guessing",
    ),
    (
        "chat_events.canonical_event_id/canonical_conversation_id/"
        "author_person_id/author_presence_id/ingress_presence_id",
        "C4 event/conversation/author mapping; C26 cutover owns proven Conversation",
    ),
    (
        "conversation_scopes.canonical_conversation_id",
        "C4 conversation correlation; C26 cutover",
    ),
    (
        "plugin_notification_outbox.canonical_conversation_id",
        "C6 conversation correlation; C26 cutover",
    ),
    (
        "plugin_background_turn_jobs.canonical_conversation_id",
        "C6 conversation correlation; C26 cutover",
    ),
    (
        "speech_generations/tool_invocations/web_search_runs/"
        "model_invocations/reply_effect_events.canonical_conversation_id",
        "C6 conversation correlation; C26 cutover",
    ),
    (
        "runtime_turn_observations.canonical_conversation_id",
        "C6 conversation correlation; C26 cutover",
    ),
    (
        "memory jobs/evidence/reflection/dream/tool receipts",
        "C21 canonical memory ownership",
    ),
    (
        "canonical_conversations / aliases / routes / receipts",
        "C25/C26; C7 must not create them",
    ),
    (
        "identity_runtime_state v2 / source_fingerprint",
        "C7 stays v1; cutover later writes the epoch",
    ),
)

YUKI_SELF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("chat_events", "bot_user_id"),
    ("conversation_scopes", "bot_user_id"),
    ("automations", "bot_user_id"),
    ("plugin_background_target_grants", "bot_user_id"),
    ("plugin_notification_outbox", "bot_user_id"),
    ("plugin_background_turn_jobs", "bot_user_id"),
    ("memory_self_reflection_states", "bot_user_id"),
    ("memory_self_reflection_runs", "bot_user_id"),
)

# Capabilities C7 must see before planning. Extra future columns are allowed.
REQUIRED_C7_SCHEMA: dict[str, tuple[str, ...]] = {
    "persons": ("id", "enabled", "revision", "created_at", "updated_at"),
    "identity_bindings": (
        "id",
        "person_id",
        "platform",
        "external_account_id",
        "display_name",
        "status",
        "revision",
        "created_at",
        "updated_at",
    ),
    "spaces": (
        "id",
        "name",
        "enabled",
        "autonomous_enabled",
        "require_mention",
        "revision",
        "created_at",
        "updated_at",
    ),
    "space_bindings": (
        "id",
        "space_id",
        "platform",
        "external_space_id",
        "display_name",
        "status",
        "revision",
        "created_at",
        "updated_at",
    ),
    "presences": (
        "id",
        "platform",
        "external_account_id",
        "enabled",
        "ingest_eligible",
        "revision",
        "created_at",
        "updated_at",
    ),
    "identity_runtime_state": (
        "id",
        "state",
        "cutover_id",
        "source_fingerprint",
        "completed_at",
        "revision",
        "created_at",
        "updated_at",
    ),
    "identity_backfill_runs": (
        "mode",
        "status",
        "checkpoint",
        "processed_count",
        "persons_count",
        "identity_bindings_count",
        "spaces_count",
        "space_bindings_count",
        "presences_count",
        "conflicts_count",
        "skipped_count",
        "error_category",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    ),
    "identity_conflicts": (
        "platform",
        "external_id",
        "subject_kind",
        "conflict_kind",
        "status",
        "error_category",
        "resolved_at",
        "created_at",
        "updated_at",
    ),
    "people": ("user_id", "nickname", "enabled", "is_bot", "canonical_person_id"),
    "groups": (
        "group_id",
        "name",
        "enabled",
        "require_mention",
        "autonomous_enabled",
        "canonical_space_id",
    ),
    "memberships": ("user_id", "group_id", "canonical_person_id", "canonical_space_id"),
    "person_aliases": (
        "user_id",
        "group_scope",
        "canonical_person_id",
        "canonical_space_id",
    ),
    "chat_events": ("bot_user_id", "sender_user_id", "private_peer_user_id", "group_id"),
    "conversation_scopes": ("bot_user_id", "private_peer_user_id", "group_id"),
    "person_relationships": ("user_id", "canonical_person_id"),
    "relationship_events": ("id", "user_id", "canonical_person_id"),
    "relationship_jobs": ("id", "user_id", "canonical_person_id"),
    "person_time_settings": ("user_id", "canonical_person_id"),
    "person_speech_preferences": ("user_id", "canonical_person_id"),
    "memory_facts": (
        "id",
        "scope_type",
        "subject_user_id",
        "group_id",
        "visibility_type",
        "visibility_user_id",
        "visibility_group_id",
        "canonical_subject_person_id",
        "canonical_subject_space_id",
        "canonical_visibility_person_id",
        "canonical_visibility_space_id",
    ),
    "automations": (
        "id",
        "bot_user_id",
        "creator_user_id",
        "canonical_creator_person_id",
        "canonical_presence_id",
    ),
    "plugin_config_values": (
        "id",
        "scope_type",
        "scope_id",
        "canonical_person_id",
        "canonical_space_id",
    ),
    "plugin_state": ("id", "subject_user_id", "canonical_person_id"),
    "plugin_agent_sessions": (
        "session_id",
        "owner_user_id",
        "scope_type",
        "scope_id",
        "canonical_owner_person_id",
        "canonical_space_id",
    ),
    "plugin_agent_messages": (
        "id",
        "sender_user_id",
        "role",
        "canonical_sender_person_id",
    ),
    "plugin_background_target_grants": (
        "id",
        "bot_user_id",
        "created_by_user_id",
        "target_type",
        "target_id",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_created_by_person_id",
        "canonical_presence_id",
    ),
    "plugin_notification_outbox": (
        "id",
        "bot_user_id",
        "target_type",
        "target_id",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_presence_id",
    ),
    "plugin_background_turn_jobs": (
        "id",
        "bot_user_id",
        "target_type",
        "target_id",
        "canonical_target_person_id",
        "canonical_target_space_id",
        "canonical_presence_id",
    ),
    "runtime_config_overrides": (
        "id",
        "scope_type",
        "scope_id",
        "canonical_person_id",
        "canonical_space_id",
    ),
    "emoji_assets": (
        "id",
        "first_seen_user_id",
        "first_seen_group_id",
        "canonical_first_seen_person_id",
        "canonical_first_seen_space_id",
    ),
    "emoji_scope_states": ("id", "scope_type", "scope_id", "canonical_space_id"),
    "emoji_usage_events": (
        "id",
        "actor_user_id",
        "group_id",
        "canonical_actor_person_id",
        "canonical_space_id",
    ),
    "memory_self_reflection_states": ("bot_user_id",),
    "memory_self_reflection_runs": ("bot_user_id",),
}
