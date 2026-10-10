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
from qq_ai_bot.control_plane.commands import ControlCommand
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.domain.identity import PrincipalId, RequestId
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.llm.base import LLMUnavailableError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_scheduler import WorkScheduler
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, scope, work
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
                {"action": "accept", "goal": "保存原工作结果"},
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
    [
        ("auto", False),
        ("auto", True),
        ("cancel", False),
        ("cancel", True),
        ("unknown", False),
        ("lost-response", False),
        ("lost-response-input", False),
        ("deferred-heartbeat", True),
        ("deferred-permanent", True),
        ("deferred-provider-retry", True),
        ("deferred-lost-response", False),
    ],
)
async def test_expired_original_work_recovers_from_original_dispatch_facts(
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
        if "lost-response" in action:
            control.session = session
            await control.reserve_request()
            await session.save("dispatched")
            outgoing = session.transcript.request()
            lost_request = replace(
                provider.requests[-1],
                messages=outgoing.messages,
                continuation=outgoing.continuation,
                continuation_items=outgoing.items,
                request_chain_id=session.transcript.chain_id,
            )
            response = await provider.complete(lost_request)
            assert response.tool_calls[0].id == "original-mutation"
        else:
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
    if action == "lost-response-input":
        input_id = await repository.enqueue(
            original["conversation_id"],
            original["generation"],
            "late-paid-loss-business",
            kind="message",
            work_id=original["id"],
            ready=False,
        )
        assert await repository.prepare_input(input_id, {"text": "late-authorized-business"})
        state.update({"slot": 1, "text": "unsubmitted-fresh-state", "expected_revision": 1})

        def fresh_response(request):
            serialized = json.dumps([item.content for item in request.messages], ensure_ascii=False)
            assert "late-authorized-business" in serialized
            assert "unsubmitted-fresh-state" in serialized
            assert request.request_chain_id != lost_request.request_chain_id
            assert request.continuation is None
            assert not any(item.tool_calls for item in request.messages)
            return _tool("task_control", {"action": "complete"}, "fresh-finish")

        provider._responder = fresh_response
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == original["id"]).values(state="running")
        )
        await writer.execute(
            update(scope)
            .where(scope.c.conversation_id == original["conversation_id"])
            .values(owner="dead-process", lease_until=0)
        )
    deferred = None
    if action.startswith("deferred-"):
        from qq_ai_bot.runtime.activation_outcome import WorkRecoveryDeferred
        from qq_ai_bot.runtime.work_activation import bind_work_activation

        owner = await repository.acquire(original["conversation_id"], original["generation"])

        async def validate_owner():
            assert await repository.valid(owner)

        control = WorkControl(
            repository,
            owner,
            original["source_key"],
            json.loads(original["source_json"]),
            validate_owner,
        )
        with pytest.raises(WorkRecoveryDeferred) as interrupted:
            async with bind_work_activation(control):
                control.current = await repository.get(original["id"])
                async with database.immediate_session() as writer:
                    await writer.execute(
                        update(scope)
                        .where(scope.c.conversation_id == original["conversation_id"])
                        .values(lease_until=0)
                    )
                if action == "deferred-permanent":
                    await control.recover_failure(ValueError("original preparation failure"))
                if action == "deferred-provider-retry":
                    await control.recover_failure(
                        LLMUnavailableError("original provider unavailable")
                    )
                raise WorkConflict("work_heartbeat_lease_expired")
        deferred = interrupted.value
        assert deferred.lease == owner and deferred.work["id"] == original["id"]
        if action in {"deferred-permanent", "deferred-provider-retry"}:
            assert isinstance(deferred.__cause__, WorkRecoveryDeferred)
            assert isinstance(deferred.__cause__.__cause__, (ValueError, LLMUnavailableError))
    await database.close()
    scheduler = original_scheduler(repository, env, harness, chat)
    requests_before = len(provider.requests)
    if action == "cancel":
        result = await public_action(database, original, "cancel")
        assert result.success
    if deferred is not None:
        await scheduler._resume.__self__._recover_preparation_failure(
            original, json.loads(original["source_json"]), deferred
        )
    else:
        await scheduler.drain_once()
    recovered = await repository.get(original["id"])
    if action == "lost-response-input":
        assert recovered["state"] == "queued"
        assert len(provider.requests) == requests_before
        async with database.sessions() as reader:
            failure = await reader.scalar(
                select(recovery.c.failure_json).where(recovery.c.work_id == original["id"])
            )
            retained = (await reader.execute(select(journal))).mappings().one()
            assert json.loads(failure)["code"] == "work_response_not_persisted"
            assert retained["phase"] == "dispatched"
        await scheduler.drain_once()
    if action == "unknown":
        provider._responder = lambda _: _tool(
            "task_control", {"action": "complete", "result": "unknown retained"}, "unknown-finish"
        )
    if action in {"auto", "unknown", "deferred-heartbeat", "deferred-provider-retry"}:
        assert recovered["state"] == "queued"
        assert recovered["reason"] == (
            "LLMUnavailableError"
            if action == "deferred-provider-retry"
            else "work_activation_interrupted"
        )
        assert len(provider.requests) == requests_before
        await asyncio.sleep(2.05)
        for _ in range(4):
            await scheduler.drain_once()
            if (await repository.get(original["id"]))["state"] == "completed":
                break
    final = await repository.get(original["id"])
    assert final["id"] == original["id"] and final["source_json"] == original["source_json"]
    assert final["state"] == (
        "cancelled"
        if action == "cancel"
        else "failed"
        if action in {"lost-response", "deferred-lost-response", "deferred-permanent"}
        else "completed"
    )
    assert state.snapshot()[0]["text"] == (
        "unsubmitted-fresh-state"
        if action == "lost-response-input"
        else "original-snapshot"
        if action in {"cancel", "unknown", "lost-response", "deferred-lost-response"}
        and not accepted_effect
        else "committed-original"
    )
    if action in {"cancel", "lost-response", "deferred-lost-response", "deferred-permanent"}:
        assert len(provider.requests) == requests_before
    if action in {"lost-response", "deferred-lost-response"}:
        assert final["reason"] == "work_response_not_persisted"
        async with database.sessions() as reader:
            retained = (await reader.execute(select(journal))).mappings().one()
            assert retained["work_id"] == original["id"] and retained["phase"] == "dispatched"
        await scheduler.drain_once()
        assert len(provider.requests) == requests_before
        assert (await repository.get(original["id"]))["model_requests"] == original[
            "model_requests"
        ]
    if action == "deferred-permanent":
        assert final["reason"] == "ValueError"
    if action == "lost-response-input":
        assert len(provider.requests) == requests_before + 1
        assert final["model_requests"] == original["model_requests"] + 1
        async with database.sessions() as reader:
            assert (
                await reader.scalar(select(inputs.c.state).where(inputs.c.id == input_id))
                == "consumed"
            )
    if action == "unknown":
        async with database.sessions() as reader:
            assert (
                await reader.scalar(select(effects.c.state).where(effects.c.effect_key == key))
                == "unknown"
            )
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
                {"action": "accept", "goal": f"work {count}"},
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


