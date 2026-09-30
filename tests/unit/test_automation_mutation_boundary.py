"""Creation policy and required audit commit with the domain mutation."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from tests.conftest import make_settings
from tests.unit.test_automation_runtime import FakeClock, _inbound, _script
from tests.unit.test_self_initiative_runtime import self_source

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.identity.db_models import PresenceModel
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    AutomationModel,
    AutomationVersionModel,
)
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.time.service import TimeContextService


def service_for(database, maximum=5, *, runtime_work=False):
    return AutomationService(
        settings=make_settings(
            database.url,
            automation_enabled=True,
            automation_max_active_per_user=maximum,
            runtime_work_enabled=runtime_work,
        ),
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(
            database, clock=FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        ),
        audit=AdminAuditService(database),
    )


async def test_failed_wait_poll_does_not_block_automation_claim(database, monkeypatch):
    service = service_for(database)
    worker = AutomationWorker(
        settings=service._settings,
        repository=service._repository,
        executor=None,
        time_service=service._time,
    )
    claimed = []

    async def broken_wait(now):
        raise ValueError("invalid_wait_binding")

    async def claim_due(**kwargs):
        claimed.append(kwargs)
        worker._stop.set()
        return ()

    worker._waits = SimpleNamespace(deliver_due=broken_wait)
    monkeypatch.setattr(worker._repository, "claim_due", claim_due)
    await asyncio.wait_for(worker._loop(), timeout=1)
    assert len(claimed) == 1


async def test_creation_capacity_is_atomic_and_same_key_replays_when_full(database, monkeypatch):
    service = service_for(database, maximum=1)
    actor = ToolActor.from_inbound(_inbound())
    barrier = asyncio.Barrier(2)
    original = service._repository.get_by_creation_key

    async def read_before_any_insert(*args, **kwargs):
        result = await original(*args, **kwargs)
        await barrier.wait()
        return result

    monkeypatch.setattr(service._repository, "get_by_creation_key", read_before_any_insert)
    actors = [actor, replace(actor, event_id=2)]
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                service.create(_script(), actor=current, conversation_key="private:10001")
                for current in actors
            ),
            return_exceptions=True,
        ),
        timeout=5,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    winners = [
        (current, result)
        for current, result in zip(actors, results, strict=True)
        if not isinstance(result, BaseException)
    ]
    assert len(winners) == 1
    monkeypatch.setattr(service._repository, "get_by_creation_key", original)
    replay = await service.create(_script(), actor=winners[0][0], conversation_key="private:10001")
    assert replay.id == winners[0][1].id
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AutomationModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AdminOperationEventModel)) == 1


@pytest.mark.parametrize("operation", ["create", "update", "pause", "resume", "cancel", "run_now"])
@pytest.mark.parametrize("failure_phase", ["before_cursor_execute", "after_cursor_execute"])
async def test_required_audit_failure_rolls_back_domain_and_version(
    database, operation, failure_phase
):
    service = service_for(database)
    actor = ToolActor.from_inbound(_inbound())
    arguments = dict(actor=actor, conversation_key="private:10001")
    row = None
    if operation != "create":
        row = await service.create(_script(), **arguments)
        if operation == "resume":
            await service.pause(row.id, **arguments)

    async def snapshot():
        async with database.sessions() as session:
            domain = tuple(
                (await session.execute(select(*AutomationModel.__table__.columns))).all()
            )
            versions = await session.scalar(
                select(func.count()).select_from(AutomationVersionModel)
            )
            audits = await session.scalar(
                select(func.count()).select_from(AdminOperationEventModel)
            )
            return domain, versions, audits

    before = await snapshot()

    def reject_audit(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO admin_operation_events"):
            raise RuntimeError("required_audit_unavailable")

    event.listen(database.engine.sync_engine, failure_phase, reject_audit)
    try:
        with pytest.raises(RuntimeError, match="required_audit_unavailable"):
            if operation == "create":
                await service.create(_script(), **arguments)
            elif operation == "update":
                script = _script().model_copy(update={"name": "changed"})
                await service.update(row.id, script, **arguments)
            else:
                await getattr(service, operation)(row.id, **arguments)
    finally:
        event.remove(database.engine.sync_engine, failure_phase, reject_audit)
    assert await snapshot() == before


async def test_concurrent_update_audit_uses_actual_previous_version(database, monkeypatch):
    service = service_for(database)
    actor = ToolActor.from_inbound(_inbound())
    arguments = dict(actor=actor, conversation_key="private:10001")
    first = await service.create(_script(), **arguments)
    observed, resume = asyncio.Event(), asyncio.Event()
    original = service._management_context

    async def pause_old_preparation(*args):
        result = await original(*args)
        if asyncio.current_task().get_name() == "stale-update":
            observed.set()
            await resume.wait()
        return result

    monkeypatch.setattr(service, "_management_context", pause_old_preparation)
    second_update = asyncio.create_task(
        service.update(first.id, _script().model_copy(update={"name": "third"}), **arguments),
        name="stale-update",
    )
    try:
        await asyncio.wait_for(observed.wait(), timeout=5)
        second = await service.update(
            first.id, _script().model_copy(update={"name": "second"}), **arguments
        )
        resume.set()
        third = await asyncio.wait_for(second_update, timeout=5)
    finally:
        resume.set()
        if not second_update.done():
            second_update.cancel()
        await asyncio.gather(second_update, return_exceptions=True)
    audits = [
        row
        for row in await service._audit.history(capability="automation")
        if row.operation == "update"
    ]
    assert len(audits) == 2
    assert audits[0].before == {"script_hash": second.script_hash}
    assert audits[0].after == {"script_hash": third.script_hash}
    assert audits[1].before == {"script_hash": first.script_hash}
    async with database.sessions() as session:
        assert await session.scalar(select(func.max(AutomationVersionModel.version))) == 3


async def test_resume_uses_current_schedule_after_concurrent_edit_and_pause(database, monkeypatch):
    service = service_for(database)
    actor = ToolActor.from_inbound(_inbound())
    arguments = dict(actor=actor, conversation_key="private:10001")
    row = await service.create(_script(), **arguments)
    await service.pause(row.id, **arguments)
    observed, proceed = asyncio.Event(), asyncio.Event()
    original = service.require_manageable

    async def pause_old_read(*args, **kwargs):
        result = await original(*args, **kwargs)
        if asyncio.current_task().get_name() == "stale-resume" and kwargs.get("session") is None:
            observed.set()
            await proceed.wait()
        return result

    monkeypatch.setattr(service, "require_manageable", pause_old_read)
    resuming = asyncio.create_task(service.resume(row.id, **arguments), name="stale-resume")
    try:
        await asyncio.wait_for(observed.wait(), timeout=5)
        raw = _script().model_dump(mode="json")
        raw["schedule"] = {"type": "after", "seconds": 100}
        edited = await service.update(row.id, raw, **arguments)
        await service.pause(row.id, **arguments)
        proceed.set()
        assert await asyncio.wait_for(resuming, timeout=5)
    finally:
        proceed.set()
        if not resuming.done():
            resuming.cancel()
        await asyncio.gather(resuming, return_exceptions=True)
    current = await service._repository.get(row.id)
    assert current.next_run_at == edited.next_run_at


@pytest.mark.parametrize("changed", ["generation", "presence"])
async def test_self_creation_rechecks_prepared_scene_before_write(database, monkeypatch, changed):
    source, _, _ = await self_source(database)
    actor = ToolActor(
        user_id="",
        bot_user_id=source["bot_user_id"],
        group_id=source["group_id"],
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="整理当前群",
        execution_id="work",
        conversation_id=source["conversation_id"],
        presence_id=source["presence_id"],
        principal_kind="self",
        initiative_run_id=source["initiative_run_id"],
    )
    raw = _script().model_dump(mode="json")
    raw["context"] = {"scene": "current_group"}
    raw["steps"] = [
        {
            "id": "work",
            "call": "yuki.agent",
            "arguments": {"instruction": "整理当前群", "context_profile": "current_group"},
        }
    ]
    raw["limits"].update(max_llm_calls=2, max_tool_calls=4, agent_budget_managed=True)
    service = service_for(database, runtime_work=True)
    original = service._self_scene_fields

    async def change_after_prepare(current_actor, **kwargs):
        prepared = await original(current_actor, **kwargs)
        if kwargs.get("session") is None:
            async with database.immediate_session() as session:
                if changed == "generation":
                    conversation = await session.get(
                        CanonicalConversationModel, source["conversation_id"]
                    )
                    conversation.generation += 1
                else:
                    presence = await session.get(PresenceModel, source["presence_id"])
                    presence.enabled = False
        return prepared

    monkeypatch.setattr(service, "_self_scene_fields", change_after_prepare)
    with pytest.raises(PermissionError, match=r"self_.*scene_changed"):
        await service.create(raw, actor=actor, conversation_key="group:2001")
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AutomationModel)) == 0
