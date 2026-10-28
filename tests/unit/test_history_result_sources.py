"""History evidence keeps its original sources through archives and erasure."""

import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_event
from tests.support.social_identity_cases import social_env

from yuki.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ScopeType
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.persistence.models import ChatEventModel, MemoryToolReceiptModel, ToolArtifactModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import EventLedgerRepository
from yuki.runtime.effect_outcomes import ResultCapture, current_result_capture, execution_evidence
from yuki.runtime.work_schema_v1 import effects, journal, work
from yuki.tool_results.access import ArtifactAccess, ArtifactRead
from yuki.tool_results.artifacts import ToolArtifactRepository


def test_original_history_sources_are_evidence_not_model_or_activation_input():
    outcome = ToolExecutionResult(
        ok=True,
        tool_name="query_chat_history",
        data={"events": [{"event_id": 17, "content": "original"}]},
        source_event_ids=(17,),
        source_privacy_generation=3,
    )
    assert "source_event_ids" not in outcome.model_payload()
    assert "source_privacy_generation" not in outcome.model_payload()
    evidence = execution_evidence(outcome, tool=outcome.tool_name, side_effecting=False)
    assert evidence["source_event_ids"] == [17]
    assert "observed_event_ids" not in evidence


async def test_composed_result_keeps_all_child_sources_without_changing_public_text():
    capture = ResultCapture("work", "effect")
    capture.observe_sources((11, 17), 2)
    capture.observe_sources((17, 23), 3)
    token = current_result_capture.set(capture)
    try:
        rendered = await ToolResultBudgeter(max_characters=None).render(
            ToolExecutionResult(ok=True, data="program result", tool_name="execute_code")
        )
    finally:
        current_result_capture.reset(token)
    assert capture.outcome.source_event_ids == (11, 17, 23)
    assert capture.outcome.source_privacy_generation == 2
    assert "source_event_ids" not in json.loads(rendered.text)


async def test_erased_text_cannot_enter_journal_or_reexecute_but_sent_receipt_survives(
    database, tmp_path
):
    from tests.support.work_effect_results_helpers import owned_session
    from tests.support.work_session import invoke_tool

    from yuki.domain.messages import ToolCall, ToolFunction
    from yuki.runtime.work_repository import WorkConflict

    control, session, _store = await owned_session(database, tmp_path)
    call = ToolCall("history", ToolFunction("query_chat_history", "{}"))
    source_id = control.current["id"]
    sent = f"sent:{source_id}"
    now = time.time()
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(effects).values(
                effect_key=sent,
                work_id=source_id,
                kind="tool",
                state="accepted",
                receipt_json=json.dumps({"result": "sent", "outcome": {"ok": True}}),
                created=now,
                updated=now,
            )
        )

    async def read_then_erase():
        async with database.immediate_session() as writer:
            writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        rendered = await ToolResultBudgeter(max_characters=None).render(
            ToolExecutionResult(
                ok=True,
                data="old private body",
                source_event_ids=(17,),
                source_privacy_generation=0,
            )
        )
        return rendered.text

    with pytest.raises(WorkConflict, match="work_effect_history_source_changed"):
        await invoke_tool(session, call, read_then_erase, side_effecting=False)
    async with database.read_session() as reader:
        receipt = await reader.scalar(
            select(effects.c.receipt_json).where(effects.c.effect_key == session.call_key(call.id))
        )
        assert "old private body" not in receipt
        assert (
            await reader.scalar(select(effects.c.state).where(effects.c.effect_key == sent))
            == "accepted"
        )

    async def forbidden():
        pytest.fail("A failed publication does not authorize replay")

    replay = await invoke_tool(session, call, forbidden, side_effecting=False)
    assert json.loads(replay)["error"] == "execution_outcome_unknown"


async def test_accepted_child_history_replay_keeps_sources_for_parent(database, tmp_path):
    from tests.support.work_effect_results_helpers import owned_session
    from tests.support.work_session import invoke_tool

    from yuki.domain.messages import ToolCall, ToolFunction

    _control, session, _store = await owned_session(database, tmp_path)
    call = ToolCall("history", ToolFunction("query_chat_history", "{}"))

    async def initial():
        rendered = await ToolResultBudgeter(max_characters=None).render(
            ToolExecutionResult(
                ok=True, data="source", source_event_ids=(17,), source_privacy_generation=0
            )
        )
        return rendered.text

    original = await invoke_tool(session, call, initial, side_effecting=False)
    parent = ResultCapture("parent", "parent-effect")
    token = current_result_capture.set(parent)
    try:
        assert await session.journal.effect_result(session.call_key(call.id)) == original
    finally:
        current_result_capture.reset(token)
    assert parent.source_event_ids == (17,)
    assert parent.source_privacy_generation == 0


async def test_independent_audit_uses_read_privacy_instead_of_late_current_value(
    database, tmp_path
):
    from tests.unit.test_tool_effect_audit import active_work, invoke_audit

    from yuki.memory.partition import MemoryPartitionResolutionError
    from yuki.tool_results.recorder import ToolReceiptRepository

    _env, _work, runtime = await active_work(database, tmp_path)
    async with database.immediate_session() as writer:
        writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
    capture = ResultCapture("", "independent")
    capture.observe_sources((17,), 0)
    token = current_result_capture.set(capture)
    try:
        with pytest.raises(MemoryPartitionResolutionError, match="tool_receipt_source_changed"):
            await invoke_audit(
                ToolReceiptRepository(database), runtime, "independent", "old copied text"
            )
    finally:
        current_result_capture.reset(token)
    async with database.read_session() as reader:
        assert await reader.scalar(select(MemoryToolReceiptModel.id)) is None


