"""Background semantic publication retains the Work's still-effective overlay."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.rollup_test_helpers import candidate_summary
from tests.unit.test_conversation_rollup_370 import _append, _policy
from tests.unit.test_work_journal_source_retry import _saved, _session

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationRollupEmergencyOverlayModel,
)
from qq_ai_bot.conversation.rollup.models import RollupKind
from qq_ai_bot.conversation.rollup.renderer import render_rollup_message
from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ToolCall, ToolFunction
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.runtime.work_journal import decode_transcript
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _candidate_with_overlay(database, *, catches_up=False, frozen_summary=False):
    policy = _policy(batch_max_events=2)
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    scope = ConversationScope.group("8000", "2001")
    await _append(uow, scope, 8)
    repository = ConversationRollupRepository(database, policy)
    claim = await repository.claim_next_job(lease_owner="background", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    snapshot = await repository.load_prompt_snapshot(scope)
    coverage = candidate.events[-1].id if catches_up else snapshot.raw_events[-1].id
    assert catches_up or coverage > candidate.events[-1].id
    now = datetime.now(UTC)
    async with database.sessions() as writer, writer.begin():
        writer.add(
            CanonicalConversationRollupEmergencyOverlayModel(
                conversation_id=claim.conversation_id,
                generation=claim.generation,
                covered_through_event_id=coverage,
                summary_text="frozen effective overlay",
                source_fingerprint="a" * 64,
                base_semantic_revision=0,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
    initial = (
        ChatMessage("system", "fixed contract"),
        render_rollup_message(
            "frozen effective overlay", kind="emergency", covered_through_event_id=coverage
        ),
        ChatMessage("user", "original task"),
    )
    control, session, _selected, _unselected = await _session(database, initial)
    if frozen_summary:
        session.source_guard = WorkSourceGuard(
            replace(
                session.source_guard.version,
                selected_summary_text="frozen effective overlay",
            )
        )
        assert await session.source_guard.check(control)
        await session.save("paired")
    return repository, scope, claim, candidate, control, session


def _payload(adapter, transcript):
    sequence = transcript.request()
    return json.dumps(
        adapter._build_payload(
            ChatRequest(
                messages=sequence.messages,
                continuation=sequence.continuation,
                continuation_items=sequence.items,
                model="fixed-model",
                max_output_tokens=8192,
                request_chain_id=transcript.chain_id,
            )
        ),
        ensure_ascii=False,
    )


async def _receipt(database, control):
    async with database.sessions() as reader:
        return dict(
            (
                await reader.execute(
                    select(effects).where(effects.c.work_id == control.current["id"])
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.parametrize("adapter_type", [GeminiProvider, OpenAICompatibleProvider])
async def test_real_semantic_commit_keeps_effective_overlay_and_exact_request(
    database, adapter_type
):
    repository, scope, claim, candidate, control, session = await _candidate_with_overlay(
        database, frozen_summary=True
    )
    before = await repository.load_prompt_snapshot(scope)
    old_revision = session.source_revision
    adapter = adapter_type(
        base_url="https://wire.invalid/v1/",
        api_key="test-key",
        timeout_seconds=1,
        max_retries=0,
    )
    call = ToolCall("original-call", ToolFunction("read", "{}"))
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    invoke = AsyncMock(
        return_value='{"ok":true,"executed":true,"execution_id":"original-execution"}'
    )
    result = await invoke_tool(session, call, invoke)
    session.transcript.append_result(call.id, result)
    original_receipt = await _receipt(database, control)
    original_budget = await control.repository.get(control.current["id"])
    assert original_receipt["state"] == "accepted"
    frozen = _payload(adapter, session.transcript)
    await repository.commit_candidate(
        claim,
        candidate,
        summary_text=candidate_summary(candidate, "new background semantic result"),
        summary_kind=RollupKind.MODEL,
    )
    after = await repository.load_prompt_snapshot(scope)
    assert after.rollup == before.rollup
    assert after.rollup_stamp != before.rollup_stamp
    async with database.sessions() as reader:
        overlay = await reader.get(
            CanonicalConversationRollupEmergencyOverlayModel, control.lease.conversation_id
        )
        assert overlay.base_semantic_revision == 1
    # Same-owner derived publication no longer invalidates frozen selection.
    # An actually dispatched request still restores its exact private protocol.
    assert await session.source_guard.check(control)
    await session.save("dispatched")
    assert session.source_revision == old_revision
    loaded = await session.journal.load(control.lease, control.current["id"], session.contract)
    assert loaded.reason == "resume"
    restored = decode_transcript(json.loads(loaded.record["payload_json"])["transcript"])
    assert restored.chain_id == session.transcript.chain_id
    assert _payload(adapter, restored) == frozen
    recovered = WorkSession(control, session.contract)
    control.session = recovered
    await recovered.restore(TurnTranscript((ChatMessage("user", "unused new wakeup"),)))
    assert await invoke_tool(recovered, call, invoke) == result
    invoke.assert_awaited_once()
    assert await _receipt(database, control) == original_receipt
    current_budget = await control.repository.get(control.current["id"])
    assert current_budget["tool_calls"] == original_budget["tool_calls"] == 1
    assert current_budget["model_requests"] == original_budget["model_requests"]
    await adapter.close()


async def test_semantic_catchup_keeps_already_selected_summary_until_explicit_new_input(database):
    repository, scope, claim, candidate, control, session = await _candidate_with_overlay(
        database, catches_up=True, frozen_summary=True
    )
    saved = await _saved(database, control)
    await repository.commit_candidate(
        claim,
        candidate,
        summary_text=candidate_summary(candidate, "semantic now replaces overlay"),
        summary_kind=RollupKind.MODEL,
    )
    after = await repository.load_prompt_snapshot(scope)
    assert after.overlay is None and after.rollup.summary_kind is RollupKind.MODEL
    assert await session.source_guard.check(control)
    await session.save("response")
    current = await _saved(database, control)
    assert current["source_revision"] == saved["source_revision"]
    assert current["chain_id"] == saved["chain_id"]
    assert session.source_guard.version.selected_summary_text == "frozen effective overlay"


async def test_privacy_generation_rejects_even_with_unchanged_effective_summary(database):
    _repository, _scope, _claim, _candidate, control, session = await _candidate_with_overlay(
        database
    )
    fingerprint = session.source_guard.fingerprint
    async with database.sessions() as writer, writer.begin():
        state = await writer.get(ExecutionTraceStateModel, 1)
        if state is None:
            writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        else:
            state.privacy_generation += 1
    assert not await session.source_guard.check(control)
    assert session.source_guard.fingerprint == fingerprint


@pytest.mark.parametrize(
    "field", ["summary_text", "covered_through_event_id", "revision", "source_fingerprint"]
)
async def test_effective_overlay_payload_changes_remain_source_conflicts(database, field):
    _repository, _scope, _claim, _candidate, control, session = await _candidate_with_overlay(
        database
    )
    async with database.sessions() as writer, writer.begin():
        overlay = await writer.get(
            CanonicalConversationRollupEmergencyOverlayModel, control.lease.conversation_id
        )
        if field == "summary_text":
            value = "edited actual summary"
        elif field == "source_fingerprint":
            value = "b" * 64
        else:
            value = getattr(overlay, field) + 1
        setattr(overlay, field, value)
    assert not await session.source_guard.check(control)
