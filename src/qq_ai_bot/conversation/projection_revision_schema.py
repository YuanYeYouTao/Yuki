"""Revision 0082 closes existing source fingerprints over event metadata and moved rollups."""

from qq_ai_bot.asr.schema import PROJECTION_TRIGGERS_0055

# The original content/audio triggers cover the remaining event columns. Keep
# this list frozen with 0082, including identity and all admitted-event metadata.
EVENT_METADATA_COLUMNS_0082 = (
    "id",
    "bot_user_id",
    "platform_message_id",
    "scope_type",
    "group_id",
    "private_peer_user_id",
    "sender_user_id",
    "sender_nickname",
    "sender_group_card",
    "direction",
    "event_kind",
    "source_plugin_id",
    "external_source",
    "external_event_key",
    "external_event_type",
    "external_resume_wait",
    "external_target_id",
    "reply_to_message_id",
    "reply_to_event_id",
    "origin",
    "automation_id",
    "automation_run_id",
    "occurred_at",
    "observed_at",
    "canonical_event_id",
    "author_kind",
    "author_person_id",
    "author_presence_id",
    "ingress_presence_id",
    "utterance_fingerprint",
    "ingress_provider",
    "ingress_gateway_instance_id",
    "caused_by_event_id",
)

_columns = ", ".join(EVENT_METADATA_COLUMNS_0082)
_changed = " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in EVENT_METADATA_COLUMNS_0082)
PROJECTION_ADDITIONS_0082 = {
    "prompt_projection_owner_update": """CREATE TRIGGER prompt_projection_owner_update
    AFTER UPDATE OF kind, person_id, space_id ON canonical_conversations
    WHEN OLD.kind IS NOT NEW.kind OR OLD.person_id IS NOT NEW.person_id
        OR OLD.space_id IS NOT NEW.space_id
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id = NEW.id;
        UPDATE prompt_projections SET payload_json='[]', byte_size=2, revision=revision+1,
            invalidated_reason='read_scope_changed'
        WHERE conversation_id = NEW.id;
    END""",
    "prompt_projection_event_metadata_update": f"""CREATE TRIGGER
    prompt_projection_event_metadata_update AFTER UPDATE OF {_columns} ON chat_events
    WHEN {_changed}
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);
        UPDATE prompt_projections SET payload_json='[]', byte_size=2, revision=revision+1,
            invalidated_reason='source_changed'
        WHERE conversation_id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);
    END""",
}

for _table in (
    "canonical_conversation_rollups",
    "canonical_conversation_rollup_emergency_overlays",
):
    _name = f"prompt_projection_{_table}_move"
    PROJECTION_ADDITIONS_0082[_name] = f"""CREATE TRIGGER {_name}
    AFTER UPDATE OF conversation_id ON {_table}
    WHEN OLD.conversation_id IS NOT NEW.conversation_id
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id = OLD.conversation_id;
        UPDATE prompt_projections SET payload_json='[]', byte_size=2, revision=revision+1,
            invalidated_reason='rollup'
        WHERE conversation_id = OLD.conversation_id;
    END"""

PROJECTION_TRIGGERS_CURRENT = {**PROJECTION_TRIGGERS_0055, **PROJECTION_ADDITIONS_0082}