async def test_persistent_maintenance_errors_do_not_block_original_work_selection(
    database, tmp_path, monkeypatch
):
    from tests.unit.test_work_settlement_writer import _running_work

    from qq_ai_bot.runtime.protocol_store import ProtocolStore

    repository, lease, control = await _running_work(database, tmp_path)
    row = await repository.transition(
        lease, control.current["id"], control.current["revision"], "queued"
    )
    await repository.release(lease)
    attempts = []

    async def failed_reclaim():
        attempts.append("reclaim")
        raise ValueError("original maintenance failure")

    async def failed_cleanup(self):
        attempts.append("cleanup")
        raise RuntimeError("original protocol cleanup failure")

    resumed = []

    async def resume(item):
        resumed.append(item["id"])
        return None

    monkeypatch.setattr(repository, "reclaim_terminal", failed_reclaim)
    monkeypatch.setattr(ProtocolStore, "cleanup", failed_cleanup)
    scheduler = WorkScheduler(repository, resume, chat_admission_enabled=True)
    for _ in range(2):
        scheduler._last_reclaim = 0
        await scheduler.drain_once()
    assert resumed == [row["id"], row["id"]]
    assert attempts == ["reclaim", "cleanup", "reclaim", "cleanup"]
    assert (await scheduler.health())["maintenance_error_category"] == "RuntimeError"


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


