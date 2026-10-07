"""Real Processor/Runner/SQLite/WorkResumer/adapters; inert domain and explicit VM substitute."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.support.codemode_cases import FakeDomain, build_host
from tests.support.correctness_wire import wire
from tests.support.runtime_execution import make_work_resumer
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from qq_ai_bot.codemode.driver_types import EngineCall, EngineOutcome, HostCounters
from qq_ai_bot.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, work

MARK = "CHILD_NOTE_BEFORE_PARENT_SETTLEMENT"
ORD = "ORDINARY_INTERLEAVED_PUBLIC_MARKER"
OTHER = "OTHER_WORK_OBSERVED_MARKER"


class Run:
    def __init__(self):
        self.index = 0
        self.waiting = False
        self.counters = HostCounters()

    def event(self):
        if self.index == 3:
            return EngineOutcome("completed", output="PROGRAM_RESULT_EXACTLY_ONCE")
        name, args = [
            (
                "yuki_task_control",
                dict(
                    action="update",
                    context_note=dict(
                        version=1,
                        facts=[dict(text=MARK, refs=["goal"])],
                        unresolved=[],
                        next_steps=[],
                    ),
                ),
            ),
            ("yuki_search_chat_history", {"query": "inert-first"}),
            ("yuki_search_chat_history", {"query": "inert-second"}),
        ][self.index]
        return EngineOutcome(
            "suspended",
            call=EngineCall(
                "future" if self.waiting else "function",
                0,
                None if self.waiting else self.index,
                None if self.waiting else name,
                args=() if self.waiting else (args,),
                pending_call_ids=(self.index,) if self.waiting else (),
            ),
        )

    async def start(self, code, inputs):
        return self.event()

    async def answer(self, call_id, answer):
        self.waiting = True
        return self.event()

    async def settle(self, results):
        self.index += 1
        self.waiting = False
        return self.event()

    def dump(self):
        return b"MONTY\0" + json.dumps([self.index, self.waiting]).encode()

    async def restore(self, dump, saved, counters):
        self.index, self.waiting = json.loads(dump[6:])
        self.counters = counters
        return self.event()

    async def terminate(self):
        pass


class Engine:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def run(self, names):
        return Run()


@pytest.mark.parametrize("pause", ["pending", "settled"])
@pytest.mark.parametrize("kind", ["chat", "responses", "anthropic", "gemini"])
@pytest.mark.asyncio
async def test_pending_code_interleave(database, tmp_path, monkeypatch, kind, pause):
    provider = FakeLLMProvider()

    def respond(request):
        n = len(provider.requests)
        if n in (1, 5):
            return _tool(
                "task_control",
                dict(
                    action="accept",
                    goal="W1" if n == 1 else "W2",
                    output_kind="answer",
                    reporting="quiet",
                ),
                "accept-" + str(n),
            )
        if n == 2:
            return _tool(
                "execute_code",
                {"code": "# Explicit audit VM: update note; await two inert reads", "inputs": {}},
                "compose-1",
            )
        if n == 3:
            return _tool("send_message", {"text": "ordinary response"}, "ordinary-send")
        if n == 6:
            return _tool(
                "task_control",
                dict(
                    action="update",
                    context_note=dict(
                        version=1,
                        facts=[dict(text=OTHER, refs=["goal"])],
                        unresolved=[],
                        next_steps=[],
                    ),
                ),
                "note-W2",
            )
        if n == 7:
            return _tool(
                "task_control",
                {"action": "update", "reporting": "quiet"},
                "post-resume-safe-control",
            )
        assert n in (4, 8), n
        return ChatResponse("ordinary done" if n == 4 else "resume result", 0)

    provider._responder = respond
    env, harness, chat, _, inbound = await _scene(
        database, tmp_path, provider, request_limit=2, code_enabled=True
    )
    runner = chat.runtime.runner
    client, wires = wire(SimpleNamespace(provider=provider, runner=runner), kind)
    chat._models = runner._models
    domain = FakeDomain()
    hosts = []

    def host(tools, runtime, **kwargs):
        e = build_host(
            runtime.work_control.session,
            domain,
            tool_limit=1 if pause == "pending" and not hosts else 32,
        )
        e.host.api = runtime.script_api or runner.main_contract.script_api
        e.host.worker = SimpleNamespace(execution_digest=lambda: "round3-explicit-inert-vm")
        e.host.engine_factory = lambda *args: Engine()
        hosts.append(e.host)
        return e.host

    monkeypatch.setattr(runner, "_code_host", host)
    snapshots = []

    async def snap(label):
        async with database.sessions() as r:
            projs = (await r.scalars(select(PromptProjectionModel))).all()
            notes = (await r.scalars(select(ContextObservationModel))).all()
            selections = (await r.scalars(select(ContextSelectionModel))).all()
            rows = (await r.execute(select(effects))).mappings().all()
            snapshots.append(
                dict(
                    label=label,
                    projection=[p.payload_json for p in projs],
                    notes=[dict(id=n.id, payload=n.payload_json) for n in notes],
                    selections=[
                        dict(id=s.id, observations=s.observation_sources_json) for s in selections
                    ],
                    effects=[
                        {
                            k: v
                            for k, v in row.items()
                            if k in ("effect_key", "state", "receipt_json")
                        }
                        for row in rows
                    ],
                )
            )

    try:
        await harness.processor.handle(replace(inbound, text="W1 initial request"), MemorySender())
        async with database.sessions() as r:
            first_id = await r.scalar(select(work.c.id))
        await snap("parent_pending_child_note_committed")
        assert len(domain.log) == (1 if pause == "pending" else 2)
        await harness.processor.handle(
            replace(inbound, message_id="ordinary-between", text=ORD), MemorySender()
        )
        await snap("ordinary_observed_child_note")
        await harness.processor.handle(
            replace(inbound, message_id="other-work", text="W2 new task"), MemorySender()
        )
        await snap("other_work_note_committed")
        repository = WorkRepository(database)
        resumer = make_work_resumer(
            repository,
            ledger=harness.ledger,
            scopes=chat._conversation_scopes,
            turns=chat._turn_coordinator,
            router=env.router,
            config=chat._runtime_config,
            generate_self=chat.generate_self_initiative,
            generate_wakeup=chat.generate_main_agent_wakeup,
            validate_snapshot=chat.validate_turn_snapshot,
            run_effect=chat.run_effect,
            bindings=chat.runtime.bindings,
        )
        await database.close()
        await resumer.resume(await repository.get(first_id))
        assert resumer.last_error is None, resumer.last_error
        await snap("original_work_resumed")
        assert len(wires) == 8
        assert domain.log == [
            ("search_chat_history", {"query": "inert-first"}),
            ("search_chat_history", {"query": "inert-second"}),
        ]
        assert MARK in json.dumps(wires[2], ensure_ascii=False)
        final = json.dumps(wires[-2], ensure_ascii=False)
        next_round = json.dumps(wires[-1], ensure_ascii=False)
        print(
            "FINAL_VISIBILITY",
            kind,
            pause,
            {m: final.count(m) for m in [MARK, ORD, OTHER, "PROGRAM_RESULT_EXACTLY_ONCE"]},
        )
        assert "PROGRAM_RESULT_EXACTLY_ONCE" in final
        print(
            "NEXT_VISIBILITY",
            kind,
            pause,
            {m: next_round.count(m) for m in [MARK, ORD, OTHER, "PROGRAM_RESULT_EXACTLY_ONCE"]},
        )
        assert ORD in final and OTHER in final, (
            "Settled Code Mode resume must use currently authorized interleaved history"
        )
        assert ORD in next_round and OTHER in next_round
        assert MARK in final and MARK in next_round
        assert final.count("PROGRAM_RESULT_EXACTLY_ONCE") == 1
        assert next_round.count("PROGRAM_RESULT_EXACTLY_ONCE") == 1
        before = json.loads(snapshots[0]["projection"][0])
        after = json.loads(snapshots[-1]["projection"][0])
        assert after[: len(before)] == before
        parent = [
            r for r in snapshots[-1]["effects"] if json.loads(r["receipt_json"]).get("composition")
        ]
        assert len(parent) == 1 and parent[0]["state"] == "accepted"
        current = await repository.get(first_id)
        assert current["tool_calls"] == 2
    finally:
        await client.aclose()
