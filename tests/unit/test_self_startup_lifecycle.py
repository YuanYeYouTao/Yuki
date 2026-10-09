"""T07/T09: SELF startup failure exit, retained SELF owner and held wait signals."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select, update
from tests.support.runtime_execution import make_work_resumer
from tests.support.social_identity_cases import social_env
from tests.unit.test_self_initiative_runtime import self_source

from qq_ai_bot.gateway.registry import RegistryClosed
from qq_ai_bot.identity.routing import RouteSendError
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.activation_outcome import ExitReason
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_management import (
    WorkManagementError,
    manage_work,
    management_view,
)
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_repository import TERMINAL, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs, journal, work
from qq_ai_bot.runtime.work_supervisor import recover_failure
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.services.participation_feedback import reconcile_page


def disconnected() -> RouteSendError:
    try:
        raise RouteSendError("original_presence_unavailable") from RegistryClosed("disconnected")
    except RouteSendError as exc:
        return exc


async def queued_self_work(database):
    source, admissions, _ = await self_source(database)
    repo = WorkRepository(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    item = await repo.accept(
        lease,
        initial_state="queued",
        source_key=f"initiative:{source['initiative_run_id']}",
        source=source,
        goal="inspect",
        output_kind="answer",
        deliver_artifacts=False,
    )
    await repo.release(lease)
    return source, admissions, repo, item


def resumer_for(repo, source, *, presence, generate=None):
    @asynccontextmanager
    async def background(_key):
        yield SimpleNamespace(version=1)

    return make_work_resumer(
        repo,
        ledger=EventLedgerRepository(repo.database),
        scopes=SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    id=source["conversation_id"],
                    generation=1,
                    runtime_scope_key="bot:8000:group:2001",
                )
            )
        ),
        turns=SimpleNamespace(background_turn=background),
        router=SimpleNamespace(resolve_presence=presence),
        config=SimpleNamespace(snapshot=AsyncMock(return_value=SimpleNamespace())),
        generate_self=generate or AsyncMock(side_effect=AssertionError("no execution")),
        generate_wakeup=AsyncMock(side_effect=AssertionError("no message actor")),
        validate_snapshot=AsyncMock(return_value=True),
        run_effect=AsyncMock(side_effect=AssertionError("no automatic delivery")),
    )


def feedback_service(database, admissions, repo):
    return SimpleNamespace(
        database=database,
        repository=admissions,
        work=repo,
        _sessions={},
        _session_lock=asyncio.Lock(),
        _dispatch=AsyncMock(),
        _save=AsyncMock(),
    )


async def resume_until_settled(repo, resumer, identity, attempts=4):
    for _ in range(attempts):
        current = await repo.get(identity)
        if current["state"] != "queued":
            break
        await resumer.resume(current)
    return await repo.get(identity)


async def active_count(database) -> int:
    async with database.sessions() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(work).where(work.c.state.not_in(TERMINAL))
            )
        )


async def test_definite_not_sent_startup_failures_fail_original_self_work(database):
    source, admissions, repo, item = await queued_self_work(database)
    before = await active_count(database)
    presence = AsyncMock(side_effect=disconnected())
    resumer = resumer_for(repo, source, presence=presence)
    final = await resume_until_settled(repo, resumer, item["id"])
    assert presence.await_count == 4
    # Same original ID, terminal: admission capacity released, fact retained.
    assert final["id"] == item["id"] and final["state"] == "failed"
    assert await active_count(database) == before - 1
    async with database.sessions() as session:
        saved = (
            (await session.execute(select(recovery).where(recovery.c.work_id == item["id"])))
            .mappings()
            .one()
        )
        with pytest.raises(WorkManagementError, match="precondition_failed"):
            await manage_work(session, item["id"], final["revision"], "resume")
    failure = json.loads(saved["failure_json"])
    assert saved["attempts"] == 4 and failure["code"] == "gateway_disconnected"
    assert failure["certainty"] == "not_sent" and failure["diagnostics"]["startup_failed"]
    # The real terminal Work, not the earlier retries, ends the initiative.
    await reconcile_page(
        feedback_service(database, admissions, repo),
        (await admissions.get_run(source["initiative_run_id"]),),
    )
    assert (await admissions.get_run(source["initiative_run_id"])).state == "interrupted"


async def test_reserved_request_without_journal_keeps_original_recovery(database):
    source, _, repo, item = await queued_self_work(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    # Request admitted and counted, crash before the dispatched journal save.
    await repo.checkpoint(lease, item["id"], None, models=1)
    await repo.release(lease)
    resumer = resumer_for(repo, source, presence=AsyncMock(side_effect=disconnected()))
    final = await resume_until_settled(repo, resumer, item["id"])
    assert final["state"] == "suspended" and final["model_requests"] == 1


async def test_startup_policy_ignores_activation_and_non_self_sources(database):
    source, _, repo, item = await queued_self_work(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    control = WorkControl(repo, lease, item["source_key"], dict(source), AsyncMock())
    # Not the first scene boundary (e.g. a later activation failure).
    for _ in range(4):
        control.current = await repo.get(item["id"])
        outcome = await recover_failure(control, disconnected())
    assert outcome.reason is ExitReason.PAUSED
    assert (await repo.get(item["id"]))["state"] == "suspended"
    await repo.release(lease)


async def test_self_automation_not_sent_keeps_retained_pause(database):
    source, _, repo, item = await queued_self_work(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    automation = {"origin": "scheduled_automation", "principal_kind": "self"}
    control = WorkControl(repo, lease, item["source_key"], automation, AsyncMock())
    control.startup_boundary = True
    for _ in range(4):
        control.current = await repo.get(item["id"])
        await recover_failure(control, disconnected())
    assert (await repo.get(item["id"]))["state"] == "suspended"
    await repo.release(lease)


@pytest.mark.parametrize("paused", ["suspended", "waiting_user"])
async def test_executed_self_pause_is_resumed_by_original_owner(database, paused):
    source, admissions, repo, item = await queued_self_work(database)
    now = time.time()
    async with database.immediate_session() as session:
        # Executed: one dispatched request with its retained journal.
        await session.execute(
            update(work)
            .where(work.c.id == item["id"])
            .values(state=paused, reason="paused_for_test", model_requests=1)
        )
        await session.execute(
            insert(journal).values(
                work_id=item["id"],
                chain_id="chain",
                contract="main",
                source_revision=1,
                phase="response",
                payload_json="{}",
                updated=now,
            )
        )
    service = feedback_service(database, admissions, repo)
    run_id = source["initiative_run_id"]
    await reconcile_page(service, (await admissions.get_run(run_id),))
    assert (await admissions.get_run(run_id)).state in {"accepted", "running"}
    current = await repo.get(item["id"])
    async with database.immediate_session() as session:
        managed = await management_view(session, item["id"])
        assert managed["pause_reason"] == "paused_for_test"
        assert managed["actions"] == {"resume": True, "resume_blocked_by": None, "cancel": True}
        assert await manage_work(session, item["id"], current["revision"], "resume") == (
            current["revision"] + 1,
            "queued",
        )

    async def generate(**kwargs):
        assert kwargs["trigger"].run_id == run_id
        control = current_work_control.get()
        assert control.current["id"] == item["id"]
        await control.execute("task_control", {"action": "complete"}, "finish")
        return SimpleNamespace(text="NO_REPLY", outcome=None)

    resumer = resumer_for(
        repo,
        source,
        presence=AsyncMock(
            return_value=SimpleNamespace(connection=SimpleNamespace(bot=None, snapshot="c"))
        ),
        generate=AsyncMock(side_effect=generate),
    )
    await resumer.resume(await repo.get(item["id"]))
    resumer.services.generate_self.assert_awaited_once()
    assert (await repo.get(item["id"]))["state"] == "completed"
    await reconcile_page(service, (await admissions.get_run(run_id),))
    assert (await admissions.get_run(run_id)).state == "no_reply"


@pytest.mark.parametrize("outcome", ["interrupted", "failed"])
async def test_terminal_initiative_is_not_revived_by_resume(database, outcome):
    source, admissions, repo, item = await queued_self_work(database)
    async with database.immediate_session() as session:
        await session.execute(update(work).where(work.c.id == item["id"]).values(state="suspended"))
    await admissions.record_feedback(source["initiative_run_id"], sequence=1, outcome=outcome)
    current = await repo.get(item["id"])
    async with database.immediate_session() as session:
        managed = await management_view(session, item["id"])
        assert managed["actions"]["resume"] is False
        assert managed["actions"]["resume_blocked_by"] == "precondition_failed"
        assert managed["actions"]["cancel"] is True
        with pytest.raises(WorkManagementError, match="precondition_failed"):
            await manage_work(session, item["id"], current["revision"], "resume")
    assert (await admissions.get_run(source["initiative_run_id"])).state == outcome


async def test_signal_on_paused_work_is_held_once_and_consumed_by_explicit_resume(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    waiting = WorkWaitRepository(repo)
    lease = await repo.acquire(env.context.conversation_id, 1)
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_user_id": "10001",
        "actor_person_id": env.person,
    }
    item = await repo.accept(lease, source_key="held", source=source, goal="继续原任务")
    binding = await waiting.register(
        lease,
        work_id=item["id"],
        source=source,
        call_key="held-wait",
        mode="any",
        conditions=[{"kind": "time_due", "at": "2000-01-01T00:00:00+00:00"}],
        deadline_at=None,
    )
    paused = await repo.transition(
        lease, item["id"], item["revision"], "suspended", reason="work_checkpoint_capacity"
    )
    await repo.release(lease)
    assert await waiting.deliver_due() == 1
    assert await waiting.deliver_due() == 0
    held = await repo.get(item["id"])
    # An independent pause is not unlocked by the time signal.
    assert held["state"] == "suspended" and held["reason"] == "work_checkpoint_capacity"
    assert held["revision"] == paused["revision"]
    async with database.sessions() as session:
        signals = (
            (
                await session.execute(
                    select(inputs).where(
                        inputs.c.work_id == item["id"],
                        inputs.c.source_key == f"wait:{binding['id']}",
                    )
                )
            )
            .mappings()
            .all()
        )
        events = await session.scalar(select(func.count()).select_from(ChatEventModel))
    assert len(signals) == 1 and signals[0]["state"] == "pending"
    described = await waiting.describe(item["id"])
    assert described["status"] == "delivered"
    assert described["work"]["pause_reason"] == "work_checkpoint_capacity"
    assert described["work"]["signal"]["input_state"] == "pending"
    assert described["work"]["actions"]["resume"] and described["work"]["actions"]["cancel"]

    async with database.immediate_session() as session:
        assert (await manage_work(session, item["id"], held["revision"], "resume"))[1] == "queued"
    lease = await repo.acquire(env.context.conversation_id, 1)
    control = WorkControl(repo, lease, "held", source, AsyncMock())
    control.current = await repo.get(item["id"])
    taken = await control.take_inputs("attempt")
    await control.confirm_inputs()
    await repo.release(lease)
    assert len(taken) == 1 and binding["id"] in taken[0].content
    async with database.sessions() as session:
        rows = (
            await session.execute(
                select(inputs.c.source_key, inputs.c.state).where(inputs.c.work_id == item["id"])
            )
        ).all()
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == events
    assert rows == [(f"wait:{binding['id']}", "consumed")]
    after = await waiting.describe(item["id"])
    assert after["work"]["signal"]["input_state"] == "consumed"
