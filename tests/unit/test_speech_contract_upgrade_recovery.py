"""A contract upgrade over a legacy frozen plan pauses it; nothing runs or sends."""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.support.work_session import WorkSession
from tests.unit.test_runtime_recovery import setup
from tests.unit.test_speech_retirement_recovery import accept, old_message, run, snapshot

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatTool, ProviderContinuation
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.runtime.delivery_intents import reserve
from qq_ai_bot.runtime.work_delivery import LEGACY_DELIVERY_PAUSE, frozen_plan_hash
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import journal
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
    digest = frozen_plan_hash(values)
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
    assert result.work_state == "suspended"
    assert control.completion_rejected == LEGACY_DELIVERY_PAUSE
    assert await snapshot(control) == before
    assert control.current["model_requests"] == original_budget["model_requests"]
    assert control.current["tool_calls"] == original_budget["tool_calls"]
    assert control.current["sent_messages"] == original_budget["sent_messages"]
    return control, new_contract, models


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "accepted", "unknown", "prepared"])
async def test_contract_upgrade_pauses_plan_without_model_or_send(database, tmp_path, state):
    values = [old_message(kind="audio"), old_message(text="remaining image")]
    control, _, models = await original_delivery(database, tmp_path, values, state)
    await control.settle(pending_inputs=False)
    row = await control.repository.get(control.current["id"])
    assert (row["state"], row["reason"]) == ("suspended", LEGACY_DELIVERY_PAUSE)
    models.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_importer_reconciles_upgraded_plan_on_original_keys(database, tmp_path):
    values = [old_message(kind="audio"), old_message(text="remaining image")]
    control, _, _ = await original_delivery(database, tmp_path, values, "accepted")
    original = {item["effect_key"] for item in (await snapshot(control))[1]}
    report = await run(control)
    assert report.paused == 1 and report.not_sent_recorded == 1
    keys = {item["effect_key"] for item in (await snapshot(control))[1]}
    # The new final-2 shares the original chain/sequence prefix of final-1.
    (first,) = original
    assert {key.rsplit(":", 1)[0] for key in keys} == {first.rsplit(":", 1)[0]}


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