@pytest.mark.parametrize(
    "principal,promoted", [("person", False), ("person", True), ("self", False)]
)
async def test_derived_automation_work_runs_on_original_scheduler_after_owner_settles(
    database, tmp_path, principal, promoted, monkeypatch
):
    from tests.support.automation_unified_delivery_helpers import sent, setup_run

    from qq_ai_bot.automation.models import RunStatus
    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel
    from qq_ai_bot.runtime.work_activation import current_work_control

    case = await setup_run(database, tmp_path, delivery="none", mode="silent", principal=principal)
    wait_for_timer = principal == "person" and not promoted

    def respond(request):
        if len(case.provider.requests) == 1:
            return _tool(
                "task_control", {"action": "derive", "goal": "original automation child"}, "derive"
            )
        if wait_for_timer and len(case.provider.requests) == 3:
            return _tool(
                "task_control",
                {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 0}]},
                "timer",
            )
        return _tool("task_control", {"action": "complete", "result": "done"}, "finish")

    case.provider._responder = respond
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        rows = list((await reader.execute(select(work))).mappings())
    parent = next(dict(row) for row in rows if row["parent_work_id"] is None)
    child = next(dict(row) for row in rows if row["parent_work_id"] == parent["id"])
    assert parent["state"] == "completed" and child["state"] == "queued"
    assert child["source_json"] == parent["source_json"]
    assert await case.repository.finish_run(
        case.run.id,
        status=RunStatus.SUCCEEDED,
        steps_completed=result.steps_completed,
        llm_calls=result.llm_calls,
        tool_calls=result.tool_calls,
        messages_sent=result.messages_sent,
        error_category=None,
        summary=result.summary,
        finished_at=case.clock.now(),
    )
    handlers = case.executor._registry.require("yuki.agent").handler.__self__
    if promoted:
        from uuid import uuid4

        from qq_ai_bot.automation.authority import PermissionLevel
        from qq_ai_bot.automation.control_context import resolve_execution_identity
        from qq_ai_bot.identity.db_models import IdentityBindingModel

        assert "9000" in case.executor._settings.superusers
        case.executor._settings.superusers_csv = "9000,9001"
        del case.executor._settings.__dict__["superusers"]
        await case.env.router.cas_takeover_person(case.env.person)
        now = case.clock.now()
        async with database.immediate_session() as writer:
            writer.add(
                IdentityBindingModel(
                    id=str(uuid4()),
                    person_id=case.env.person,
                    platform="qq",
                    external_account_id="9001",
                    status="active",
                    first_seen_at=now,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        async with database.sessions() as reader:
            actor, permission = await resolve_execution_identity(
                reader, case.executor._settings, owner_id=case.env.person
            )
        assert actor == "10001" and permission is PermissionLevel.SUPERUSER
    original_agent = handlers.agent
    resumed_authorities = []

    async def capture_agent(arguments, context, **kwargs):
        resumed_authorities.append(context.authority)
        return await original_agent(arguments, context, **kwargs)

    monkeypatch.setattr(handlers, "agent", capture_agent)
    resumer = make_work_resumer(
        repository,
        ledger=case.chat._ledger,
        scopes=case.chat._conversation_scopes,
        turns=case.chat._turn_coordinator,
        router=case.env.router,
        config=case.chat._runtime_config,
        generate_self=case.chat.generate_self_initiative,
        generate_wakeup=case.chat.generate_main_agent_wakeup,
        bindings=case.chat.runtime.bindings,
    )

    async def resume_automation(item, source):
        await handlers.resume_work(database, item, source)

    resumer.services = replace(
        resumer.services,
        settings=case.executor._settings,
        resume_automation=resume_automation,
    )
    identity = ConversationScope.group("80001", "20001")
    scene = await case.chat._conversation_scopes.get(identity)
    resolve = case.env.router.resolve_presence
    observed = False

    async def observe_before_work_binds(presence_id):
        nonlocal observed
        resolved = await resolve(presence_id)
        if not observed:
            observed = True
            assert current_work_control.get() is None
            await case.chat._ledger.append(
                bot_user_id="80001",
                platform_message_id="derived-automation-observation",
                scope_type=ScopeType.GROUP,
                sender_user_id="10001",
                group_id="20001",
                direction="inbound",
                content="新的普通群消息",
            )
            await case.chat._turn_coordinator.notify_message(
                scene.runtime_scope_key or identity.key, observation=True
            )
        return resolved

    monkeypatch.setattr(case.env.router, "resolve_presence", observe_before_work_binds)
    scheduler = WorkScheduler(repository, resumer.resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    requests_before = len(case.provider.requests)
    if wait_for_timer:
        claimed = await case.repository.claim_due(
            worker_id="independent-original-owner",
            now=case.run.scheduled_for,
            lease_seconds=case.executor._settings.automation_lease_seconds,
            limit=1,
        )
        assert len(claimed) == 1 and claimed[0].id == case.row.id
        async with database.sessions() as reader:
            owner_before = dict(
                (
                    await reader.execute(
                        select(AutomationModel.__table__).where(AutomationModel.id == case.row.id)
                    )
                )
                .mappings()
                .one()
            )
        assert owner_before["claimed_by"] == "independent-original-owner"
    await scheduler.drain_once()
    if wait_for_timer:
        from qq_ai_bot.runtime.work_wait import WorkWaitRepository

        assert (await repository.get(child["id"]))["state"] == "waiting_external"
        waits = WorkWaitRepository(repository)
        binding = await waits.describe(child["id"])
        assert await waits.deliver_due(now=binding["registered_at"] + 1) == 1
        async with database.sessions() as reader:
            owner_after = dict(
                (
                    await reader.execute(
                        select(AutomationModel.__table__).where(AutomationModel.id == case.row.id)
                    )
                )
                .mappings()
                .one()
            )
        assert owner_after == owner_before
        assert (await repository.get(parent["id"]))["state"] == "completed"
        await scheduler.drain_once()
    final = await repository.get(child["id"])
    assert final["state"] == "completed", (final, await scheduler.health())
    assert final["parent_work_id"] == parent["id"] and final["source_json"] == child["source_json"]
    assert len(case.provider.requests) == requests_before + (2 if wait_for_timer else 1)
    assert not sent(case.env)
    assert len(resumed_authorities) == (2 if wait_for_timer else 1)
    assert all(not authority.actor_is_superuser for authority in resumed_authorities)
    assert resumed_authorities[0].principal_kind == principal
    assert (await repository.get(parent["id"]))["state"] == "completed"
    async with database.sessions() as reader:
        runs = list(await reader.scalars(select(AutomationRunModel)))
        assert len(runs) == 1 and runs[0].id == case.run.id
        assert runs[0].status == "succeeded" and runs[0].finished_at is not None
        assert runs[0].llm_calls == result.llm_calls


@pytest.mark.parametrize("authority", ["original", "promoted", "revoked"])
async def test_derived_sdk_plugin_work_resumes_original_id_and_permissions(
    database, tmp_path, authority, monkeypatch
):
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.plugin_host.facades import (
        HostPluginContext,
        PluginFacadeServices,
        PluginInvocation,
    )
    from qq_ai_bot.plugin_host.main_turn import resume_plugin_work
    from qq_ai_bot.runtime.origin import TurnOrigin
    from yuki_plugin_sdk.permissions import PluginPermission

    provider = FakeLLMProvider(responder=lambda _: "original conversation observed")
    env, harness, chat, _, message = await _scene(database, tmp_path, provider)
    await harness.processor.handle(message, MemorySender())
    async with database.sessions() as reader:
        event = await reader.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
    scene = await chat._conversation_scopes.get(message.scope())
    inbound = replace(
        message, source_event_id=event.id, legacy_conversation_key=scene.runtime_scope_key
    )
    host = HostPluginContext(
        plugin_id="test.derived-sdk",
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=PluginFacadeServices(
            approval_revision="original-approved-version",
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    invocation = PluginInvocation(
        plugin_id=host.plugin_id,
        origin=TurnOrigin.PLUGIN_SESSION,
        actor_user_id=message.sender.user_id,
        bot_user_id=message.bot_user_id,
        inbound=inbound,
    )
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _tool(
                "task_control", {"action": "derive", "goal": "derived plugin goal"}, "derive"
            )
        if calls == 2 and authority == "original":
            return _tool(
                "task_control",
                {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 60}]},
                "wait-original",
            )
        return _tool("task_control", {"action": "complete", "result": "SDK done"}, "finish")

    provider._responder = respond
    prepared_context = {}
    assemble_plugin = chat._context_assembler.assemble_plugin

    async def capture_context(**kwargs):
        prepared_context.update(kwargs)
        return await assemble_plugin(**kwargs)

    with monkeypatch.context() as prepared:
        prepared.setattr(chat._context_assembler, "assemble_plugin", capture_context)
        async with host.bind(invocation):
            result = await host.agent.run("original plugin goal")
    assert result.ok and result.data["state"] == (
        "waiting_external" if authority == "original" else "completed"
    ), result
    repository = WorkRepository(database)
    parent = await repository.get(result.data["work_id"])
    async with database.sessions() as reader:
        child = dict(
            (await reader.execute(select(work).where(work.c.parent_work_id == parent["id"])))
            .mappings()
            .one()
        )
        event_ids = set(await reader.scalars(select(ChatEventModel.id)))
    assert child["state"] == "queued" and child["source_json"] == parent["source_json"]
    if authority == "original":
        from qq_ai_bot.services.context_assembler import ConversationCoverageError

        with pytest.raises(ConversationCoverageError, match="explicit compaction"):
            await assemble_plugin(**{**prepared_context, "capacity_budget": 0})
        waits = WorkWaitRepository(repository)
        wait_before = await waits.describe(parent["id"])
        with monkeypatch.context() as constrained:
            constrained.setattr(chat, "_history_input_budget", lambda *args, **kwargs: 0)
            constrained.setattr(chat._settings, "runtime_work_enabled", False)
            async with host.bind(invocation):
                waiting = await host.agent.run("original plugin goal")
            assert waiting.ok and waiting.data["state"] == "waiting_external", waiting
            assert waiting.data["work_id"] == parent["id"]
            assert waiting.data["model_requests"] == waiting.data["tool_calls_used"] == 0
            assert calls == 2 and await repository.get(parent["id"]) == parent
            assert await waits.describe(parent["id"]) == wait_before
        assert await waits.deliver_due(now=wait_before["registered_at"] + 61) == 1
        monkeypatch.setattr(chat._settings, "runtime_work_enabled", False)
        async with host.bind(invocation):
            result = await host.agent.run("original plugin goal")
        assert result.ok and result.data["state"] == "completed", result
        parent = await repository.get(parent["id"])
        assert calls == 3
        with monkeypatch.context() as constrained:
            constrained.setattr(chat, "_history_input_budget", lambda *args, **kwargs: 0)
            constrained.setattr(chat._settings, "runtime_work_enabled", False)
            for saved_result in ("text", "missing", "null"):
                checkpoint = json.loads(parent["checkpoint_json"])
                if saved_result == "missing":
                    checkpoint.pop("sync_result")
                elif saved_result == "null":
                    checkpoint["sync_result"] = None
                async with database.immediate_session() as writer:
                    await writer.execute(
                        update(work)
                        .where(work.c.id == parent["id"])
                        .values(checkpoint_json=json.dumps(checkpoint))
                    )
                before_read = await repository.get(parent["id"])
                async with host.bind(invocation):
                    repeated = await host.agent.run("original plugin goal")
                assert repeated.ok and repeated.data["state"] == "completed", repeated
                assert repeated.data["work_id"] == parent["id"]
                assert repeated.data["text"] == ("SDK done" if saved_result == "text" else "")
                assert repeated.data["model_requests"] == repeated.data["tool_calls_used"] == 0
                assert calls == 3 and await repository.get(parent["id"]) == before_read
    revoked = authority == "revoked"
    if revoked:
        host._approved_permissions = frozenset()
    elif authority == "promoted":
        host._superuser_ids = frozenset({message.sender.user_id})
    main = chat.runtime.main_turns
    original_run = main.run
    resumed_authorities = []

    async def capture_run(messages, runtime, backend):
        resumed_authorities.append(runtime.actor_is_superuser)
        return await original_run(messages, runtime, backend)

    monkeypatch.setattr(main, "run", capture_run)
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

    async def resume_plugin(item, source):
        await resume_plugin_work(lambda plugin_id: host, harness.ledger, item, source)

    resumer.services = replace(resumer.services, resume_plugin=resume_plugin)
    scheduler = WorkScheduler(repository, resumer.resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    await scheduler.drain_once()
    final = await repository.get(child["id"])
    assert final["state"] == ("failed" if revoked else "completed"), (
        final,
        await scheduler.health(),
    )
    assert final["parent_work_id"] == parent["id"] and final["source_json"] == child["source_json"]
    assert calls == (2 if revoked else 4 if authority == "original" else 3)
    assert resumed_authorities == ([] if revoked else [False])
    assert (await repository.get(parent["id"]))["state"] == "completed"
    if not revoked:
        assert (await host.agent.result(child["id"])).data["text"] == "SDK done"
    async with database.sessions() as reader:
        assert set(await reader.scalars(select(ChatEventModel.id))) == event_ids
        assert len(list(await reader.scalars(select(work.c.id)))) == 2
    await host.close_host_resources()


@pytest.mark.parametrize("path", ["derived", "revoked", "failed-root"])
async def test_retained_background_plugin_work_resumes_after_job_settles_without_reopening_it(
    database, tmp_path, path
):
    from datetime import UTC, datetime

    from tests.support.background_authority import approve_background_plugin

    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker
    from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel
    from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

    provider = FakeLLMProvider()
    env, harness, chat, _, _ = await _scene(database, tmp_path, provider)
    plugin_id = "test.derived-background"
    notifications = await approve_background_plugin(
        database,
        plugin_id=plugin_id,
        bot_user_id="80001",
        group_id="20001",
        creator_user_id="10001",
    )
    target = NotificationTarget(target_type="group", target_id="20001")
    receipt = await notifications.publish(
        plugin_id=plugin_id,
        request=PublishNotificationRequest(
            event_key="original-background-event",
            event_type="fixture",
            external_source="fixture",
            target=target,
            occurred_at=datetime.now(UTC),
            summary="original external event",
            ask_agent=True,
            agent_intent="original background goal",
        ),
    )
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if path == "failed-root":
                return _tool("task_control", {"action": "fail"}, "original-root-failure")
            return _tool(
                "task_control", {"action": "derive", "goal": "derived background goal"}, "derive"
            )
        return _tool("task_control", {"action": "complete", "result": "background done"}, "finish")

    provider._responder = respond
    worker = PluginBackgroundTurnWorker(
        repository=notifications,
        ledger=harness.ledger,
        runtime_config=chat._runtime_config,
        chat=chat,
        turns=chat._turn_coordinator,
        conversation_scopes=chat._conversation_scopes,
        router=env.router,
    )
    job = await notifications.claim_turn()
    assert job and job.source_event_id == receipt.source_event_id
    await worker._execute(job)
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        stored_job = dict(
            (await reader.execute(select(PluginBackgroundTurnJobModel.__table__))).mappings().one()
        )
        parent = await repository.get(stored_job["work_id"])
        selected = (
            parent
            if path == "failed-root"
            else dict(
                (await reader.execute(select(work).where(work.c.parent_work_id == parent["id"])))
                .mappings()
                .one()
            )
        )
        event_ids = set(await reader.scalars(select(ChatEventModel.id)))
    if path == "failed-root":
        assert stored_job["status"] == "failed" and parent["state"] == "failed"
        assert (await public_action(database, selected, "resume")).success
        assert await notifications.claim_turn() is None
    else:
        assert stored_job["status"] == "completed" and parent["state"] == "completed"
        assert selected["state"] == "queued" and selected["source_json"] == parent["source_json"]
    revoked = path == "revoked"
    if revoked:
        assert await notifications.revoke_target(plugin_id=plugin_id, target=target)
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
    resumer.services = replace(resumer.services, resume_plugin=worker.resume_work)
    scheduler = WorkScheduler(repository, resumer.resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    calls_before = calls
    owner_lease = await repository.acquire(selected["conversation_id"], selected["generation"])
    await scheduler.drain_once()
    assert calls == calls_before
    await repository.release(owner_lease)
    await scheduler.drain_once()
    final = await repository.get(selected["id"])
    assert final["state"] == ("failed" if revoked else "completed"), (
        final,
        await scheduler.health(),
    )
    assert final["source_json"] == selected["source_json"]
    assert final["parent_work_id"] == selected["parent_work_id"]
    assert final["model_requests"] == selected["model_requests"] + int(not revoked)
    assert calls == calls_before + int(not revoked)
    async with database.sessions() as reader:
        final_job = dict(
            (await reader.execute(select(PluginBackgroundTurnJobModel.__table__))).mappings().one()
        )
        assert final_job == stored_job
        assert set(await reader.scalars(select(ChatEventModel.id))) == event_ids
        assert len(list(await reader.scalars(select(work.c.id)))) == (
            1 if path == "failed-root" else 2
        )
