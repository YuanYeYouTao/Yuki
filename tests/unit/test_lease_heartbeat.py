"""A failed renewal stops the original activation without replaying its effects."""

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.runtime.activation_outcome import WorkActivationHandled
from qq_ai_bot.runtime.lease_heartbeat import lease_heartbeat, supervise_lease
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import WorkJournal
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, journal
from qq_ai_bot.services.turn_transcript import TurnTranscript


def sqlite_error(code):
    original = sqlite3.OperationalError("lock fixture")
    original.sqlite_errorcode = code
    return OperationalError("UPDATE", {}, original)


@pytest.mark.asyncio
async def test_busy_renewal_recovers_before_confirmed_expiry():
    renewed = asyncio.Event()
    renew = AsyncMock(side_effect=[sqlite_error(sqlite3.SQLITE_BUSY_SNAPSHOT), True])
    pulse = asyncio.create_task(
        lease_heartbeat(
            renew,
            AsyncMock(return_value=time.time() + 2),
            seconds=2,
            interval=0.001,
            meter=AsyncMock(side_effect=renewed.set),
        )
    )
    try:
        await asyncio.wait_for(renewed.wait(), 1)
        assert renew.await_count == 2
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["busy_expiry", "locked", "obsolete", "meter"])
async def test_pulse_failure_interrupts_parent_with_visible_failure(mode, caplog):
    failure = {
        "busy_expiry": sqlite_error(sqlite3.SQLITE_BUSY),
        "locked": sqlite_error(sqlite3.SQLITE_LOCKED_SHAREDCACHE),
        "obsolete": None,
        "meter": None,
    }[mode]
    renew = AsyncMock(side_effect=failure, return_value=mode == "meter")
    meter = AsyncMock(side_effect=RuntimeError("meter fixture")) if mode == "meter" else None
    expected = {
        "busy_expiry": "expired",
        "locked": "renew_failed",
        "obsolete": "obsolete",
        "meter": "meter_failed",
    }[mode]
    continued = False
    with pytest.raises(WorkConflict, match=expected):
        async with supervise_lease(
            renew,
            AsyncMock(return_value=time.time() + 0.03),
            seconds=0.03,
            interval=0.001,
            meter=meter,
        ):
            await asyncio.Event().wait()
            continued = True
    assert not continued and "lease_heartbeat_failed" in caplog.text
    if mode == "locked":
        assert renew.await_count == 1
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
async def test_external_cancellation_remains_cancellation():
    ready = asyncio.Event()

    async def activation():
        async with supervise_lease(
            AsyncMock(return_value=True), AsyncMock(return_value=time.time() + 60)
        ):
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(activation())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["renew", "meter"])
async def test_original_work_journal_budget_and_unknown_effect_survive_heartbeat_failure(
    database, tmp_path, monkeypatch, failure
):
    import qq_ai_bot.runtime.work_activation as activation_module

    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    item = await repository.accept(
        lease, source_key="heartbeat-owner", source={}, goal="keep owner"
    )
    checkpoint = {"execution_evidence": [{"run_id": "original-run", "uncertain": True}]}
    await repository.checkpoint(lease, item["id"], checkpoint, models=2, tools=1)
    await repository.prepare_effect(lease, item["id"], "unknown-call", "terminal")
    await repository.record_effect("unknown-call", "unknown", {"run_id": "original-run"})
    await WorkJournal(repository).save(
        lease,
        item["id"],
        "original-contract",
        TurnTranscript((ChatMessage("user", "original goal"),)),
        phase="dispatched",
        pending=[{"id": "original-call", "name": "run_code", "arguments": "{}"}],
        source_revision=0,
        metadata={"sequence": 1, "effects": checkpoint["execution_evidence"]},
    )
    async with database.sessions() as session:
        before = (await session.execute(select(journal))).mappings().one()
        original_payload = before["payload_json"]
    await repository.release(lease)
    ready = asyncio.Event()

    def fast_supervision(renew, expiry, **kwargs):
        async def wait_then_renew():
            await ready.wait()
            if failure == "renew":
                return False
            return await renew()

        kwargs["interval"] = 0.001
        return supervise_lease(wait_then_renew, expiry, **kwargs)

    monkeypatch.setattr(activation_module, "supervise_lease", fast_supervision)
    if failure == "meter":

        async def broken_meter(self):
            raise RuntimeError("meter fixture")

        monkeypatch.setattr(WorkControl, "meter_active_time", broken_meter)

    async def validate():
        pass

    with pytest.raises(WorkActivationHandled):
        async with activate_work(
            repository,
            env.context.conversation_id,
            1,
            "heartbeat-owner",
            {},
            validate,
            work_id=item["id"],
        ):
            ready.set()
            await asyncio.Event().wait()
    saved = await repository.get(item["id"])
    assert saved["state"] == "suspended"
    assert saved["model_requests"] == 2 and saved["tool_calls"] == 1
    assert json.loads(saved["checkpoint_json"]) == checkpoint
    async with database.sessions() as session:
        assert await session.scalar(select(journal.c.payload_json)) == original_payload
        assert await session.scalar(select(effects.c.state)) == "unknown"


