"""Person merge preserves unobserved tool evidence and lost paid dispatch facts."""

import copy
import json
from dataclasses import replace

from sqlalchemy import select, update
from tests.conftest import MemorySender
from tests.support.work_session import WorkSession
from tests.unit.test_history_dispatch_ownership import _scene, _tool
from tests.unit.test_work_owner_recovery import original_responses, original_scheduler

from yuki.domain.messages import ChatMessage
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.person_identity import merge_persons
from yuki.llm.fake import FakeLLMProvider
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_recovery_schema import recovery
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, journal, scope, work
from yuki.services.main_agent_turns import MainAgentTurnService
from yuki.services.turn_transcript import TurnTranscript


async def _facts(database, identity):
    async with database.sessions() as reader:
        saved = dict(
            (await reader.execute(select(journal).where(journal.c.work_id == identity)))
            .mappings()
            .one()
        )
        receipts = [
            dict(row)
            for row in (
                await reader.execute(select(effects).where(effects.c.work_id == identity))
            ).mappings()
        ]
        budget = dict(
            (await reader.execute(select(budgets).where(budgets.c.root_id == identity)))
            .mappings()
            .one()
        )
    return saved, receipts, budget


async def _merge_original_person(database, source_id):
    async with database.immediate_session() as writer:
        target = await find_identity_binding(writer, "1002")
        assert target is not None and target.person_id != source_id
        return await merge_persons(writer, source_id, target.person_id)


async def test_merge_cold_work_observes_original_paired_result_once(
    database, tmp_path, monkeypatch
):
    provider = FakeLLMProvider()
    provider._responder = original_responses(provider)
    env, harness, chat, state, message = await _scene(database, tmp_path, provider, request_limit=2)
    mutations = []
    update_state = state.update

    def record_update(arguments):
        mutations.append(dict(arguments))
        return update_state(arguments)

    monkeypatch.setattr(state, "update", record_update)
    await harness.processor.handle(message, MemorySender())
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
    saved, receipts, budget = await _facts(database, original["id"])
    assert saved["phase"] == "paired" and original["state"] == "queued"
    assert len(provider.requests) == 2 and len(mutations) == 1
    assert budget["models"] == 2
    mutation_receipt = next(
        row
        for row in receipts
        if json.loads(row["receipt_json"]).get("outcome", {}).get("tool") == "update_short_state"
    )
    assert mutation_receipt["state"] == "accepted"
    original_requests = copy.deepcopy(provider.requests)
    root_id = await _merge_original_person(database, env.person)
    assert root_id != env.person
    await database.close()
    chat.runtime.main_turns = MainAgentTurnService(
        chat._prompt_composer, chat.runtime.runner, database
    )

    def complete_from_recorded_result(request):
        evidence = [
            json.loads(item.content)
            for item in request.messages
            if item.content and '"kind": "work_unobserved_tool_round"' in item.content
        ]
        assert len(evidence) == 1
        calls = evidence[0]["calls"]
        assert len(calls) == 1 and calls[0]["call_id"] == "original-mutation"
        assert calls[0]["effect_key"] == mutation_receipt["effect_key"]
        assert calls[0]["tool"] == "update_short_state"
        assert "committed-original" in calls[0]["result"]
        return _tool("task_control", {"action": "complete"}, "original-finish")

    provider._responder = complete_from_recorded_result
    scheduler = original_scheduler(repository, env, harness, chat)
    await scheduler.drain_once()
    final = await repository.get(original["id"])
    assert final["state"] == "completed"
    assert final["source_json"] == original["source_json"]
    assert json.loads(final["source_json"])["actor_person_id"] == env.person
    assert final["model_requests"] == 3 and len(provider.requests) == 3
    assert provider.requests[:2] == original_requests
    assert len(mutations) == 1
    _, final_receipts, final_budget = await _facts(database, original["id"])
    assert final_budget["models"] == budget["models"] + 1
    assert final_budget["tools"] == budget["tools"]
    for receipt in receipts:
        assert (
            next(item for item in final_receipts if item["effect_key"] == receipt["effect_key"])
            == receipt
        )
    await scheduler.drain_once()
    assert len(provider.requests) == 3 and len(mutations) == 1


async def test_merge_cold_work_keeps_lost_paid_response_unknown_without_repurchase(
    database, tmp_path
):
    provider = FakeLLMProvider()
    provider._responder = original_responses(provider)
    env, harness, chat, state, message = await _scene(database, tmp_path, provider, request_limit=1)
    await harness.processor.handle(message, MemorySender())
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
        contract = await reader.scalar(
            select(journal.c.contract).where(journal.c.work_id == original["id"])
        )
    lease = await repository.acquire(original["conversation_id"], original["generation"])

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository, lease, original["source_key"], json.loads(original["source_json"]), validate
    )
    control.current = original
    session = WorkSession(control, contract)
    await session.restore(TurnTranscript((ChatMessage("user", "original accepted task"),)))
    control.session = session
    await control.reserve_request()
    await session.save("dispatched")
    outgoing = session.transcript.request()
    dispatched = replace(
        provider.requests[-1],
        messages=outgoing.messages,
        continuation=outgoing.continuation,
        continuation_items=outgoing.items,
        request_chain_id=session.transcript.chain_id,
    )
    response = await provider.complete(dispatched)
    assert response.tool_calls[0].id == "original-mutation"
    # The provider accepted the original request, but the process lost its
    # response before the response/journal boundary. Do not synthesize a result.
    saved, receipts, budget = await _facts(database, original["id"])
    assert saved["phase"] == "dispatched" and budget["models"] == 2
    original_requests = copy.deepcopy(provider.requests)
    await repository.release(lease)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == original["id"]).values(state="running")
        )
        await writer.execute(
            update(scope)
            .where(scope.c.conversation_id == original["conversation_id"])
            .values(owner="lost-original-process", lease_until=0)
        )
    await _merge_original_person(database, env.person)
    await database.close()
    chat.runtime.main_turns = MainAgentTurnService(
        chat._prompt_composer, chat.runtime.runner, database
    )
    scheduler = original_scheduler(repository, env, harness, chat)
    await scheduler.drain_once()
    final = await repository.get(original["id"])
    assert final["state"] == "failed" and final["reason"] == "work_response_not_persisted"
    assert final["source_json"] == original["source_json"]
    assert final["model_requests"] == 2
    retained, final_receipts, final_budget = await _facts(database, original["id"])
    assert retained == saved
    assert final_receipts == receipts and final_budget == budget
    assert state.snapshot()[0]["text"] == "original-snapshot"
    assert provider.requests == original_requests
    async with database.sessions() as reader:
        failure = await reader.scalar(
            select(recovery.c.failure_json).where(recovery.c.work_id == original["id"])
        )
    assert json.loads(failure)["code"] == "work_response_not_persisted"
    await scheduler.drain_once()
    assert provider.requests == original_requests
    assert (await repository.get(original["id"]))["model_requests"] == 2
