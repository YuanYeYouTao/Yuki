"""A real static-contract upgrade preserves only original delivery authority."""

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.support.work_session import WorkSession
from tests.unit.test_runtime_recovery import Sender, setup
from tests.unit.test_speech_retirement_recovery import accept, old_message, snapshot

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatTool, ProviderContinuation
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.delivery_intents import reserve
from qq_ai_bot.runtime.work_delivery import _frozen_plan_hash, resume_delivery_plan
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects, journal
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard
from qq_ai_bot.services.agent_runner import AgentRunner
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.turn_execution import TurnExecution
from qq_ai_bot.services.turn_transcript import TurnTranscript


@dataclass(frozen=True)
class ConfigFixture:
    model: str = "fixed-model"


def static_contract(version):
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    if version == 12:
        parameters["properties"]["voice"] = {"type": "object"}
    return (
        ChatTool(
            "send_message",
            "explicit send",
            parameters,
            schema_version="1" if version == 12 else "2",
        ),
    )


async def original_delivery(database, tmp_path, values, state, *, guarded=False):
    control = await setup(database, tmp_path)
    models = SimpleNamespace(
        profile_revision=lambda _: "fixture",
        execute=AsyncMock(side_effect=AssertionError("model must not run")),
    )
    runner = AgentRunner(models, ConcurrencyManager(1))
    runtime = SimpleNamespace(
        fixed_tools=static_contract(13),
        origin=TurnOrigin.USER_MESSAGE,
        visible_event_ids=frozenset(),
        work_control=control,
        runtime_config=SimpleNamespace(llm=ConfigFixture(), web=ConfigFixture()),
        compaction_brief=None,
        script_api=None,
    )
    old_messages = (ChatMessage("system", "Main Agent contract version 12"),)
    new_messages = (ChatMessage("system", "Main Agent contract version 13"),)
    old_contract = runner.work_contract(runtime.runtime_config, old_messages, static_contract(12))
    new_contract = runner.work_contract(runtime.runtime_config, new_messages, static_contract(13))
    assert old_contract != new_contract
    control.session = WorkSession(control, old_contract)
    await control.session.restore(TurnTranscript(old_messages))
    if guarded:
        async with database.sessions() as session:
            source = await session.get(CanonicalConversationModel, control.lease.conversation_id)
            version = ConversationReadVersion(
                ConversationScope.group("80001", "20001"),
                source.id,
                source.generation,
                source.starts_after_event_id,
                source.prompt_source_revision,
                (1,),
            )
        control.session.source_guard = WorkSourceGuard(version)
        assert await control.session.source_guard.check(control)
    control.session.sequence = 7
    control.session.transcript.accept(
        ProviderContinuation(
            provider="gemini", protocol="gemini", payload={"secret": "old-private-signature"}
        )
    )
    control.session.progress["provider_pause_replay"] = True
    control.session.progress["delivery_plan"] = values
    original_prefix = control.session.call_key("final-1")
    digest = _frozen_plan_hash(values)
    await control.session.save("delivery")
    await reserve(
        control,
        control.session.call_key("final-plan"),
        "final",
        {"plan_hash": digest},
        count=len(values),
    )
    if state:
        await accept(control, 1, state=state)
    before = await snapshot(control)
    original_budget = dict(control.current)
    result = await TurnExecution(runner, new_messages, runtime, None).activate()
    assert result.model_requests == result.tool_calls_used == 0
    assert result.suppress_delivery and result.text == ""
    models.execute.assert_not_awaited()

    assert control.session.recovered_delivery == "delivery"
    assert control.session.call_key("final-1") != original_prefix
    assert control.session.delivery_call_key("final-1") == original_prefix
    request = control.session.transcript.request()
    assert request.continuation is None
    assert "old-private-signature" not in repr(request)
    assert "provider_pause_replay" not in control.session.progress
    assert await snapshot(control) == before
    assert control.current["model_requests"] == original_budget["model_requests"]
    assert control.current["tool_calls"] == original_budget["tool_calls"]
    assert control.current["sent_messages"] == original_budget["sent_messages"]
    return control, new_contract, models


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "unknown"])
async def test_changed_delivery_prefix_cannot_bypass_original_reservation(
    database, tmp_path, state
):
    control, contract, models = await original_delivery(database, tmp_path, [old_message()], state)
    original_budget = control.current["sent_messages"]
    control.session.delivery_origin["chain_id"] = "f" * 32
    await control.session.save("delivery")
    control.session = WorkSession(control, contract)
    await control.session.restore(TurnTranscript(()))
    before = await snapshot(control)
    sender = Sender()
    with pytest.raises(WorkConflict, match="requires_reconciliation"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before
    assert control.current["sent_messages"] == original_budget
    models.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_contract_12_to_13_confirmed_audio_and_image_use_original_calls(database, tmp_path):
    values = [old_message(kind="audio"), old_message(text="remaining image")]
    control, contract, models = await original_delivery(
        database, tmp_path, values, "accepted", guarded=True
    )
    original_key = control.session.delivery_call_key("final-1")
    budget = control.current["sent_messages"]
    sender = Sender()
    assert await resume_delivery_plan(control, sender)
    assert sender.messages == ["remaining image"]
    assert control.current["sent_messages"] == budget
    # Restart the new code after its first saved delivery, then upgrade again.
    for changed in (contract, "another-static-contract"):
        control.session = WorkSession(control, changed)
        await control.session.restore(TurnTranscript((ChatMessage("system", "new contract"),)))
        assert control.session.delivery_call_key("final-1") == original_key
        assert control.session.progress["delivery_plan"] == json.loads(json.dumps(values))
        assert await resume_delivery_plan(control, sender)
        assert sender.messages == ["remaining image"]
        assert control.current["sent_messages"] == budget
    models.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "unknown"])
