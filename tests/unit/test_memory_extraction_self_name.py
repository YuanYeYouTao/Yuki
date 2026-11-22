"""Memory extraction knows which name in the chat is Yuki herself.

Production had 155 active facts such as "ErrorAila 在本群称呼 Agent 为「妈妈」": the extractor
saw a native @ of Yuki as "@Agent" and her own lines labelled "Agent", and was never told the
bot's name. Failure cases written before the change:
1. Batch payload: a native @ of Yuki reaches the extractor as "@Agent"; her own context lines
   are labelled "Agent"; nothing names the bot.
2. Single-event payload: the same, without labels.
3. A member who is really called "Agent" keeps that name.
4. A claim quoting "@Yuki …" as the extractor read it is refused by the validator, while the
   stored "@Agent …" quote must keep working; "@Yuki" never shown for an event stays refused.
5. A rename is used on the next extraction.
"""

import json
from datetime import UTC, datetime

from tests.support.model_executor import InjectedModelExecutor

from yuki.domain.conversations import ScopeType
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.event_extractor import MemoryEventExtractor
from yuki.memory.extraction import MemoryClaim
from yuki.memory.validation import MemoryClaimValidator
from yuki.persistence.repository_records import EventRecord
from yuki.services.concurrency import ConcurrencyManager

PERSON = "00000000-0000-4000-8000-000000000001"


def _event(event_id: int, content: str, kind: str = "yuki", **changes: object) -> EventRecord:
    fields: dict[str, object] = {
        "id": event_id,
        "bot_user_id": "8000",
        "origin_key": f"native-{event_id}",
        "platform": "qq",
        "scope_type": ScopeType.GROUP,
        "sender_user_id": "9912345",
        "sender_group_card": "ErrorAila",
        "direction": "inbound",
        "content": content,
        "visual_summary": "",
        "segments": (
            {
                "type": "mention",
                "data": {"kind": kind, "display_name": "Agent", "text_start": 0, "text_end": 6},
            },
        )
        if content.startswith("@Agent")
        else (),
        "occurred_at": datetime(2026, 11, 18, 8, 0, tzinfo=UTC),
        "group_id": "2001",
        "author_kind": "person",
        "author_person_id": PERSON,
    }
    fields.update(changes)
    return EventRecord(**fields)  # type: ignore[arg-type]


OWN = _event(
    1,
    "早上好呀儿子",
    direction="outbound",
    sender_group_card="",
    author_kind="yuki",
    author_person_id=None,
)
CALL = _event(2, "@Agent 妈妈好")


def _extractor() -> tuple[MemoryEventExtractor, FakeLLMProvider]:
    provider = FakeLLMProvider(lambda request: '{"claims":[]}')
    return MemoryEventExtractor(InjectedModelExecutor(provider), ConcurrencyManager()), provider


async def test_batch_payload_names_the_bot_and_renders_its_mentions() -> None:
    extractor, provider = _extractor()

    await extractor.extract_batch((CALL,), context=(OWN,), self_name="Yuki", tightened=False)

    payload = json.loads(provider.requests[0].messages[-1].content)
    assert payload["bot_name"] == "Yuki"
    assert payload["events"][0]["content"] == "@Yuki 妈妈好"
    assert payload["conversation_context"][0]["sender_label"] == "Yuki"
    assert "Agent" not in json.dumps(payload, ensure_ascii=False)


async def test_single_payload_names_the_bot_and_renders_its_mentions() -> None:
    extractor, provider = _extractor()

    await extractor.extract(CALL, context=(OWN,), self_name="Yuki", tightened=False)

    payload = json.loads(provider.requests[0].messages[-1].content)
    assert payload["bot_name"] == "Yuki"
    assert payload["primary_event"]["content"] == "@Yuki 妈妈好"


async def test_member_called_agent_and_rename() -> None:
    extractor, provider = _extractor()
    member = _event(3, "@Agent 你好", kind="member")

    await extractor.extract_batch((member, CALL), self_name="ゆき", tightened=False)

    payload = json.loads(provider.requests[0].messages[-1].content)
    assert payload["bot_name"] == "ゆき"
    assert [event["content"] for event in payload["events"]] == ["@Agent 你好", "@ゆき 妈妈好"]


def _claim(quote: str) -> MemoryClaim:
    return MemoryClaim.model_validate(
        {
            "subject_ref": "speaker",
            "scope_type": "person_group",
            "kind": "fact",
            "category": "relationship",
            "memory_key": "nickname",
            "content": "ErrorAila 在本群叫 Yuki 妈妈",
            "evidence_quote": quote,
            "confidence": 0.9,
            "importance": 3,
            "source_type": "automatic",
        }
    )


def test_validator_accepts_the_quote_as_read_or_as_stored() -> None:
    validator = MemoryClaimValidator()
    literal = _event(4, "@Agent 妈妈好", kind="member")

    for quote in ("@Yuki 妈妈好", "@Agent 妈妈好"):
        result = validator.validate_claim_result(_claim(quote), CALL, self_name="Yuki")
        assert result.reason_code != "evidence_quote_not_in_event", result
    refused = validator.validate_claim_result(_claim("@Yuki 妈妈好"), literal, self_name="Yuki")
    assert refused.reason_code == "evidence_quote_not_in_event"
