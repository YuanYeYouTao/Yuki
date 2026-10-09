"""Real accepted inputs retain their identity, journal and receipts across recovery."""

import asyncio
import json
import time
from dataclasses import replace

import pytest
from sqlalchemy import select, update
from tests.conftest import MemorySender
from tests.support.runtime_execution import make_work_resumer
from tests.support.work_session import WorkSession
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from qq_ai_bot.control_plane.command_service import ControlCommandService
from qq_ai_bot.control_plane.command_types import ControlCommandError
from qq_ai_bot.control_plane.commands import ControlCommand
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_scheduler import WorkScheduler
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, scope, work
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def public_action(database, row, action):
    principal = ControlPrincipal(
        principal_id=PrincipalId.new(),
        person_id=None,
        source=PrincipalSource.CLI,
        granted_capabilities=("control.work.mutate",),
        authenticated=True,
        active=True,
    )
    request = RequestId.new()
    command = ControlCommand(
        request_id=request,
        expected_revision=row["revision"],
        payload={"resource_id": row["id"], "action": action},
    )
    context = DecisionContext(
        request_id=request,
        principal=principal,
        source=PrincipalSource.CLI,
        canonical_target=row["id"],
    )
    return await ControlCommandService(ControlCommandAdapter(database)).mutate_work(
        context, command
    )


def original_scheduler(repository, env, harness, chat):
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
    scheduler = WorkScheduler(repository, resumer.resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    return scheduler


def original_responses(provider):
    def respond(request):
        count = len(provider.requests)
        if count == 1:
            return _tool(
                "task_control",
                {"action": "accept", "goal": "保存原工作结果", "output_kind": "state_change"},
                "original-accept",
            )
        if count == 2:
            return _tool(
                "update_short_state",
                {"slot": 1, "text": "committed-original", "expected_revision": 1},
                "original-mutation",
            )
        if count == 3:
            return _tool("task_control", {"action": "complete"}, "original-finish")
        raise AssertionError("unexpected additional dispatch")

    return respond


@pytest.mark.parametrize(
    ("action", "accepted_effect"),
    [("auto", False), ("auto", True), ("cancel", False), ("cancel", True), ("unknown", False)],
)
async def test_expired_original_work_recovers_without_new_input(
    database, tmp_path, accepted_effect, action
):
    provider = FakeLLMProvider()
    provider._responder = original_responses(provider)
    env, harness, chat, state, message = await _scene(
        database, tmp_path, provider, request_limit=2 if accepted_effect else 1
    )
    await harness.processor.handle(message, MemorySender())
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
        prior_effects = [dict(row) for row in (await reader.execute(select(effects))).mappings()]
    assert original["state"] == "queued"
    if not accepted_effect:
        lease = await repository.acquire(original["conversation_id"], original["generation"])

        async def validate():
            assert await repository.valid(lease)

        control = WorkControl(
            repository, lease, original["source_key"], json.loads(original["source_json"]), validate
        )
        control.current = original
        async with database.sessions() as reader:
            contract = await reader.scalar(
                select(journal.c.contract).where(journal.c.work_id == original["id"])
            )
        session = WorkSession(control, contract)
        await session.restore(TurnTranscript((ChatMessage("user", "original task"),)))
        call = ToolCall(
            "original-mutation",
            ToolFunction(
                "update_short_state",
                json.dumps({"slot": 1, "text": "committed-original", "expected_revision": 1}),
            ),
        )
        session.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
        await session.save("response", (call,))
        if action == "unknown":
            key = session.call_key(call.id)
            await repository.prepare_effect(lease, original["id"], key, "tool")
            await repository.record_effect(
                key,
                "unknown",
                {
                    "outcome": {
                        "tool": "update_short_state",
                        "ok": False,
                        "uncertain": True,
                        "side_effecting": True,
                        "executed": True,
                    },
                    "error": "dispatch result lost",
                },
            )
        await repository.release(lease)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == original["id"]).values(state="running")
        )
        await writer.execute(
            update(scope)
            .where(scope.c.conversation_id == original["conversation_id"])
            .values(owner="dead-process", lease_until=0)
        )
    await database.close()
    scheduler = original_scheduler(repository, env, harness, chat)
    requests_before = len(provider.requests)
    if action == "cancel":
        result = await public_action(database, original, "cancel")
        assert result.success
    await scheduler.drain_once()
    recovered = await repository.get(original["id"])
    if action == "unknown":
        assert recovered["state"] == "suspended"
        with pytest.raises(ControlCommandError) as denied:
            await public_action(database, recovered, "resume")
        assert denied.value.problem.code.value == "precondition_failed"
        assert len(provider.requests) == requests_before
        async with database.sessions() as reader:
            assert (
                await reader.scalar(select(effects.c.state).where(effects.c.effect_key == key))
                == "unknown"
            )
        return
    if action == "auto":
        assert (
            recovered["state"] == "queued" and recovered["reason"] == "work_activation_interrupted"
        )
        assert len(provider.requests) == requests_before
        await asyncio.sleep(2.05)
        for _ in range(4):
            await scheduler.drain_once()
            if (await repository.get(original["id"]))["state"] == "completed":
                break
    final = await repository.get(original["id"])
    assert final["id"] == original["id"] and final["source_json"] == original["source_json"]
    assert final["state"] == ("completed" if action == "auto" else "cancelled")
    assert state.snapshot()[0]["text"] == (
        "original-snapshot" if action == "cancel" and not accepted_effect else "committed-original"
    )
    if action == "cancel":
        assert len(provider.requests) == requests_before
    async with database.sessions() as reader:
        final_effects = [dict(row) for row in (await reader.execute(select(effects))).mappings()]
    for prior in prior_effects:
        assert (
            next(row for row in final_effects if row["effect_key"] == prior["effect_key"]) == prior
        )


