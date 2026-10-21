"""Real Processor/Runner/SQLite/WorkResumer/adapters and pinned worker; inert domain."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from tests.conftest import MemorySender
from tests.integration.test_codemode_lifecycle_recovery import FakeEngine, FakeRun
from tests.support.codemode_cases import FakeDomain, build_host, requires_worker
from tests.support.correctness_wire import wire
from tests.support.event_seed import seed_event
from tests.support.projection import projection_record
from tests.support.public_history import _publish_checkpoint
from tests.support.runtime_execution import make_work_resumer
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from yuki.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.conversation.projections import PromptProjectionRepository
from yuki.domain.messages import ChatResponse
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, work
from yuki.services.main_agent_turns import _hash

MARK = "CHILD_NOTE_BEFORE_PARENT_SETTLEMENT"
ORD = "ORDINARY_INTERLEAVED_PUBLIC_MARKER"
OTHER = "OTHER_WORK_OBSERVED_MARKER"
SUMMARY = "ORIGINAL_SELECTED_PUBLIC_SUMMARY"
LATE = "PUBLIC_EVENT_AFTER_SELECTED_BOUNDARY"


@pytest.mark.parametrize(
    ("native", "evict_projection"),
    [
        (False, False),
        (False, True),
        pytest.param(True, False, marks=[requires_worker, pytest.mark.native_worker]),
    ],
)
@pytest.mark.parametrize("pause", ["pending", "settled"])
@pytest.mark.parametrize("kind", ["chat", "responses", "anthropic", "gemini"])
@pytest.mark.asyncio
async def test_pending_code_interleave(
    database, tmp_path, monkeypatch, kind, pause, native, evict_projection
):
    provider = FakeLLMProvider()

    def respond(request):
        n = len(provider.requests)
        if n in (1, 5):
            return _tool(
                "task_control",
                dict(
                    action="accept",
                    goal="W1" if n == 1 else "W2",
                    reporting="quiet",
                ),
                "accept-" + str(n),
            )
        if n == 2:
            return _tool(
                "execute_code",
                {
                    "code": "await yuki_task_control({'action': 'update', 'context_note': "
                    "{'version': 1, 'facts': [{'text': '" + MARK + "', 'refs': ['goal']}], "
                    "'unresolved': [], 'next_steps': []}})\n"
                    "await yuki_query_chat_history({'view': 'search', 'query': 'inert-first'})\n"
                    "await yuki_query_chat_history({'view': 'search', 'query': 'inert-second'})\n"
                    "'PROGRAM_RESULT_' + 'EXACTLY_ONCE'",
                    "inputs": {},
                },
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
        if n == 9:
            return ChatResponse("ordinary follow-up", 0)
        assert n in (4, 8), n
        return ChatResponse("ordinary done" if n == 4 else "resume result", 0)

    if not native:
        original_event = FakeRun.event

        def fake_event(run):
            event = original_event(run)
            return (
                replace(event, output="PROGRAM_RESULT_EXACTLY_ONCE")
                if event.status == "completed"
                else event
            )

        calls = [
            (
                "yuki_task_control",
                {
                    "action": "update",
                    "context_note": {
                        "version": 1,
                        "facts": [{"text": MARK, "refs": ["goal"]}],
                        "unresolved": [],
                        "next_steps": [],
                    },
                },
            ),
            ("yuki_query_chat_history", {"view": "search", "query": "inert-first"}),
            ("yuki_query_chat_history", {"view": "search", "query": "inert-second"}),
        ]
        monkeypatch.setattr(FakeRun, "event", fake_event)

    provider._responder = respond
    env, harness, chat, _, inbound = await _scene(
        database, tmp_path, provider, request_limit=2, code_enabled=True
    )
    if evict_projection:
        await _publish_checkpoint(database, env.context.conversation_id, 1, text=SUMMARY)
        async with database.immediate_session() as writer:
            await writer.execute(
                update(PromptProjectionModel)
                .where(PromptProjectionModel.view_key == env.context.conversation_id)
                .values(context_key=_hash([env.context.conversation_id, 1]))
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
        if not native:
            e.host.worker = SimpleNamespace(execution_digest=lambda: "fake-engine-for-audit")
            e.host.engine_factory = lambda *_args: FakeEngine(calls=calls)
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
                    projection=[(await projection_record(r, p)).payload_json for p in projs],
                    notes=[dict(id=n.id, payload=n.payload_json) for n in notes],
                    selections=[dict(id=s.id, event_ids=s.event_ids_json) for s in selections],
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
            bindings=chat.runtime.bindings,
        )
        if evict_projection:
            await PromptProjectionRepository(database).invalidate_view(
                env.context.conversation_id, reason="capacity"
            )
        injected = []
        if evict_projection and kind == "responses" and pause == "pending":
            original_delta = EventLedgerRepository.read_scope_delta

            async def append_after_selection(ledger, *args, **kwargs):
                version, rows = await original_delta(ledger, *args, **kwargs)
                if rows and not injected:
                    event, created = await seed_event(
                        harness.ledger,
                        bot_user_id=inbound.bot_user_id,
                        group_id=inbound.group_id,
                        scope_type=inbound.scope_type,
                        sender_user_id=inbound.sender.user_id,
                        platform_message_id="late-after-boundary-selection",
                        direction="inbound",
                        content=LATE,
                    )
                    assert created and event.id > max(row.id for row in rows)
                    injected.append(event.id)
                return version, rows

            monkeypatch.setattr(EventLedgerRepository, "read_scope_delta", append_after_selection)
        await database.close()
        error = await resumer.resume(await repository.get(first_id))
        assert error is None, error
        await snap("original_work_resumed")
        assert len(wires) == 8
        assert domain.log == [
            ("query_chat_history", {"view": "search", "query": "inert-first"}),
            ("query_chat_history", {"view": "search", "query": "inert-second"}),
        ]
        # A neutral turn may use current public history, but does not publish
        # another Work's private note into ordinary input.
        assert MARK not in json.dumps(wires[2], ensure_ascii=False)
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
        assert ORD in final, (
            "Settled Code Mode resume must use currently authorized interleaved history"
        )
        assert ORD in next_round
        if injected:
            assert LATE not in final and LATE in next_round
        assert OTHER not in final and OTHER not in next_round
        assert MARK in final and MARK in next_round
        assert final.count("PROGRAM_RESULT_EXACTLY_ONCE") == 1
        assert next_round.count("PROGRAM_RESULT_EXACTLY_ONCE") == 1
        before = json.loads(snapshots[0]["projection"][0])
        after = json.loads(snapshots[-1]["projection"][0])
        if not evict_projection:
            assert after[: len(before)] == before
        parent = [
            r for r in snapshots[-1]["effects"] if json.loads(r["receipt_json"]).get("composition")
        ]
        assert len(parent) == 1 and parent[0]["state"] == "accepted"
        current = await repository.get(first_id)
        assert current["tool_calls"] == 2
        if evict_projection:
            restored_public = await PromptProjectionRepository(database).read(
                env.context.conversation_id
            )
            assert restored_public.selected_summary_text == SUMMARY
            assert restored_public.selected_summary_coverage == 1
            assert "W1 initial request" in restored_public.payload_json
        await harness.processor.handle(
            replace(inbound, message_id="ordinary-after-resume", text="after recovery"),
            MemorySender(),
        )
        ordinary = json.dumps(wires[-1], ensure_ascii=False)
        assert len(wires) == 9
        assert "W1 initial request" in ordinary and ORD in ordinary
        assert MARK not in ordinary and OTHER not in ordinary
        if evict_projection:
            assert SUMMARY in ordinary
    finally:
        await client.aclose()
