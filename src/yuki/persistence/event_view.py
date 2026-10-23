"""Facts from an immutable ledger record, shared by interface envelopes."""

from typing import Any

from yuki.conversation.media_index import indexed_media_segments
from yuki.domain.audio import parse_transcripts
from yuki.domain.messages import mention_snapshots
from yuki.persistence.repository_records import EventRecord
from yuki.time.formatting import local_iso


def event_view(row: EventRecord, *, timezone: str = "Asia/Taipei") -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.id,
        "event_kind": row.event_kind,
        "source_plugin_id": row.source_plugin_id,
        "external_source": row.external_source,
        "external_event_type": row.external_event_type,
        "author_kind": row.author_kind,
        "source": row.origin,
        "origin": row.origin,
        "person_id": row.author_person_id,
        "author_person_id": row.author_person_id,
        "presence_id": row.author_presence_id,
        "author_presence_id": row.author_presence_id,
        "author_account_id": row.author_account_id,
        "ingress_presence_id": row.ingress_presence_id,
        "sender_nickname": row.sender_nickname,
        "sender_group_card": row.sender_group_card,
        "sender_display_name": row.sender_display_name,
        "scope": row.scope_type.value,
        "conversation_id": row.canonical_conversation_id,
        "direction": row.direction,
        "content": row.evidence_content,
        "mentions": list(mention_snapshots(row.segments)),
        "asr": [
            {"segment_index": item.segment_index, "text": item.text, "source": item.source}
            for item in parse_transcripts(row.audio_transcript)
            if item.source == "current"
        ],
        "reply_to_event_id": row.reply_to_event_id,
        "attachments": [
            {
                "attachment_index": index,
                "segment_index": position,
                "kind": kind,
                **{
                    key: value
                    for key, value in data.items()
                    if key
                    in {
                        "media_id",
                        "artifact_id",
                        "version",
                        "filename",
                        "name",
                        "mime_type",
                        "size",
                    }
                },
            }
            for index, position, kind, data in indexed_media_segments(row.segments)
        ],
        "occurred_at": local_iso(row.occurred_at, timezone),
        "observed_at": local_iso(row.observed_at or row.occurred_at, timezone),
    }
