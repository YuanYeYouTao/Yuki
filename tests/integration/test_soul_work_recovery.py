"""Hot role edits do not invalidate an already-paid response's original tail."""

from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.integration.test_soul_runtime_consumers import activate
from tests.support.work_runner_helpers import case, tool

from yuki.domain.messages import ChatMessage, ChatResponse, ProviderContinuation
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, journal, work
from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


class PaidResponseCrash(BaseException):
    pass


@pytest.mark.parametrize("native", [False, True])
async def test_hot_soul_edit_finishes_original_paid_response_after_cold_restore(
    database, tmp_path, monkeypatch, native
):
    calls = (
        tool("task_control", {"action": "complete", "result": "原结果"}, "original-complete"),
        tool("write_fixture", {"step": 1}, "original-write"),
        tool("task_control", {"action": "get"}, "original-status"),
        tool("send", {"text": "原已付报告"}, "original-send"),
    )
    continuation = ProviderContinuation(
        "gemini",
        "gemini",
        ({"role": "model", "parts": [{"thoughtSignature": "original-paid-signature"}]},),
    )
    probe = await case(
        database,
        tmp_path,
        [
            ChatResponse(
                "原响应", 0, tool_calls=calls, continuation=continuation if native else None
            )
        ],
    )
    probe.runner.main_contract = MainAgentContract(
        probe.chat, ShortState(WorkspaceStore(tmp_path / "main-contract"))
    )
    settings = probe.chat._settings
    path = tmp_path / "SOUL.md"
    activate(settings, path, name="初雪", soul="原人格{{name}}", revision=1)

    def initial():
        return (*probe.chat._prompt_composer.static_messages(), ChatMessage("user", "执行原任务"))

    execute = probe.runner._execute_tool_batch
    armed = True

    async def pause_original_send(pending, *args, **kwargs):
        nonlocal armed
        if armed and pending[0].id == "original-send":
            armed = False
            raise PaidResponseCrash()
        return await execute(pending, *args, **kwargs)

    monkeypatch.setattr(probe.runner, "_execute_tool_batch", pause_original_send)
    with pytest.raises(PaidResponseCrash):
        await probe.runner.run(initial(), probe.runtime, probe.backend)
    control = probe.control
    original_chain = control.session.transcript.chain_id
    original_key = control.session.call_key("original-send")
    original_request = deepcopy(probe.provider.requests[0])
    original_continuation = control.session.transcript.request().continuation
    assert (original_continuation is not None) is native
    assert probe.observed == ["write_fixture"]
    original_send = probe.backend.execute_call
    sent_from = []

    async def observe_original_send(invocation):
        if invocation.call.id == "original-send":
            sent_from.append(
                (
                    invocation.identity.chain_id,
                    invocation.context.runtime.work_control.session.transcript.request(),
                )
            )
        return await original_send(invocation)

    monkeypatch.setattr(probe.backend, "execute_call", observe_original_send)
    original_work = await probe.repository.get(control.current["id"])
    assert original_work["model_requests"] == 1
    async with database.sessions() as reader:
        original_receipts = [
            dict(row)
            for row in (
                await reader.execute(
                    select(effects).where(effects.c.work_id == original_work["id"])
                )
            ).mappings()
        ]

    activate(settings, path, name="Aurora", soul="新人格{{name}}", revision=2)
    assert "Aurora" in initial()[0].content
    await database.close()
    recovered = WorkControl(
        probe.repository, control.lease, control.source_key, control.source, control.validate
    )
    recovered.current = await probe.repository.get(original_work["id"])
    result = await probe.runner.run(
        initial(), replace(probe.runtime, work_control=recovered), probe.backend
    )
    assert result.work_state == "completed" and result.text == "原结果"
    assert probe.provider.requests == [original_request]
    assert probe.observed == ["write_fixture", "send"]
    assert len(sent_from) == 1 and sent_from[0][0] == original_chain
    send_request = sent_from[0][1]
    assert "初雪" in send_request.messages[0].content
    assert "Aurora" not in send_request.messages[0].content
    assert send_request.continuation == original_continuation
    if native:
        assert recovered.session.transcript.chain_id == original_chain
        assert recovered.session.transcript.request().continuation == original_continuation
        assert "original-send" in recovered.session.paired_call_ids()
    assert await recovered.session.journal.effect_state(original_key) == "accepted"
    async with database.sessions() as reader:
        persisted = {
            row["effect_key"]: dict(row)
            for row in (
                await reader.execute(
                    select(effects).where(effects.c.work_id == original_work["id"])
                )
            ).mappings()
        }
        budget = (
            (await reader.execute(select(budgets).where(budgets.c.root_id == original_work["id"])))
            .mappings()
            .one()
        )
    assert all(persisted[row["effect_key"]] == row for row in original_receipts)
    assert budget["models"] == 1 and budget["tools"] == 2


