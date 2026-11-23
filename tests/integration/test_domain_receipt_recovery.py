"""Real consumers recover domain commits whose original Work T3 was not saved.

The boundary snapshot reconstructs a durable interruption, not an OS kill. Models
are deterministic; domain writes, journals and recovery are real.
"""

import json

import pytest
from sqlalchemy import func, select, update
from tests.conftest import MemorySender
from tests.support.runtime_execution import make_child_executor, make_work_resumer
from tests.unit.test_history_dispatch_ownership import (
    _scene,
    _tool,
    retry_due_now,
    yield_activation_after,
)
from tests.unit.test_memory_mutation import _service

from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import MemoryFactModel, MemoryMutationReceiptModel
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, journal, work
from yuki.services.main_agent_contract import MainAgentContract


class DomainCommitBoundary:
    """Retain actual pre-T3 rows, then restore that boundary after fixture setup."""

    def __init__(self, monkeypatch, tool, recorded=None):
        # ``recorded`` adds invocation fields a historical receipt carried.
        self.recorded = recorded or {}
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
                if row["effect_key"] == self.key and self.recorded:
                    receipt = json.loads(row["receipt_json"])
                    receipt["invocation"].update(self.recorded)
                    row = {**row, "receipt_json": json.dumps(receipt)}
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
    provider = FakeLLMProvider()

    def respond(_request):
        if len(provider.requests) == 1:
            return _tool("manage_task", {"action": "start", "goal": "remember once"}, "accept")
        if len(provider.requests) == 2:
            return _tool(
                "save_memory",
                {
                    "content": "remember the original committed preference",
                    "target": {"person_id": env.person},
                },
                "remember-once",
            )
        return "The original committed result is available."

    provider._responder = respond
    env, harness, chat, _state, message = await _scene(
        database, tmp_path, provider, yield_after=1 if worker else 2
    )
    chat._tools._memory_mutations = _service(database)[0]
    chat.runtime.runner.main_contract = MainAgentContract(chat, _state)
    observed_effects = []
    observe = WorkControl.observe_evidence

    def observe_memory(owner, evidence):
        if evidence.get("tool") == "save_memory":
            observed_effects.append(dict(evidence))
        return observe(owner, evidence)

    monkeypatch.setattr(WorkControl, "observe_evidence", observe_memory)
    boundary = DomainCommitBoundary(monkeypatch, "save_memory")
    await harness.processor.handle(message, MemorySender())
    await retry_due_now(database)
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

        # The worker activation also ends right after its committed domain call.
        yield_activation_after(provider, len(provider.requests) + 1)
        assert await executor().run(identity) == "LLMUnavailableError"
        await retry_due_now(database)
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
    assert str(fact_id) in observed
    assert "execution_outcome_unknown" not in observed
    assert "remember-once" in observed
    recovered = next(item for item in observed_effects if item.get("effect_key") == boundary.key)
    assert (
        recovered.get("ok") is not False
        and recovered["executed"]
        and recovered["mutation_committed"]
    )
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
