"""Frozen inventory of legacy people/groups FK dependencies removed at 0048.

conversation_scopes.id children stay. Only FKs that required a people/groups
carrier row are rebuilt. This module is persistence-free.
"""

from __future__ import annotations

from typing import Final

LEGACY_CARRIER_PARENTS: Final[frozenset[str]] = frozenset({"people", "groups"})

# table.column -> parent.column. conversation_scopes.* people/groups FKs included.
LEGACY_CARRIER_FOREIGN_KEYS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("automations", "creator_user_id", "people", "user_id"),
    ("chat_events", "group_id", "groups", "group_id"),
    ("chat_events", "private_peer_user_id", "people", "user_id"),
    ("chat_events", "sender_user_id", "people", "user_id"),
    ("conversation_scopes", "bot_user_id", "people", "user_id"),
    ("conversation_scopes", "group_id", "groups", "group_id"),
    ("conversation_scopes", "private_peer_user_id", "people", "user_id"),
    ("emoji_assets", "first_seen_group_id", "groups", "group_id"),
    ("emoji_assets", "first_seen_user_id", "people", "user_id"),
    ("memberships", "group_id", "groups", "group_id"),
    ("memberships", "user_id", "people", "user_id"),
    ("memory_dream_runs", "created_by_user_id", "people", "user_id"),
    ("memory_evidence", "source_speaker_user_id", "people", "user_id"),
    ("memory_fact_state_events", "actor_user_id", "people", "user_id"),
    ("memory_facts", "group_id", "groups", "group_id"),
    ("memory_facts", "subject_user_id", "people", "user_id"),
    ("memory_mutation_receipts", "current_group_id", "groups", "group_id"),
    ("memory_mutation_receipts", "executed_by_bot_user_id", "people", "user_id"),
    ("memory_mutation_receipts", "trigger_actor_user_id", "people", "user_id"),
    ("memory_rebuild_proposals", "group_id", "groups", "group_id"),
    ("memory_rebuild_proposals", "reviewed_by_user_id", "people", "user_id"),
    ("memory_rebuild_proposals", "subject_user_id", "people", "user_id"),
    ("memory_rebuild_runs", "created_by_user_id", "people", "user_id"),
    ("memory_tool_receipts", "bot_user_id", "people", "user_id"),
    ("person_aliases", "user_id", "people", "user_id"),
    ("person_relationships", "user_id", "people", "user_id"),
    ("person_speech_preferences", "user_id", "people", "user_id"),
    ("person_time_settings", "user_id", "people", "user_id"),
    ("plugin_agent_messages", "sender_user_id", "people", "user_id"),
    ("plugin_agent_sessions", "owner_user_id", "people", "user_id"),
    ("plugin_background_target_grants", "created_by_user_id", "people", "user_id"),
    ("plugin_state", "subject_user_id", "people", "user_id"),
    ("relationship_events", "user_id", "people", "user_id"),
    ("relationship_jobs", "user_id", "people", "user_id"),
)

LEGACY_CARRIER_REBUILD_TABLES: Final[tuple[str, ...]] = tuple(
    sorted({item[0] for item in LEGACY_CARRIER_FOREIGN_KEYS})
)

# 0005-era FKs re-injected when copying stripped ORM tables for the 0005 stage.
C27_0005_RESTORED_CARRIER_FKS: Final[dict[str, tuple[tuple[str, str, str, str], ...]]] = {
    "person_aliases": (("user_id", "people", "user_id", "CASCADE"),),
    "memberships": (
        ("user_id", "people", "user_id", "CASCADE"),
        ("group_id", "groups", "group_id", "CASCADE"),
    ),
    "chat_events": (
        ("sender_user_id", "people", "user_id", "CASCADE"),
        ("private_peer_user_id", "people", "user_id", "CASCADE"),
        ("group_id", "groups", "group_id", "CASCADE"),
    ),
}
