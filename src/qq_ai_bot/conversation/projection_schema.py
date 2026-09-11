"""Frozen SQLite invalidation definitions used by migration 0054 and test metadata."""


def _trigger(name: str, event: str, scope: str, reason: str, when: str = "") -> str:
    return f"""CREATE TRIGGER {name} AFTER {event}
    {f"WHEN {when}" if when else ""}
    BEGIN
        UPDATE canonical_conversations
        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN ({scope});
        UPDATE prompt_projections
        SET payload_json='[]', byte_size=2, revision=revision+1,
            invalidated_reason='{reason}'
        WHERE conversation_id IN ({scope});
    END"""


PROJECTION_TRIGGERS_0054 = {
    "prompt_projection_reset": _trigger(
        "prompt_projection_reset",
        "UPDATE OF generation, starts_after_event_id ON canonical_conversations",
        "NEW.id",
        "reset",
        "OLD.generation IS NOT NEW.generation "
        "OR OLD.starts_after_event_id IS NOT NEW.starts_after_event_id",
    ),
    "prompt_projection_event_delete": _trigger(
        "prompt_projection_event_delete",
        "DELETE ON chat_events",
        "OLD.canonical_conversation_id",
        "deleted_event",
    ),
    "prompt_projection_event_update": _trigger(
        "prompt_projection_event_update",
        "UPDATE OF content, segments_json, visual_summary, external_payload_json, "
        "suppression_status, canonical_conversation_id "
        "ON chat_events",
        "OLD.canonical_conversation_id, NEW.canonical_conversation_id",
        "source_changed",
        "OLD.content IS NOT NEW.content OR OLD.segments_json IS NOT NEW.segments_json "
        "OR OLD.visual_summary IS NOT NEW.visual_summary "
        "OR OLD.external_payload_json IS NOT NEW.external_payload_json "
        "OR OLD.suppression_status IS NOT NEW.suppression_status "
        "OR OLD.canonical_conversation_id IS NOT NEW.canonical_conversation_id",
    ),
}

for _table in (
    "canonical_conversation_rollups",
    "canonical_conversation_rollup_emergency_overlays",
):
    for _event in ("INSERT", "UPDATE", "DELETE"):
        _name = f"prompt_projection_{_table}_{_event.lower()}"
        PROJECTION_TRIGGERS_0054[_name] = _trigger(
            _name,
            f"{_event} ON {_table}",
            "OLD.conversation_id" if _event == "DELETE" else "NEW.conversation_id",
            "rollup",
        )