async def test_exact_33rd_input_is_selected_after_32_paused_works(database, tmp_path):
    provider = FakeLLMProvider()

    def respond(request):
        count = len(provider.requests)
        if count <= 33:
            return _tool(
                "task_control",
                {"action": "accept", "goal": f"work {count}", "output_kind": "state_change"},
                "accept-input",
            )
        if count == 34:
            return _tool(
                "update_short_state",
                {"slot": 1, "text": "selected-original-33", "expected_revision": 1},
                "commit-33",
            )
        return _tool("task_control", {"action": "complete"}, "finish-33")

    provider._responder = respond
    env, harness, chat, _, message = await _scene(database, tmp_path, provider, request_limit=1)
    harness.processor._rate_limiter = SlidingWindowRateLimiter(per_user=1000, per_group=1000)
    repository = WorkRepository(database)
    for number in range(33):
        incoming = replace(
            message, message_id=f"capacity-input-{number}", text=f"distinct input {number}"
        )
        await harness.processor.handle(incoming, MemorySender())
        async with database.sessions() as reader:
            current = dict(
                (await reader.execute(select(work).order_by(work.c.created.desc()).limit(1)))
                .mappings()
                .one()
            )
        assert current["state"] == "queued"
        if number < 32:
            lease = await repository.acquire(current["conversation_id"], current["generation"])
            await repository.transition(
                lease,
                current["id"],
                current["revision"],
                "suspended",
                reason="model_request_capacity",
            )
            await repository.release(lease)
    scheduler = original_scheduler(repository, env, harness, chat)
    await scheduler.drain_once()
    await scheduler.drain_once()
    final = await repository.get(current["id"])
    assert final["state"] == "completed" and final["source_json"] == current["source_json"]
    assert (
        len(provider.requests) == 35 and final["model_requests"] == 3 and final["tool_calls"] == 1
    )


