"""Current projection chunk invalidation, frozen with migration 0133."""

from yuki.conversation.projection_compact_schema import PROJECTION_TRIGGERS_0119
from yuki.conversation.projection_revision_schema import EVENT_METADATA_COLUMNS_0082

PROJECTION_TRIGGERS_0133 = {
    name: statement.replace("payload_json='[]',", "tail_ordinal=0, item_count=0,")
    for name, statement in PROJECTION_TRIGGERS_0119.items()
}
# Event/identity enrichment only advances its existing canonical source revision.
# One revision trigger owns ordinary invalidation. Reset and deletion establish
# their precise reason first, so that trigger observes the already retired head.
for _name in (
    "prompt_projection_event_update",
    "prompt_projection_audio_update",
    "prompt_projection_owner_update",
    "prompt_projection_event_metadata_update",
):
    PROJECTION_TRIGGERS_0133[_name] = (
        PROJECTION_TRIGGERS_0119[_name].split("UPDATE prompt_projections", 1)[0] + "END"
    )
PROJECTION_TRIGGERS_0133["prompt_projection_reset"] = """
CREATE TRIGGER prompt_projection_reset
AFTER UPDATE OF generation, starts_after_event_id ON canonical_conversations
WHEN OLD.generation IS NOT NEW.generation
    OR OLD.starts_after_event_id IS NOT NEW.starts_after_event_id
BEGIN
    UPDATE prompt_projections
    SET tail_ordinal=0, item_count=0, byte_size=2, revision=revision+1,
        invalidated_reason='reset'
    WHERE conversation_id=NEW.id;
    UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
    WHERE id=NEW.id;
END"""
PROJECTION_TRIGGERS_0133["prompt_projection_event_delete"] = """
CREATE TRIGGER prompt_projection_event_delete AFTER DELETE ON chat_events
BEGIN
    UPDATE prompt_projections
    SET tail_ordinal=0, item_count=0, byte_size=2, revision=revision+1,
        invalidated_reason='deleted_event'
    WHERE conversation_id=OLD.canonical_conversation_id;
    UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
    WHERE id=OLD.canonical_conversation_id;
END"""
PROJECTION_TRIGGERS_0133["prompt_projection_retire_chunks"] = """
CREATE TRIGGER prompt_projection_retire_chunks
AFTER UPDATE OF epoch_id, invalidated_reason ON prompt_projections
WHEN OLD.epoch_id IS NOT NEW.epoch_id OR NEW.invalidated_reason IS NOT NULL
BEGIN
    DELETE FROM prompt_projection_chunks WHERE view_key=OLD.view_key;
END"""
PROJECTION_TRIGGERS_0133["prompt_projection_source_revision"] = """
CREATE TRIGGER prompt_projection_source_revision
AFTER UPDATE OF prompt_source_revision ON canonical_conversations
WHEN OLD.prompt_source_revision IS NOT NEW.prompt_source_revision
BEGIN
    UPDATE prompt_projections
    SET tail_ordinal=0, item_count=0, byte_size=2, revision=revision+1,
        invalidated_reason='source_changed'
    WHERE conversation_id=NEW.id AND source_revision IS NOT NEW.prompt_source_revision
      AND invalidated_reason IS NULL;
END"""

# Current events include proven physical-account authors. Historical migration
# definitions remain frozen; only the current metadata closure grows here.
EVENT_METADATA_COLUMNS_CURRENT = (*EVENT_METADATA_COLUMNS_0082, "author_account_id")
_metadata_columns = ", ".join(EVENT_METADATA_COLUMNS_CURRENT)
_metadata_changed = " OR ".join(
    f"OLD.{name} IS NOT NEW.{name}" for name in EVENT_METADATA_COLUMNS_CURRENT
)
PROJECTION_TRIGGERS_CURRENT = {
    **PROJECTION_TRIGGERS_0133,
    "prompt_projection_event_metadata_update": f"""CREATE TRIGGER
    prompt_projection_event_metadata_update AFTER UPDATE OF {_metadata_columns} ON chat_events
    WHEN {_metadata_changed}
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);
    END""",
}