@pytest.mark.asyncio
async def test_automation_renewal_failure_stops_original_process_and_releases_claim(monkeypatch):
    import qq_ai_bot.automation.worker as worker_module

    repository = SimpleNamespace(
        _database=object(),
        renew_claim=AsyncMock(side_effect=sqlite_error(sqlite3.SQLITE_LOCKED)),
        claim_expiry=AsyncMock(return_value=time.time() + 60),
        release_claim=AsyncMock(),
    )
    worker = AutomationWorker(
        settings=SimpleNamespace(automation_lease_seconds=60),
        repository=repository,
        executor=None,
        time_service=SimpleNamespace(clock=SimpleNamespace(now=lambda: datetime.now(UTC))),
    )
    cancelled = asyncio.Event()
    calls = []

    async def process(original):
        calls.append(original.id)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    def fast_supervision(*args, **kwargs):
        kwargs["interval"] = 0.001
        return supervise_lease(*args, **kwargs)

    monkeypatch.setattr(worker_module, "supervise_lease", fast_supervision)
    monkeypatch.setattr(worker, "_process", process)
    original = SimpleNamespace(id=17, claimed_by="original-owner", next_run_at=datetime.now(UTC))
    await asyncio.wait_for(worker._process_guarded(original), 1)
    assert cancelled.is_set() and calls == [17]
    repository.release_claim.assert_awaited_once_with(
        17, worker_id="original-owner", next_run_at=original.next_run_at
    )


@pytest.mark.asyncio
async def test_automation_claim_expiring_while_writer_is_busy_cannot_be_revived(database):
    from datetime import timedelta

    from sqlalchemy import update
    from tests.conftest import make_settings
    from tests.unit.test_automation_runtime import FakeClock, _inbound, _script

    from qq_ai_bot.automation.registry import build_capability_registry
    from qq_ai_bot.automation.repository import AutomationRepository
    from qq_ai_bot.automation.service import AutomationService
    from qq_ai_bot.domain.tool_actor import ToolActor
    from qq_ai_bot.persistence.models import AutomationModel
    from qq_ai_bot.time.service import TimeContextService

    clock = FakeClock(datetime.now(UTC))
    repository = AutomationRepository(database)
    service = AutomationService(
        settings=make_settings(database.url, automation_enabled=True),
        repository=repository,
        registry=build_capability_registry(),
        time_service=TimeContextService(database, clock=clock),
    )
    original = await service.create(
        _script(), actor=ToolActor.from_inbound(_inbound()), conversation_key="private:10001"
    )
    clock.advance(2)
    claimed = await repository.claim_due(
        worker_id="original-owner", now=clock.now(), lease_seconds=60
    )
    assert len(claimed) == 1 and claimed[0].id == original.id
    expiry = datetime.now(UTC) + timedelta(seconds=0.05)
    async with database.immediate_session() as session:
        await session.execute(
            update(AutomationModel)
            .where(AutomationModel.id == original.id)
            .values(claimed_until=expiry)
        )
    assert await repository.claim_expiry(original.id, "original-owner") == pytest.approx(
        expiry.timestamp()
    )
    async with database.immediate_session():
        pending = asyncio.create_task(
            repository.renew_claim(
                original.id, "original-owner", datetime.now(UTC) + timedelta(seconds=60)
            )
        )
        await asyncio.sleep(max(0.01, expiry.timestamp() - time.time() + 0.05))
        assert not pending.done()
    assert not await pending
    assert await repository.claim_expiry(original.id, "original-owner") == pytest.approx(
        expiry.timestamp()
    )


@pytest.mark.asyncio
async def test_natural_exit_same_turn_as_pulse_failure_cannot_cancel_later_work():
    body_done = asyncio.Event()

    async def renew():
        body_done.set()
        raise RuntimeError("same turn failure")

    with pytest.raises(RuntimeError, match="same turn failure"):
        async with supervise_lease(
            renew, AsyncMock(return_value=time.time() + 2), seconds=2, interval=0.001
        ):
            await body_done.wait()
    await asyncio.sleep(0)
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
async def test_simultaneous_external_and_heartbeat_cancellation_keeps_external_request():
    owner = None

    async def renew():
        # This external cancellation and the heartbeat's callback both target
        # the parent. Removing our one request must retain the external one.
        owner.cancel("external-stop")
        raise RuntimeError("renew fixture")

    async def activation():
        nonlocal owner
        owner = asyncio.current_task()
        async with supervise_lease(
            renew, AsyncMock(return_value=time.time() + 2), seconds=2, interval=0.001
        ):
            await asyncio.Event().wait()

    task = asyncio.create_task(activation())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelling() == 1