@pytest.mark.parametrize("entry", ["public", "hard_boundary"])
async def test_cancel_retires_only_stale_control_preserving_original_facts(
    database, tmp_path, entry
):
    from tests.unit.test_work_settlement_writer import _running_work

    repository, lease, control = await _running_work(database, tmp_path)
    identity = control.current["id"]
    await repository.checkpoint(
        lease, identity, {"sync_result": "preserved result", "other": "retained"}
    )
    await repository.accept_control(lease, identity, {"action": "complete", "ending": "completed"})
    key = f"{identity}:real-mutation"
    await repository.prepare_effect(lease, identity, key, "tool")
    await repository.record_effect(
        key, "accepted", {"outcome": {"ok": True, "mutation_committed": True}}
    )
    async with database.sessions() as reader:
        prior = dict(
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )
    await repository.release(lease)
    if entry == "public":
        assert (await public_action(database, await repository.get(identity), "cancel")).success
    else:
        await repository.cancel(lease.conversation_id)
    final = await repository.get(identity)
    checkpoint = json.loads(final["checkpoint_json"])
    assert final["state"] == "cancelled" and "accepted_control" not in checkpoint
    assert checkpoint["sync_result"] == "preserved result" and checkpoint["other"] == "retained"
    async with database.sessions() as reader:
        assert (
            dict(
                (await reader.execute(select(effects).where(effects.c.effect_key == key)))
                .mappings()
                .one()
            )
            == prior
        )


async def test_wait_accepts_derived_source_metadata_but_fences_other_principal(database, tmp_path):
    from tests.unit.test_work_settlement_writer import _running_work

    repository, lease, control = await _running_work(database, tmp_path)
    waits = WorkWaitRepository(repository)
    args = dict(
        work_id=control.current["id"],
        mode="any",
        conditions=[{"kind": "time_due", "after_seconds": 1}],
        deadline_at=None,
    )
    registered = await waits.register(
        lease,
        source={**control.source, "work_id": control.current["id"], "derived": True},
        call_key="derived-source",
        **args,
    )
    assert registered["id"] and registered["status"] == "active"
    with pytest.raises(WorkConflict, match="wait_work_source_changed"):
        await waits.register(
            lease,
            source={**control.source, "actor_person_id": "different-person"},
            call_key="changed-owner",
            **args,
        )


async def test_communication_query_accepts_original_custom_report_kind(database, tmp_path):
    from tests.unit.test_work_settlement_writer import _running_work

    repository, lease, control = await _running_work(database, tmp_path)
    key = f"{control.current['id']}:custom-report"
    target = {"kind": "person", "id": control.source["actor_person_id"]}
    await repository.prepare_effect(lease, control.current["id"], key, "tool")
    await repository.record_effect(
        key,
        "accepted",
        {
            "outcome": {
                "tool": "send_message",
                "ok": True,
                "delivered_message": True,
                "delivery_target": target,
                "work_report": {"kind": "phase_verified"},
            }
        },
    )
    reports = await repository.communication_reports(
        lease, control.current["id"], target, kind="phase_verified", delivered_only=True
    )
    assert len(reports) == 1 and reports[0]["effect_key"] == key


async def test_message_recovery_uses_original_event_account_and_canonical_owner(database, tmp_path):
    from qq_ai_bot.services.execution_sources import recover_source

    provider = FakeLLMProvider()
    provider._responder = original_responses(provider)
    _, harness, _, _, message = await _scene(database, tmp_path, provider, request_limit=1)
    await harness.processor.handle(message, MemorySender())
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
    source = json.loads(original["source_json"])
    recovered = await recover_source(
        database,
        original["conversation_id"],
        {**source, "actor_user_id": "old-derived-account"},
        request_id=original["id"],
    )
    assert recovered.actor_user_id == message.sender.user_id
    assert recovered.actor_person_id == source["actor_person_id"]
    assert recovered.event_id == source["trigger_event_id"]
    with pytest.raises(ValueError, match="task_source_identity_unavailable"):
        await recover_source(
            database,
            original["conversation_id"],
            {**source, "actor_person_id": "different-owner"},
            request_id=original["id"],
        )
