"""Memory evidence quotes accept the event text exactly as the model read it.

Failure cases written before the change:
1. The model sees a native @ of Yuki as "@Yuki 笨蛋" (stored "@Agent 笨蛋"); a save_memory or
   self-reflection quote copied from what it read is refused as not in the event.
2. A quote of the stored text ("@Agent 笨蛋") must keep working.
3. "@Yuki" never shown for this event (literal text, no native self mention) is not invented
   as evidence.
4. A quote that is in neither form stays refused.
"""

from datetime import UTC, datetime

import pytest

from yuki.domain.conversations import ScopeType
from yuki.memory.mutation.models import MemoryMutationRequest
from yuki.memory.mutation.service import MemoryMutationRejected, MemoryMutationService
from yuki.persistence.repository_records import EventRecord


def _event(content: str, segments: tuple[dict[str, object], ...]) -> EventRecord:
    return EventRecord(
        id=1,
        bot_user_id="8000",
        origin_key="native-1",
        platform="qq",
        scope_type=ScopeType.GROUP,
        sender_user_id="9912345",
        direction="inbound",
        content=content,
        visual_summary="",
        segments=segments,
        occurred_at=datetime(2026, 11, 18, 15, 40, tzinfo=UTC),
        author_kind="person",
    )


SELF_MENTION = _event(
    "@Agent 叫我宝宝",
    (
        {
            "type": "mention",
            "data": {"kind": "yuki", "display_name": "Agent", "text_start": 0, "text_end": 6},
        },
    ),
)
LITERAL = _event("@Agent 叫我宝宝", ())


def _request(quote: str) -> MemoryMutationRequest:
    return MemoryMutationRequest.model_validate(
        {"operation": "create", "new_content": "叫 Yuki 宝宝", "evidence_quote": quote}
    )


@pytest.mark.parametrize(
    "check",
    [MemoryMutationService._evidence_quote, MemoryMutationService._self_reflection_evidence_quote],
)
def test_quotes_match_the_stored_or_the_rendered_self_mention(check) -> None:
    for quote in ("@Yuki 叫我宝宝", "@Agent 叫我宝宝", "叫我宝宝"):
        assert check(_request(quote), SELF_MENTION, self_name="Yuki") == quote

    for event, quote in ((LITERAL, "@Yuki 叫我宝宝"), (SELF_MENTION, "@Yuki 叫你宝宝")):
        with pytest.raises(MemoryMutationRejected):
            check(_request(quote), event, self_name="Yuki")
