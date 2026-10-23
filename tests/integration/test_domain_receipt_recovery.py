"""Real consumers recover domain commits whose original Work T3 was not saved.

The boundary snapshot reconstructs a durable interruption, not an OS kill. Models
and the Code VM are deterministic; domain writes, journals and recovery are real.
"""

import json
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from tests.conftest import MemorySender
from tests.integration.test_codemode_lifecycle_recovery import FakeEngine, FakeRun
from tests.support.codemode_cases import TOOLS, build_host, effect_rows, outer_call
from tests.support.runtime_execution import make_child_executor, make_work_resumer
from tests.unit.test_external_agent_repository import prepare, register_device
from tests.unit.test_external_agent_work import case as _native_case
from tests.unit.test_history_dispatch_ownership import _scene, _tool
from tests.unit.test_memory_mutation import _service
from tests.unit.test_memory_operation_identity import create_request

from yuki.codemode.api_projection import project
from yuki.codemode.driver import ChildClass, CodeModeDriver
from yuki.domain.messages import ChatMessage, ChatTool
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import MemoryFactModel, MemoryMutationReceiptModel
from yuki.runtime.effect_outcomes import current_result_capture
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, journal, work
from yuki.runtime.work_session import WorkSession
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.turn_transcript import TurnTranscript

native_case = _native_case


class DomainCommitBoundary:
    """Retain actual pre-T3 rows, then restore that boundary after fixture setup."""

    def __init__(self, monkeypatch, tool):
        self.saved = None
        original = WorkRepository.record_effect

        async def record(repository, key, state, receipt, **kwargs):
            if self.saved is None and receipt.get("outcome", {}).get("tool") == tool:
                async with repository.database.read_session() as reader:
                    row = dict(
                        (await reader.execute(select(effects).where(effects.c.effect_key == key)))
                        .mappings()
                        .one()
                    )
                    metadata = json.loads(row["receipt_json"])["invocation"]
                    keys = [key]
                    if metadata.get("parent_effect_key"):
                        keys.append(metadata["parent_effect_key"])
                    rows = [
                        dict(item)
                        for item in (
                            await reader.execute(
                                select(effects).where(effects.c.effect_key.in_(keys))
                            )
                        ).mappings()
                    ]
                    checkpoint = dict(
                        (
                            await reader.execute(
                                select(journal).where(journal.c.work_id == row["work_id"])
                            )
                        )
                        .mappings()
                        .one()
                    )
                self.saved = (rows, checkpoint)
                self.key = key
                self.metadata = metadata
            return await original(repository, key, state, receipt, **kwargs)

        monkeypatch.setattr(WorkRepository, "record_effect", record)

    async def restore(self, database):
        assert self.saved is not None, "the real domain call never reached Work T3"
        rows, checkpoint = self.saved
        async with database.immediate_session() as writer:
            for row in rows:
                await writer.execute(
                    update(effects).where(effects.c.effect_key == row["effect_key"]).values(**row)
                )
            await writer.execute(
                update(journal)
                .where(journal.c.work_id == checkpoint["work_id"])
                .values(**checkpoint)
            )
        await database.engine.dispose()


async def budget(database, root_id):
    async with database.read_session() as reader:
        return dict(
            (await reader.execute(select(budgets).where(budgets.c.root_id == root_id)))
            .mappings()
            .one()
        )


@pytest.mark.parametrize("worker", [False, True], ids=["main", "worker"])
async def test_memory_commit_is_consumed_by_original_main_or_worker(
    database, tmp_path, monkeypatch, worker
):
    request = create_request("remember the original committed preference")
    provider = FakeLLMProvider()

    def respond(_request):
        if len(provider.requests) == 1:
            return _tool("task_control", {"action": "accept", "goal": "remember once"}, "accept")
        if len(provider.requests) == 2:
            return _tool("memory_change", request.model_dump(exclude_none=True), "remember-once")
        return "The original committed result is available."

    provider._responder = respond
    env, harness, chat, _state, message = await _scene(
        database, tmp_path, provider, request_limit=1 if worker else 2
    )
    chat._tools._memory_mutations = _service(database)[0]
    chat.runtime.runner.main_contract = MainAgentContract(chat, _state, code_enabled=False)
    observed_effects = []
    observe = WorkControl.observe_evidence

    def observe_memory(owner, evidence):
        if evidence.get("tool") == "memory_change":
            observed_effects.append(dict(evidence))
        return observe(owner, evidence)

    monkeypatch.setattr(WorkControl, "observe_evidence", observe_memory)
    boundary = DomainCommitBoundary(monkeypatch, "memory_change")
    await harness.processor.handle(message, MemorySender())
    repository = WorkRepository(database)
    async with database.read_session() as reader:
        root_id = await reader.scalar(select(work.c.id))
    root = await repository.get(root_id)
    identity = root_id
    if worker:
        database.subagents_enabled = True
        lease = await repository.acquire(root["conversation_id"], root["generation"])
        identity = await SubagentRepository(repository).start(
            lease, root_id, "original-memory-child", {"goal": "remember once"}
        )
        await repository.release(lease)

        def executor():
            return make_child_executor(
                repository,
                chat=chat,
                config=chat._runtime_config,
                runner=chat.runtime.runner,
                load_tools=chat.runtime.runner.main_contract.definitions,
                router=env.router,
            )

        assert await executor().run(identity) is None
    await boundary.restore(database)
    before = await repository.get(identity)
    before_budget = await budget(database, root_id)
    async with database.read_session() as reader:
        original = (await reader.execute(select(MemoryMutationReceiptModel))).scalars().one()
        mutation_id, fact_id = original.mutation_id, original.new_fact_id
        assert original.outcome == "committed"
        counts = (
            await reader.scalar(select(func.count()).select_from(MemoryFactModel)),
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)),
        )
    assert boundary.metadata["original_domain_ref"] == f"memory:{mutation_id}"
    original_requests = len(provider.requests)
    observed_effects.clear()
    if worker:
        assert await executor().run(identity) is None
    else:
        resumer = make_work_resumer(
            repository,
            ledger=harness.ledger,
            scopes=chat._conversation_scopes,
            turns=chat._turn_coordinator,
            router=env.router,
            config=chat._runtime_config,
            generate_self=chat.generate_self_initiative,
            generate_wakeup=chat.generate_main_agent_wakeup,
            bindings=chat.runtime.bindings,
        )
        assert await resumer.resume(before) is None
    assert len(provider.requests) == original_requests + 1
    observed = "\n".join(message.content or "" for message in provider.requests[-1].messages)
    assert mutation_id in observed and str(fact_id) in observed
    assert "execution_outcome_unknown" not in observed
    assert "remember-once" in observed
    recovered = next(item for item in observed_effects if item.get("effect_key") == boundary.key)
    assert recovered["ok"] and recovered["executed"] and recovered["mutation_committed"]
    assert not recovered["uncertain"] and not recovered["pending"]
    after = await repository.get(identity)
    after_budget = await budget(database, root_id)
    assert after["state"] == "completed"
    assert after["source_json"] == before["source_json"]
    assert after["parent_work_id"] == before["parent_work_id"]
    assert after_budget["tools"] == before_budget["tools"]
    assert after_budget["models"] == before_budget["models"] + 1
    async with database.read_session() as reader:
        assert counts == (
            await reader.scalar(select(func.count()).select_from(MemoryFactModel)),
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)),
        )


