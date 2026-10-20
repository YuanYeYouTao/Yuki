"""Exact immutable artifact references projected from their original ledger segments."""

from typing import Final

ARTIFACT_REFERENCE_SELECT: Final = (
    "SELECT {event_id}, json_extract(part.value, '$.data.artifact_id'), "
    "json_extract(part.value, '$.data.version') FROM json_each({segments}) AS part "
    "WHERE part.type='object' AND json_type(part.value, '$.data')='object' "
    "AND json_type(part.value, '$.data.artifact_id')='text' "
    "AND json_type(part.value, '$.data.version')='text'"
)

_INSERT = (
    "INSERT OR IGNORE INTO chat_event_artifact_refs(event_id,artifact_id,version) "
    + ARTIFACT_REFERENCE_SELECT.format(event_id="NEW.id", segments="NEW.segments_json")
    + ";"
)

ARTIFACT_REFERENCE_TRIGGERS: Final[dict[str, str]] = {
    "chat_events_artifact_refs_ai": (
        "CREATE TRIGGER chat_events_artifact_refs_ai AFTER INSERT ON chat_events "
        "BEGIN " + _INSERT + " END"
    ),
    "chat_events_artifact_refs_au": (
        "CREATE TRIGGER chat_events_artifact_refs_au AFTER UPDATE OF segments_json ON chat_events "
        "WHEN NEW.segments_json IS NOT OLD.segments_json "
        "BEGIN DELETE FROM chat_event_artifact_refs WHERE event_id=OLD.id; " + _INSERT + " END"
    ),
}
