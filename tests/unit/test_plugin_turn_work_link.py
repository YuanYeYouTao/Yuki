"""A plugin Job and its original Work are admitted, parked and closed together."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from tests.conftest import build_harness, make_settings
from tests.support.background_authority import approve_background_plugin
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.llm.base import LLMInvalidRequestError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker
from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel
from qq_ai_bot.plugin_host.notification_repository import TURN_ERROR_WORK_PARKED
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import scope, work
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

PLUGIN = "test.work-link"


class _Model:
    """First request fails before any effect; later requests end quietly."""

    def __init__(self) -> None:
        self.fail_next = True
        self.provider = FakeLLMProvider(self._respond)

    def _respond(self, _request: object) -> str:
        if self.fail_next:
            self.fail_next = False
            raise LLMInvalidRequestError("provider rejected the request")
        return "NO_REPLY"

    @property
    def calls(self) -> int:
        return len(self.provider.requests)


async def _setup(database, tmp_path, *, fail_first: bool = True):
    env = await social_env(database, tmp_path)
    repository = await approve_background_plugin(
        database,
        plugin_id=PLUGIN,
        bot_user_id="80001",
        group_id="20001",
        creator_user_id="10001",
    )
    await repository.publish(
        plugin_id=PLUGIN,
        request=PublishNotificationRequest(
            event_key="original-event",
            event_type="fixture.created",
            external_source="offline",
            target=NotificationTarget(target_type="group", target_id="20001"),
            occurred_at=datetime.now(UTC),
            summary="Original external update",
            ask_agent=True,
            agent_intent="Acknowledge the update once.",
        ),
    )
    model = _Model()
    model.fail_next = fail_first
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=True, enabled_groups_csv="20001"),
        model.provider,
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    worker = PluginBackgroundTurnWorker(
        repository=repository,
        ledger=chat._ledger,
        runtime_config=chat._runtime_config,
        chat=chat,
        turns=chat._turn_coordinator,
        conversation_scopes=chat._conversation_scopes,
        router=env.router,
    )
    return env, repository, worker, model


async def _state(database):
    async with database.sessions() as session:
        job = await session.scalar(select(PluginBackgroundTurnJobModel))
        rows = [dict(row._mapping) for row in await session.execute(select(work))]
    return job, rows


async def _parked(database, tmp_path):
    """Run the real worker once: the provider failure suspends the Work."""
    env, repository, worker, model = await _setup(database, tmp_path)
    job = await repository.claim_turn()
    assert job is not None and job.work_id is None
    await worker._execute(job)
    stored, rows = await _state(database)
    assert len(rows) == 1 and rows[0]["state"] == "suspended"
    assert stored.status == "pending" and stored.work_id == rows[0]["id"]
    assert stored.last_error_category == TURN_ERROR_WORK_PARKED
    return env, repository, worker, model, stored, rows[0]


async def _queue_work(database, identity: str, *, not_before: float = 0) -> None:
    # Equivalent to a management resume or a delivered signal on the original ID.
    async with database.immediate_session() as session:
        await session.execute(
            update(work)
            .where(work.c.id == identity)
            .values(state="queued", reason="operator_resume", revision=work.c.revision + 1)
        )
        await session.execute(
            update(recovery).where(recovery.c.work_id == identity).values(not_before=not_before)
        )


async def test_suspended_work_parks_the_job_without_claims_or_attempts(database, tmp_path):
    _env, repository, _worker, model, stored, _row = await _parked(database, tmp_path)
    assert stored.attempts == 1 and model.calls == 1
    budget = stored.max_attempts
    for _ in range(3):
        assert await repository.claim_turn() is None
    after, rows = await _state(database)
    assert (after.status, after.attempts, after.max_attempts) == ("pending", 1, budget)
    assert after.next_attempt_at.replace(tzinfo=UTC) < datetime.now(UTC) + timedelta(days=1)
    assert len(rows) == 1 and rows[0]["state"] == "suspended" and model.calls == 1


async def test_resume_makes_the_original_id_claimable_and_completes_it(database, tmp_path):
    _env, repository, worker, model, stored, row = await _parked(database, tmp_path)
    await _queue_work(database, row["id"])
    resumed = await repository.claim_turn()
    assert resumed is not None and resumed.id == stored.id and resumed.work_id == row["id"]
    await worker._execute(resumed)
    job, rows = await _state(database)
    assert [item["id"] for item in rows] == [row["id"]]
    assert rows[0]["state"] == "completed" and job.status == "completed"
    # The Job summary keeps the real calls of the original Work.
    assert job.model_requests == rows[0]["model_requests"] == model.calls == 2


async def test_queued_work_respects_recovery_backoff_and_live_scope_lease(database, tmp_path):
    env, repository, _worker, _model, _stored, row = await _parked(database, tmp_path)
    await _queue_work(database, row["id"], not_before=datetime.now(UTC).timestamp() + 600)
    assert await repository.claim_turn() is None
    await _queue_work(database, row["id"])
    lease = await WorkRepository(database).acquire(env.context.conversation_id, row["generation"])
    assert lease is not None
    assert await repository.claim_turn() is None
    # Running is recovered only after the original activation lease is lost.
    async with database.immediate_session() as session:
        await session.execute(update(work).where(work.c.id == row["id"]).values(state="running"))
    assert await repository.claim_turn() is None
    await WorkRepository(database).release(lease)
    assert (await repository.claim_turn()) is not None


async def test_expired_processing_is_reconciled_exactly_once(database, tmp_path):
    _env, repository, _worker, _model, stored, _row = await _parked(database, tmp_path)
    expired = datetime.now(UTC) - timedelta(seconds=5)
    async with database.immediate_session() as session:
        await session.execute(
            update(PluginBackgroundTurnJobModel)
            .where(PluginBackgroundTurnJobModel.id == stored.id)
            .values(status="processing", lease_until=expired, last_error_category=None)
        )
    assert await repository.claim_turn() is None
    once, _ = await _state(database)
    assert (once.status, once.attempts, once.lease_until) == ("pending", 1, None)
    assert once.last_error_category == TURN_ERROR_WORK_PARKED
    assert await repository.claim_turn() is None
    twice, _ = await _state(database)
    assert twice.updated_at == once.updated_at and twice.attempts == 1


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
async def test_terminal_work_closes_the_job_with_zero_model_requests(database, tmp_path, terminal):
    _env, repository, _worker, model, _stored, row = await _parked(database, tmp_path)
    async with database.immediate_session() as session:
        await session.execute(update(work).where(work.c.id == row["id"]).values(state=terminal))
    assert await repository.claim_turn() is None
    job, rows = await _state(database)
    assert job.status == terminal and job.lease_until is None and job.attempts == 1
    assert rows[0]["state"] == terminal and model.calls == 1


async def test_capability_change_resumes_the_same_identity(database, tmp_path, monkeypatch):
    from qq_ai_bot.services import durable_invocations

    _env, repository, worker, _model, _stored, row = await _parked(database, tmp_path)
    original = durable_invocations.invocation_boundary
    monkeypatch.setattr(
        durable_invocations,
        "invocation_boundary",
        lambda runtime: "changed-" + original(runtime)[8:],
    )
    await _queue_work(database, row["id"])
    resumed = await repository.claim_turn()
    assert resumed is not None
    await worker._execute(resumed)
    _job, rows = await _state(database)
    assert [(item["id"], item["state"]) for item in rows] == [(row["id"], "completed")]


async def test_bound_work_is_not_cancelled_by_coverage_or_later_human(database, tmp_path):
    env, repository, worker, _model, stored, row = await _parked(database, tmp_path)
    await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="later-human",
        sender_user_id="10001",
        direction="inbound",
        content="a later human message",
    )
    async with database.immediate_session() as session:
        conversation = await session.get(CanonicalConversationModel, env.context.conversation_id)
        conversation.covered_through_event_id = conversation.last_event_id
    await _queue_work(database, row["id"])
    resumed = await repository.claim_turn()
    assert resumed is not None and resumed.id == stored.id
    await worker._execute(resumed)
    job, rows = await _state(database)
    assert job.status == "completed" and rows[0]["state"] == "completed"


async def test_plugin_unapproval_closes_job_and_work_together(database, tmp_path):
    _env, repository, _worker, model, _stored, row = await _parked(database, tmp_path)
    await PluginInstallationRepository(database).set_enabled(PLUGIN, enabled=False)
    await _queue_work(database, row["id"])
    assert await repository.claim_turn() is None
    job, rows = await _state(database)
    assert (job.status, job.last_error_category) == ("cancelled", "plugin_authority_revoked")
    assert rows[0]["state"] == "cancelled" and model.calls == 1


async def test_generation_change_closes_job_and_work_together(database, tmp_path):
    env, repository, _worker, model, _stored, row = await _parked(database, tmp_path)
    await _queue_work(database, row["id"])
    async with database.immediate_session() as session:
        conversation = await session.get(CanonicalConversationModel, env.context.conversation_id)
        conversation.generation += 1
    assert await repository.claim_turn() is None
    job, rows = await _state(database)
    assert job.status == "cancelled" and rows[0]["state"] == "cancelled"
    assert model.calls == 1


async def test_admission_and_binding_commit_together(database, tmp_path, monkeypatch):
    _env, repository, worker, model = await _setup(database, tmp_path, fail_first=False)
    original = WorkRepository.accept_in_session

    async def fail_after_admission(self, session, lease, **kwargs):
        await original(self, session, lease, **kwargs)
        raise RuntimeError("injected failure after admission, before binding commit")

    monkeypatch.setattr(WorkRepository, "accept_in_session", fail_after_admission)
    job = await repository.claim_turn()
    assert job is not None
    await worker._execute(job)
    stored, rows = await _state(database)
    # No half binding and no orphan Work: the whole writer rolled back.
    assert rows == [] and stored.work_id is None
    monkeypatch.setattr(WorkRepository, "accept_in_session", original)
    async with database.immediate_session() as session:
        await session.execute(
            update(PluginBackgroundTurnJobModel).values(next_attempt_at=datetime.now(UTC))
        )
    retried = await repository.claim_turn()
    assert retried is not None
    await worker._execute(retried)
    stored, rows = await _state(database)
    assert len(rows) == 1 and stored.work_id == rows[0]["id"]
    assert stored.status == "completed" and model.calls == 1


async def test_binding_never_replaces_an_existing_link(database, tmp_path):
    from qq_ai_bot.plugin_host.notification_repository import admit_background_turn_work
    from qq_ai_bot.runtime.work_repository import WorkConflict

    env, repository, _worker, _model, stored, row = await _parked(database, tmp_path)
    await _queue_work(database, row["id"])
    job = await repository.claim_turn()
    assert job is not None
    lease = await WorkRepository(database).acquire(env.context.conversation_id, job.generation)
    assert lease is not None
    source = {
        **json.loads(row["source_json"]),
        "execution_boundary": "a-different-capability-set",
    }
    with pytest.raises(WorkConflict, match="plugin_turn_work_link_conflict"):
        await admit_background_turn_work(
            database,
            lease,
            job_id=job.id,
            attempt=job.attempts,
            generation=job.generation,
            source_key="invocation:a-different-capability-set",
            source=source,
            goal="second identity",
        )
    after, rows = await _state(database)
    assert after.work_id == stored.work_id and [item["id"] for item in rows] == [row["id"]]


async def test_privacy_purge_retires_bound_jobs_before_work(database, tmp_path):
    env, _repository, _worker, _model, stored, _row = await _parked(database, tmp_path)
    async with database.immediate_session() as session:
        await WorkRepository.purge_scope(session, env.context.conversation_id)
    job, rows = await _state(database)
    assert rows == []
    # Not revived as a new Job by the null link.
    assert (job.id, job.status, job.work_id) == (stored.id, "cancelled", None)
    assert job.last_error_category == "privacy_purged"
    async with database.sessions() as session:
        assert (await session.execute(select(scope))).first() is None


async def test_terminal_reclaim_keeps_referenced_work_rows(database, tmp_path):
    _env, _repository, _worker, _model, stored, row = await _parked(database, tmp_path)
    async with database.immediate_session() as session:
        await session.execute(
            update(work)
            .where(work.c.id == row["id"])
            .values(
                state="cancelled",
                updated=0,
                source_json=json.dumps({"owner": "plugin_background"}),
            )
        )
        for number in range(130):
            await session.execute(
                work.insert().values(
                    id=f"00000000-0000-4000-8000-{number:012d}",
                    conversation_id=row["conversation_id"],
                    generation=row["generation"],
                    source_key=f"filler-{number}",
                    source_json="{}",
                    goal="filler",
                    state="completed",
                    created=1,
                    updated=10 + number,
                )
            )
    await WorkRepository(database).reclaim_terminal()
    job, rows = await _state(database)
    assert job.work_id == stored.work_id
    assert row["id"] in {item["id"] for item in rows}


async def test_management_resume_queues_the_original_bound_work(database, tmp_path):
    from qq_ai_bot.runtime.work_management import manage_work

    _env, repository, worker, _model, stored, row = await _parked(database, tmp_path)
    current = await WorkRepository(database).get(row["id"])
    async with database.immediate_session() as session:
        await manage_work(session, row["id"], current["revision"], "resume")
    assert (await WorkRepository(database).get(row["id"]))["state"] == "queued"
    # The bound Job, not the root scheduler, claims the same Work ID.
    resumed = await repository.claim_turn()
    assert resumed is not None and resumed.id == stored.id and resumed.work_id == row["id"]
    await worker._execute(resumed)
    _job, rows = await _state(database)
    assert [item["id"] for item in rows] == [row["id"]] and rows[0]["state"] == "completed"
