"""Frozen revision 0055 definitions shared by migration and isolated databases."""

from qq_ai_bot.conversation.projection_schema import PROJECTION_TRIGGERS_0054

PROJECTION_TRIGGERS_0055 = {
    **PROJECTION_TRIGGERS_0054,
    "prompt_projection_audio_update": """CREATE TRIGGER prompt_projection_audio_update
    AFTER UPDATE OF audio_transcript ON chat_events
    WHEN OLD.audio_transcript IS NOT NEW.audio_transcript
    BEGIN
        UPDATE canonical_conversations
        SET prompt_source_revision=prompt_source_revision+1
        WHERE id = NEW.canonical_conversation_id;
        UPDATE prompt_projections
        SET payload_json='[]', byte_size=2, revision=revision+1, invalidated_reason='source_changed'
        WHERE conversation_id = NEW.canonical_conversation_id;
    END""",
}

CHAT_FTS_0055 = (
    """CREATE VIRTUAL TABLE IF NOT EXISTS chat_events_fts USING fts5(
        content, audio_transcript, content='chat_events', content_rowid='id', tokenize='trigram'
    )""",
    """CREATE TRIGGER IF NOT EXISTS chat_events_fts_ai AFTER INSERT ON chat_events BEGIN
        INSERT INTO chat_events_fts(rowid, content, audio_transcript)
        VALUES (new.id, new.content, new.audio_transcript);
    END""",
    """CREATE TRIGGER IF NOT EXISTS chat_events_fts_ad AFTER DELETE ON chat_events BEGIN
        INSERT INTO chat_events_fts(chat_events_fts, rowid, content, audio_transcript)
        VALUES ('delete', old.id, old.content, old.audio_transcript);
    END""",
    """CREATE TRIGGER IF NOT EXISTS chat_events_fts_au
    AFTER UPDATE OF content, audio_transcript ON chat_events BEGIN
        INSERT INTO chat_events_fts(chat_events_fts, rowid, content, audio_transcript)
        VALUES ('delete', old.id, old.content, old.audio_transcript);
        INSERT INTO chat_events_fts(rowid, content, audio_transcript)
        VALUES (new.id, new.content, new.audio_transcript);
    END""",
)