@pytest.mark.parametrize("uncertain", [False, True], ids=["confirmed", "unknown"])
async def test_code_resume_consumes_original_native_control_receipt(
    native_case, monkeypatch, uncertain
):
    database, control = native_case.db, native_case.control
    native = await register_device(database, native_case.source["actor_person_id"])
    native.session = (
        await native.repository.remember_sessions(native.instance, native.actor, ["thread"])
    )["thread"]
    native.source = {
        **native_case.source,
        "conversation_id": control.lease.conversation_id,
        "work_id": control.current["id"],
    }
    owner = WorkSession(control, "original-contract")
    control.session = owner
    owner.transcript = TurnTranscript((ChatMessage("user", "steer the original execution"),))
    dispatched, views = [], []

    async def domain(name, arguments):
        assert name == "external_agent"
        capture = current_result_capture.get()
        request_id = sha256(capture.effect_key.encode()).hexdigest()
        await prepare(
            native, identity=request_id, arguments=json.loads(arguments), lease=control.lease
        )
        await native.repository.response(
            request_id,
            {"status": "unknown" if uncertain else "succeeded", "pending": uncertain},
        )
        dispatched.append(request_id)
        return json.dumps(
            {
                "ok": not uncertain,
                "uncertain": uncertain,
                "data": {"operation_id": request_id, "action": "steer"},
            }
        )

    args = {"action": "steer", "text": "continue the original work"}

    class ObservedRun(FakeRun):
        async def settle(self, results):
            views.extend(answer.value for answer in results.values())
            return await super().settle(results)

    class Engine(FakeEngine):
        def run(self, names):
            return ObservedRun(calls=[("yuki_external_agent", args)])

    def host(session):
        env = build_host(session, domain)
        env.host.api = project(
            (*TOOLS, ChatTool("external_agent", "agent", {"type": "object"})), "manifest-test"
        )
        env.host.classify = lambda call: ChildClass("write", False, True)
        env.host.worker = SimpleNamespace(execution_digest=lambda: "stable-fake-engine")
        env.host.engine_factory = lambda *_args: Engine()
        return env

    env = host(owner)
    outer = outer_call(env, "await yuki_external_agent({})")
    owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await owner.save("response", (outer.call,))
    boundary = DomainCommitBoundary(monkeypatch, "external_agent")
    assert json.loads(await CodeModeDriver(env.host, outer).run())["status"] == "completed"
    await boundary.restore(database)
    original_budget = await budget(database, control.current["id"])
    restored = WorkControl(
        control.repository, control.lease, control.source_key, control.source, control.validate
    )
    restored.current = await control.repository.get(control.current["id"])
    session = WorkSession(restored, owner.contract)
    restored.session = session
    await session.restore(TurnTranscript((ChatMessage("user", "resume"),)))
    resumed = host(session)
    views.clear()
    outer = replace(outer, context=replace(outer.context, runtime=resumed.agent))
    result = json.loads(await CodeModeDriver(resumed.host, outer).resume())
    assert dispatched == [sha256(boundary.key.encode()).hexdigest()]
    if uncertain:
        assert result["status"] == "partial", result
        assert result["stop_reason"] == "unknown_effect"
        assert result["uncertain_operation_id"] == boundary.key
        assert views == []
    else:
        assert result["status"] == "completed", result
        assert views[0]["ok"] and views[0]["executed"] and not views[0]["uncertain"]
        assert views[0]["data"]["operation_id"] == dispatched[0]
        assert views[0]["data"]["action"] == "steer"
        assert views[0]["status"] == "succeeded" and not views[0]["pending"]
        assert views[0]["reused"]
        assert views[0]["operation_id"] == boundary.key
    rows, tools, root = await effect_rows(database, control.current["id"])
    assert rows[outer.identity.operation_id]["state"] == "accepted"
    if not uncertain:
        assert json.loads(rows[outer.identity.operation_id]["receipt_json"])["outcome"][
            "side_effecting"
        ]
    assert tools == root == original_budget["tools"]
    assert restored.ending is None
