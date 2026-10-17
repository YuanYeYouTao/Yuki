"""Erased sources remove checkpoint copies without erasing original paid effects."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter
from sqlalchemy import delete, select
from tests.unit.test_memory_candidate_privacy import _candidate, _values
from tests.unit.test_memory_v2 import _append_event

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemorySelfReflectionRequestModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_helpers import _event_record
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def _group_fact(writer, space_id, key):
    now = datetime.now(UTC)
    row = MemoryFactModel(
        scope_type="self",
        visibility_type="group",
        canonical_visibility_space_id=space_id,
        kind="fact",
        memory_key=key,
        category="self_reflection",
        content=f"原正式记忆：{key}",
        normalized_content=f"原正式记忆：{key}",
        source_type="automatic",
        authority="agent_reflection",
        created_at=now,
        updated_at=now,
        last_confirmed_at=now,
    )
    writer.add(row)
    await writer.flush()
    return row


@pytest.mark.parametrize(
    "copy_kind",
    [
        "event",
        "event_expired",
        "event_edited",
        "tool",
        "tool_expired",
        "fact",
        "candidate",
        "context",
    ],
)
async def test_forget_clears_only_related_group_checkpoint_and_preserves_paid_facts(
    database, copy_kind
):
    ledger = EventLedgerRepository(database)
    erased = await _append_event(ledger, message_id="erased-source", group_id="2001")
    retained = await _append_event(
        ledger, message_id="retained-source", group_id="2001", user_id="1002"
    )
    async with database.sessions() as reader:
        conversation = await reader.get(
            CanonicalConversationModel, erased.canonical_conversation_id
        )
        space_id = conversation.space_id
    raw = {
        "output": {"proposals": [], "episodes": []},
        "event_map": {},
        "tool_map": {},
        "fact_map": {},
        "candidate_map": {},
        "payload": {"context_events": [], "self_facts": [], "self_candidates": []},
        "completed_counts": None,
    }
    now = datetime.now(UTC)
    if copy_kind in {"event", "event_expired"}:
        raw["event_map"]["event_1"] = TypeAdapter(EventRecord).dump_python(erased, mode="json")
    elif copy_kind == "event_edited":
        mention, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id="original-mention",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            sender_user_id="1002",
            direction="inbound",
            content="1001的原私人资料",
            segments=(
                {"type": "at", "data": {"qq": "1001"}},
                {
                    "type": "yuki_context",
                    "data": {
                        "mentioned_user_ids": ["1001"],
                        "mentioned_person_ids": [erased.author_person_id],
                    },
                },
            ),
        )
        assert mention.mentioned_person_ids == (erased.author_person_id,)
        raw["event_map"]["event_1"] = TypeAdapter(EventRecord).dump_python(mention, mode="json")
        await ScopedEventLedgerUnitOfWork(database).update_platform_message(
            mention.id,
            InboundMessage(
                message_id="original-mention",
                event_type="message",
                scope_type=ScopeType.GROUP,
                group_id="2001",
                sender=SenderIdentity(user_id="1002"),
                text="编辑后无提及资料",
            ),
            presence_id=mention.ingress_presence_id,
            edited_at=now + timedelta(seconds=1),
            revision=1,
        )
        async with database.sessions() as reader:
            current = _event_record(await reader.get(ChatEventModel, mention.id))
        assert current.content == "编辑后无提及资料"
        assert current.mentioned_person_ids == ()
    elif copy_kind == "candidate":
        candidate_id = await _candidate(database, erased)
        raw["candidate_map"]["candidate_1"] = {
            "id": candidate_id,
            "content": "旧候选原正文",
        }
        raw["payload"]["self_candidates"] = [{"ref": "candidate_1", "content": "旧候选原正文"}]
    elif copy_kind == "context":
        rendered = ChatEventPromptRenderer((erased, retained)).render_event(erased)
        raw["payload"]["context_events"] = [
            {
                "ref": "context_1",
                "occurred_at": erased.occurred_at.isoformat(),
                "direction": "inbound",
                "rendered": rendered,
            }
        ]
    async with database.immediate_session() as writer:
        committed_fact = await _group_fact(writer, space_id, "already-committed")
        if copy_kind in {"tool", "tool_expired"}:
            tool = MemoryToolReceiptModel(
                conversation_key_hash="f" * 64,
                trigger_event_id=erased.id,
                bot_user_id="8000",
                canonical_space_id=space_id,
                provider_id="original-provider",
                tool_name="read_webpage",
                success=True,
                result_excerpt="原来源工具正文",
                result_characters=8,
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
            writer.add(tool)
            await writer.flush()
            raw["tool_map"]["tool_1"] = {
                "id": tool.id,
                "trigger_event_id": erased.id,
                "result_excerpt": tool.result_excerpt,
            }
            if copy_kind == "tool_expired":
                await writer.execute(
                    delete(MemoryToolReceiptModel).where(MemoryToolReceiptModel.id == tool.id)
                )
        elif copy_kind == "fact":
            fact = await _group_fact(writer, space_id, "copied-old-memory")
            writer.add(
                MemoryEvidenceModel(
                    fact_id=fact.id,
                    event_id=erased.id,
                    source_speaker_user_id="1001",
                    relation="agent_reflection",
                    authority="agent_reflection",
                    excerpt="旧来源证据",
                    created_at=now,
                )
            )
            raw["fact_map"]["fact_1"] = {"id": fact.id, "content": fact.content}
            raw["payload"]["self_facts"] = [{"ref": "fact_1", "content": fact.content}]
        related_raw = json.dumps(raw, ensure_ascii=False)
        related = MemorySelfReflectionRunModel(
            conversation_key_hash="f" * 64,
            bot_user_id="8000",
            canonical_space_id=space_id,
            scheduled_slot="original-related",
            trigger_reason="original",
            first_event_id=retained.id,
            last_event_id=retained.id,
            status="failed",
            proposal_count=2,
            committed_count=1,
            started_at=now,
            completed_at=now,
            attempt_count=3,
            retry_state="isolated",
            input_fingerprint="original-paid-fingerprint",
            checkpoint_json=related_raw,
        )
        # A body mentioning an event token is not a copied source or context identity.
        unrelated_raw = json.dumps(
            {
                "output": {"proposals": []},
                "event_map": {
                    "event_1": TypeAdapter(EventRecord).dump_python(retained, mode="json")
                },
                "payload": {
                    "context_events": [
                        {
                            "rendered": ChatEventPromptRenderer((retained,)).render_event(retained)
                            + f" |事件:#{erased.id}]"
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        unrelated = MemorySelfReflectionRunModel(
            conversation_key_hash="f" * 64,
            bot_user_id="8000",
            canonical_space_id=space_id,
            scheduled_slot="original-unrelated",
            trigger_reason="original",
            first_event_id=retained.id,
            last_event_id=retained.id,
            status="failed",
            proposal_count=0,
            committed_count=0,
            started_at=now,
            retry_state="isolated",
            checkpoint_json=unrelated_raw,
        )
        writer.add_all((related, unrelated))
        await writer.flush()
        result = MemorySelfReflectionResultModel(
            run_id=related.id,
            fact_id=committed_fact.id,
            result_kind="proposal",
            result_index=1,
            created_at=now,
        )
        request = MemorySelfReflectionRequestModel(
            run_id=related.id,
            local_date=now.date().isoformat(),
            created_at=now,
            status="completed",
            output_tokens=17,
        )
        writer.add_all((result, request))
        await writer.flush()
        related_id, unrelated_id = related.id, unrelated.id
        result_id, request_id, fact_id = result.id, request.id, committed_fact.id
        if copy_kind == "event_expired":
            await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == erased.id))
    async with database.sessions() as reader:
        related_before = _values(await reader.get(MemorySelfReflectionRunModel, related_id))
        unrelated_before = _values(await reader.get(MemorySelfReflectionRunModel, unrelated_id))
        result_before = _values(await reader.get(MemorySelfReflectionResultModel, result_id))
        request_before = _values(await reader.get(MemorySelfReflectionRequestModel, request_id))
        fact_before = _values(await reader.get(MemoryFactModel, fact_id))
    assert erased.id < related_before["first_event_id"]
    assert await PeopleRepository(database).delete_person_id(erased.author_person_id)
    async with database.sessions() as reader:
        assert await reader.get(ChatEventModel, erased.id) is None
        after = _values(await reader.get(MemorySelfReflectionRunModel, related_id))
        assert after == dict(related_before, checkpoint_json=None)
        assert (
            _values(await reader.get(MemorySelfReflectionRunModel, unrelated_id))
            == unrelated_before
        )
        assert (
            _values(await reader.get(MemorySelfReflectionResultModel, result_id)) == result_before
        )
        assert (
            _values(await reader.get(MemorySelfReflectionRequestModel, request_id))
            == request_before
        )
        assert _values(await reader.get(MemoryFactModel, fact_id)) == fact_before
        assert await reader.scalar(select(MemorySelfReflectionRequestModel.output_tokens)) == 17