async def test_hot_soul_edit_keeps_lost_paid_dispatch_unknown(database, tmp_path, monkeypatch):
    probe = await case(database, tmp_path, [ChatResponse("原已付结果", 0)])
    probe.runner.main_contract = MainAgentContract(
        probe.chat, ShortState(WorkspaceStore(tmp_path / "main-contract"))
    )
    settings = probe.chat._settings
    path = tmp_path / "SOUL.md"
    activate(settings, path, name="初雪", soul="原人格{{name}}", revision=1)

    def initial():
        return (*probe.chat._prompt_composer.static_messages(), ChatMessage("user", "原任务"))

    complete = probe.provider.complete

    async def lose_paid_response(request):
        await complete(request)
        raise PaidResponseCrash()

    monkeypatch.setattr(probe.provider, "complete", lose_paid_response)
    with pytest.raises(PaidResponseCrash):
        await probe.runner.run(initial(), probe.runtime, probe.backend)
    original = deepcopy(probe.provider.requests)
    control = probe.control
    activate(settings, path, name="Aurora", soul="新人格{{name}}", revision=2)
    await database.close()
    recovered = WorkControl(
        probe.repository, control.lease, control.source_key, control.source, control.validate
    )
    recovered.current = await probe.repository.get(control.current["id"])
    result = await probe.runner.run(
        initial(), replace(probe.runtime, work_control=recovered), probe.backend
    )
    assert result.work_state == "failed"
    assert result.outcome.failure.code == "work_response_not_persisted"
    assert probe.provider.requests == original
    assert probe.observed == []
    persisted = await probe.repository.get(control.current["id"])
    assert persisted["model_requests"] == 1 and persisted["tool_calls"] == 0
    async with database.sessions() as reader:
        assert await reader.scalar(
            select(journal.c.phase).where(journal.c.work_id == control.current["id"])
        ) == "dispatched"


async def test_paired_work_new_business_activation_uses_new_soul(database, tmp_path):
    from tests.conftest import MemorySender
    from tests.integration.test_person_paid_recovery import _facts
    from tests.unit.test_history_dispatch_ownership import _scene, _tool
    from tests.unit.test_work_owner_recovery import original_responses, original_scheduler

    from yuki.llm.fake import FakeLLMProvider

    provider = FakeLLMProvider()
    provider._responder = original_responses(provider)
    env, harness, chat, _state, inbound = await _scene(
        database, tmp_path, provider, request_limit=2
    )
    path = tmp_path / "SOUL.md"
    activate(harness.settings, path, name="初雪", soul="原人格{{name}}", revision=1)
    await harness.processor.handle(inbound, MemorySender())
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
    saved, receipts, budget = await _facts(database, original["id"])
    assert original["state"] == "queued" and saved["phase"] == "paired"
    original_requests = deepcopy(provider.requests)
    activate(harness.settings, path, name="Aurora", soul="新人格{{name}}", revision=2)
    await database.close()

    def finish(request):
        assert "Aurora" in request.messages[0].content
        assert "初雪" not in request.messages[0].content
        assert "committed-original" in str(request.messages)
        return _tool("task_control", {"action": "complete"}, "finish-existing-work")

    provider._responder = finish
    scheduler = original_scheduler(repository, env, harness, chat)
    await scheduler.drain_once()
    final = await repository.get(original["id"])
    assert final["state"] == "completed" and final["model_requests"] == 3
    assert provider.requests[:2] == original_requests and len(provider.requests) == 3
    _saved, final_receipts, final_budget = await _facts(database, original["id"])
    assert all(row in final_receipts for row in receipts)
    assert final_budget["models"] == budget["models"] + 1
    assert final_budget["tools"] == budget["tools"]


async def test_main_recovery_contract_retains_technical_rules_and_tool_model_settings(
    database, tmp_path, monkeypatch
):
    probe = await case(database, tmp_path, [])
    manifest = MainAgentContract(probe.chat, ShortState(WorkspaceStore(tmp_path / "main-contract")))
    probe.runner.main_contract = manifest
    settings = probe.chat._settings
    path = tmp_path / "SOUL.md"
    activate(settings, path, name="初雪", soul="原人格{{name}}", revision=1)
    runtime = probe.runtime.runtime_config
    definitions = probe.runtime.fixed_tools

    def contract(config=runtime, tools=definitions):
        return probe.runner.work_contract(
            config, probe.chat._prompt_composer.static_messages(), tools
        )

    original = contract()
    activate(settings, path, name="Aurora", soul="新人格{{name}}", revision=2)
    assert contract() == original
    assert contract(replace(runtime, llm=replace(runtime.llm, model="another-model"))) != original
    assert contract(tools=definitions[:-1]) != original
    manifest.revision = "new-manifest"
    assert contract() != original
    manifest.revision = ""
    contributions = probe.chat._prompt_composer.invariant_contributions()
    monkeypatch.setattr(
        probe.chat._prompt_composer,
        "invariant_contributions",
        lambda: (contributions[0].model_copy(update={"content": "changed technical contract"}),),
    )
    assert contract() != original