async def test_fresh_read_does_not_replace_original_audit_privacy(database, tmp_path):
    from tests.unit.test_tool_effect_audit import active_work

    from yuki.memory.partition import MemoryPartitionResolutionError
    from yuki.tool_results.recorder import ToolReceiptRepository

    _env, _work, runtime = await active_work(database, tmp_path)
    async with database.immediate_session() as writer:
        writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
    with pytest.raises(MemoryPartitionResolutionError, match="tool_receipt_source_changed"):
        await ToolReceiptRepository(database).record_receipt(
            provider_id="core",
            tool_name="query_chat_history",
            success=True,
            error_category=None,
            trigger_event_id=runtime.effective_trigger_event_id,
            bot_user_id=runtime.effective_bot_user_id,
            canonical_conversation_id=runtime.effective_conversation_id,
            ingress_presence_id=runtime.effective_presence_id,
            audit_source=(runtime.effective_conversation_id, 1, 0),
            source_event_ids=(17,),
            source_privacy_generation=1,
            result_excerpt="The call also contains material from before its fresh read",
        )


async def test_archive_page_retains_sources_and_refuses_pre_erasure_late_publication(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person)
    store = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=600)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="query_chat_history",
        content=json.dumps({"ok": True, "data": {"events": [{"event_id": 17}]}}),
        media_type="application/json",
        access=access,
        source_event_ids=(17,),
        source_privacy_generation=0,
    )
    page = await store.read(handle, operation="get", path=("events",), access=access)
    assert isinstance(page, ArtifactRead)
    assert page.source_event_ids == (17,) and page.source_privacy_generation == 0
    assert "source_event_ids" not in page
    async with database.immediate_session() as session:
        session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
    with pytest.raises(ValueError, match="artifact_source_changed"):
        await store.write_artifact(
            provider_id="core",
            tool_name="query_chat_history",
            content="old body",
            media_type="application/json",
            access=access,
            source_event_ids=(17,),
            source_privacy_generation=0,
        )


async def test_forget_follows_copied_history_sources_across_conversations(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        forgotten = await ensure_person(session, "10002")
    event, _ = await seed_event(
        EventLedgerRepository(database),
        bot_user_id="80001",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="10002",
        direction="inbound",
        origin_key="private-source",
        content="only in the other person's private chat",
    )
    identity = str(uuid4())
    now = time.time()
    async with database.immediate_session() as session:
        await session.execute(
            insert(work).values(
                id=identity,
                conversation_id=env.context.conversation_id,
                generation=1,
                source_key=f"history-copy:{identity}",
                source_json=json.dumps({"actor_person_id": env.person}),
                goal="read a previous conversation",
                state="running",
                created=now,
                updated=now,
            )
        )
        await session.execute(
            insert(effects).values(
                effect_key=f"history:{identity}",
                work_id=identity,
                kind="tool",
                state="accepted",
                receipt_json=json.dumps(
                    {
                        "result": event.content,
                        "outcome": {"tool": "query_chat_history", "source_event_ids": [event.id]},
                    }
                ),
                created=now,
                updated=now,
            )
        )
        await session.execute(
            insert(journal).values(
                work_id=identity,
                chain_id=str(uuid4()),
                contract="original",
                source_revision=1,
                phase="paired",
                payload_json=event.content,
                updated=now,
            )
        )
        from datetime import UTC, datetime, timedelta

        timestamp = datetime.now(UTC)
        receipt = MemoryToolReceiptModel(
            conversation_key_hash="group-copy",
            trigger_event_id=await session.scalar(
                select(ChatEventModel.id).where(
                    ChatEventModel.canonical_conversation_id == env.context.conversation_id
                )
            ),
            bot_user_id="80001",
            canonical_person_id=env.person,
            provider_id="core",
            tool_name="query_chat_history",
            success=True,
            result_excerpt=event.content,
            result_characters=len(event.content),
            source_event_ids_json=json.dumps([event.id]),
            created_at=timestamp,
            expires_at=timestamp + timedelta(days=1),
        )
        session.add(receipt)
        await session.flush()
        receipt_id = receipt.id
    store = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=600)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="query_chat_history",
        content=event.content,
        media_type="text/plain",
        work_id=identity,
        access=ArtifactAccess(env.context.conversation_id, 1, env.person),
        source_event_ids=(event.id,),
        source_privacy_generation=0,
    )
    assert await PeopleRepository(database).delete_person_id(forgotten)
    async with database.read_session() as session:
        assert await session.get(ChatEventModel, event.id) is None
        assert await session.scalar(select(work.c.id).where(work.c.id == identity)) is None
        assert await session.get(MemoryToolReceiptModel, receipt_id) is None
        assert (await session.get(ToolArtifactModel, handle)).deleting
        assert (await session.get(CanonicalConversationModel, env.context.conversation_id)).id
        assert await session.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                ChatEventModel.content == "hello",
            )
        )