async def test_contract_upgrade_unknown_audio_blocks_model_and_fresh_delivery_keys(
    database, tmp_path, state
):
    control, contract, models = await original_delivery(
        database, tmp_path, [old_message(kind="audio"), old_message()], state
    )
    key = control.session.delivery_call_key("final-1")
    before = await snapshot(control)
    sender = Sender()
    with pytest.raises(WorkConflict, match="requires_reconciliation"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before
    # Save the delivery-only view while suspended; a second upgrade retains old uncertainty.
    await control.session.save("delivery")
    control.session = WorkSession(control, contract + "-next")
    await control.session.restore(TurnTranscript(()))
    assert control.session.recovered_delivery == "delivery"
    assert control.session.delivery_call_key("final-1") == key
    with pytest.raises(WorkConflict, match="requires_reconciliation"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before
    models.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_contract_upgrade_unsubmitted_audio_refuses_without_model_fallback(
    database, tmp_path
):
    control, _, models = await original_delivery(
        database, tmp_path, [old_message(), old_message(kind="audio")], None
    )
    before = await snapshot(control)
    sender = Sender()
    with pytest.raises(WorkConflict, match="retired_speech_delivery"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == before
    models.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_only_view_hydrates_owned_media_without_old_private_protocol(
    database, tmp_path
):
    text = "data:image/png;base64,cGl4ZWxz"
    control, _, models = await original_delivery(database, tmp_path, [old_message(text=text)], None)
    sender = Sender()
    assert await resume_delivery_plan(control, sender)
    assert sender.messages == [text]
    models.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_contract_upgrade_cannot_discard_delivery_on_source_revision_change(
    database, tmp_path
):
    control, contract, _ = await original_delivery(
        database, tmp_path, [old_message(kind="audio"), old_message()], "unknown"
    )
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == control.lease.conversation_id)
            .values(prompt_source_revision=CanonicalConversationModel.prompt_source_revision + 1)
        )
    before = await snapshot(control)
    control.session = WorkSession(control, contract)
    with pytest.raises(WorkConflict, match="work_journal_source_changed"):
        await control.session.restore(TurnTranscript(()))
    assert await snapshot(control) == before
    async with database.sessions() as session:
        assert await session.scalar(select(journal.c.contract)) != contract


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["selected_event", "privacy", "generation"])
async def test_upgraded_delivery_still_validates_original_source_guard(database, tmp_path, change):
    control, contract, models = await original_delivery(
        database, tmp_path, [old_message(kind="audio"), old_message()], "unknown", guarded=True
    )
    async with database.sessions() as session, session.begin():
        if change == "selected_event":
            await session.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == 1)
                .values(sender_nickname="changed selected evidence")
            )
        elif change == "privacy":
            source = await session.get(ExecutionTraceStateModel, 1)
            if source is None:
                session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
            else:
                source.privacy_generation += 1
        else:
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == control.lease.conversation_id)
                .values(generation=CanonicalConversationModel.generation + 1)
            )
    before = await snapshot(control)
    control.session = WorkSession(control, contract)
    with pytest.raises(WorkConflict):
        await control.session.restore(TurnTranscript(()))
    assert await snapshot(control) == before
    models.execute.assert_not_awaited()


async def advance_privacy(database):
    async with database.sessions() as session, session.begin():
        state = await session.get(ExecutionTraceStateModel, 1)
        if state is None:
            session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        else:
            state.privacy_generation += 1


@pytest.mark.asyncio
async def test_new_contract_saved_then_same_contract_restart_rechecks_privacy(database, tmp_path):
    control, contract, models = await original_delivery(
        database, tmp_path, [old_message()], None, guarded=True
    )
    await control.session.save("delivery")
    await advance_privacy(database)
    before = await snapshot(control)
    budget = control.current["sent_messages"]
    control.session = WorkSession(control, contract)
    with pytest.raises(WorkConflict, match="work_journal_source_changed"):
        await control.session.restore(TurnTranscript(()))
    assert await snapshot(control) == before
    assert control.current["sent_messages"] == budget
    models.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_privacy_change_after_prepare_is_rechecked_before_dispatch(
    database, tmp_path, monkeypatch
):
    control, _, models = await original_delivery(
        database, tmp_path, [old_message()], None, guarded=True
    )
    budget = control.current["sent_messages"]
    original = control.repository.prepare_effect

    async def prepare_then_delete(*args, **kwargs):
        prepared = await original(*args, **kwargs)
        assert prepared
        await advance_privacy(database)
        return prepared

    monkeypatch.setattr(control.repository, "prepare_effect", prepare_then_delete)
    sender = Sender()
    with pytest.raises(WorkConflict, match="work_journal_source_changed"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == []
    assert control.current["sent_messages"] == budget
    async with database.sessions() as session:
        row = (
            (
                await session.execute(
                    select(effects).where(
                        effects.c.effect_key == control.session.delivery_call_key("final-1")
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["state"] == "failed"
    assert json.loads(row["receipt_json"])["executed"] is False
    assert json.loads(row["receipt_json"])["mutation_committed"] is False
    assert not await control.has_unresolved_effects()
    settled = await snapshot(control)
    # Failed is a known zero dispatch, not permission to re-issue the original
    # plan or refund its reservation. The existing key still fences recovery.
    with pytest.raises(WorkConflict, match="requires_reconciliation"):
        await resume_delivery_plan(control, sender)
    assert sender.messages == [] and await snapshot(control) == settled
    assert control.current["sent_messages"] == budget
    models.execute.assert_not_awaited()
