"""Isolated real SELF lifecycle probe; FakeLLM, no Jev/QQ/network calls."""

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.runtime_execution import make_work_resumer
from tests.support.semantic_participation_host_helpers import _event_and_route, _host

from qq_ai_bot.conversation.canonical_db_models import SpaceActiveRouteModel
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.identity.canonical_repository import ensure_presence, ensure_space
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, SpaceBindingModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_management import manage_work
from qq_ai_bot.runtime.work_scheduler import WorkScheduler
from qq_ai_bot.runtime.work_schema_v1 import scope
from qq_ai_bot.runtime.work_wait import WorkWaitRepository

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "scenario", ["silent", "result", "time_due", "api_time_due", "fail", "cancel"]
)
async def test_intrinsic_to_real_chat_and_terminal_feedback(database, tmp_path, scenario):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        space = await ensure_space(session, "1049765710")
        presence = await ensure_presence(session, "8000")
        conv = await ensure_canonical_conversation(
            session, kind="space", primary_scope_key="bot:8000:group:1049765710", space_id=space
        )
        scene_space = await session.get(CanonicalSpaceModel, space)
        scene_space.enabled = scene_space.autonomous_enabled = True
        binding = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.space_id == space, SpaceBindingModel.status == "active"
            )
        )
        session.add(
            SpaceActiveRouteModel(
                space_id=space,
                space_binding_id=binding.id,
                presence_id=presence,
                route_generation=1,
                paused=False,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )

    host, _ = await _host(database, tmp_path, observer=False)
    turns = 0
    tool_feedback = []

    def respond(request):
        nonlocal turns
        turns += 1
        tool_feedback.extend(m.content for m in request.messages if m.role == "tool")
        action = None
        if scenario == "time_due" and turns == 1:
            action = {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 30}]}
        elif scenario == "fail":
            action = {"action": "fail", "reason": "isolated explicit failure"}
        if action:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"{scenario}-{turns}",
                        function=ToolFunction(name="task_control", arguments=json.dumps(action)),
                    ),
                ),
            )
        return "NO_REPLY" if scenario == "silent" else "isolated verified internal result"

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=True, enabled_groups_csv="1049765710"),
        provider,
    )
    chat = harness.processor._chat
    bind_main_contract(harness, tmp_path)
    repo = host.work
    router = SimpleNamespace(
        resolve_presence=AsyncMock(
            return_value=SimpleNamespace(
                connection=SimpleNamespace(
                    snapshot="isolated-gateway", bot=SimpleNamespace(call_api=AsyncMock())
                )
            )
        )
    )
    resumer = make_work_resumer(
        repo,
        ledger=harness.ledger,
        scopes=harness.conversation_scopes,
        turns=chat._turn_coordinator,
        router=router,
        config=chat._runtime_config,
        generate_self=chat.generate_self_initiative,
        generate_wakeup=AsyncMock(side_effect=AssertionError("no inbound turn")),
        validate_snapshot=chat.validate_turn_snapshot,
        bindings=chat.runtime.bindings,
    )
    scheduler = WorkScheduler(repo, resumer.resume, chat_admission_enabled=True)
    records = []

    async def observe(label, run_id, task_id):
        task = await repo.get(task_id)
        run = await host.repository.get_run(run_id)
        async with database.sessions() as session:
            lease = (
                (
                    await session.execute(
                        select(scope).where(scope.c.conversation_id == conv.conversation_id)
                    )
                )
                .mappings()
                .one()
            )
        records.append(
            {
                "label": label,
                "work_state": task["state"],
                "run_state": run.state,
                "work_id": task_id,
                "run_id": run_id,
                "model_requests": task["model_requests"],
                "provider_requests": len(provider.requests),
                "lease_owner": lease["owner"],
                "lease_until": lease["lease_until"],
                "feedback_sequence": run.feedback_sequence,
                "reason": task["reason"],
                "scheduler_error": scheduler._last_error,
                "checkpoint": task["checkpoint_json"],
            }
        )

    try:
        # A synthetic *historical* human event supplies the controller's activity
        # dynamics; it is never the actor/trigger for the SELF execution.
        await _event_and_route(
            database,
            host.app.ledger,
            group="1049765710",
            content="isolated historical context, no invitation",
        )
        item = await host._session(await host._scene(conv.conversation_id))
        await host._hydrate(item)
        binding = await host._binding(item)
        t = time.time()
        item.controller.advance(
            t,
            controller_epoch=binding.controller_epoch,
            host_available=True,
            intrinsic_allowed=True,
            include_addressed=False,
        )
        # Deterministic arrival fixture only; real Controller builds the Proposal.
        item.controller._sample = lambda sequence, stream: 0.0
        proposal = item.controller.advance(
            t + 0.01,
            controller_epoch=binding.controller_epoch,
            host_available=True,
            intrinsic_allowed=True,
            include_addressed=False,
        )
        assert proposal is not None and proposal.kind.value == "intrinsic"
        admitted = await host._admit_proposal(item, binding, proposal)
        assert admitted["outcome"] == "accepted", admitted
        run_id = admitted["run_id"]
        assert await repo.by_source(f"initiative:{run_id}") is None
        await host._reconcile_outbox()
        task = await repo.by_source(f"initiative:{run_id}")
        assert task["state"] == "queued"
        task_id = task["id"]
        await observe("outbox_queued", run_id, task_id)
        if scenario in {"api_time_due", "cancel"}:
            # Lower-level public lifecycle test before ChatService's source
            # enrichment. This does NOT claim model-issued wait works.
            original_source = json.loads(task["source_json"])
            async with activate_work(
                repo,
                conv.conversation_id,
                1,
                task["source_key"],
                original_source,
                AsyncMock(),
                work_id=task_id,
            ) as control:
                receipt = json.loads(
                    await control.execute(
                        "task_control",
                        {
                            "action": "wait",
                            "conditions": [{"kind": "time_due", "after_seconds": 30}],
                        },
                        "api-wait-before-model",
                    )
                )
                assert receipt["ok"], receipt
            await host._reconcile_outbox()
            await observe("api_wait_registered", run_id, task_id)
            assert (await repo.get(task_id))["state"] == "waiting_external"
            if scenario == "api_time_due":
                assert await WorkWaitRepository(repo).deliver_due(time.time() + 60) == 1
                await observe("time_signal_queued", run_id, task_id)
                await scheduler.drain_once()
            else:
                current = await repo.get(task_id)
                async with database.immediate_session() as session:
                    await manage_work(session, task_id, current["revision"], "cancel")
            await host._reconcile_outbox()
            await observe("final_activation", run_id, task_id)
        else:
            await scheduler.drain_once()
            await host._reconcile_outbox()
            await observe("first_activation", run_id, task_id)
        if scenario == "time_due":
            assert (await repo.get(task_id))["state"] == "waiting_external"
            assert await WorkWaitRepository(repo).describe(task_id) is not None
            assert await WorkWaitRepository(repo).deliver_due(time.time() + 60) == 1
            await observe("model_time_signal_queued", run_id, task_id)
            await scheduler.drain_once()
            await host._reconcile_outbox()
            await observe("model_wait_resumed_and_completed", run_id, task_id)
        expected = (
            "failed" if scenario == "fail" else "cancelled" if scenario == "cancel" else "completed"
        )
        final = await repo.get(task_id)
        assert final["state"] == expected, records
        run = await host.repository.get_run(run_id)
        assert run.state == ("interrupted" if scenario in {"fail", "cancel"} else "no_reply"), (
            records
        )
        assert records[-1]["lease_owner"] is None and records[-1]["lease_until"] == 0
        assert len(provider.requests) == (
            0 if scenario == "cancel" else 2 if scenario == "time_due" else 1
        )
        assert final["model_requests"] == len(provider.requests)
        assert item.controller.state.pending is None
        assert (await router.resolve_presence()).connection.bot.call_api.await_count == 0
        # A terminal run must release the host's admission, not just its lease.
        next_proposal = item.controller.advance(
            max(time.time(), item.controller.state.now) + 0.01,
            controller_epoch=binding.controller_epoch,
            host_available=True,
            intrinsic_allowed=True,
            include_addressed=False,
        )
        assert next_proposal is not None
        next_admission = await host._admit_proposal(item, binding, next_proposal)
        assert next_admission["outcome"] == "accepted", next_admission
        evidence = {
            "scenario": scenario,
            "records": records,
            "tool_feedback": tool_feedback,
            "next_admission": next_admission,
            "provider": "FakeLLM",
            "qq_calls": 0,
            "model_wait_result": "registered_and_resumed"
            if scenario == "time_due"
            else "not_attempted",
        }
        (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf8")
    finally:
        await scheduler.close()
        await host.close()
