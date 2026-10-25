"""Real durable Host feedback; no API calls or QQ sends are performed."""

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from tests.support.autonomy_repository_helpers import _accept, _enable, _scene
from yuki_participation.controller import Controller
from yuki_participation.models import CandidateKind, Proposal, Scope, SourceRef, Support

from yuki.conversation.autonomy_binding import AutonomyOwner
from yuki.conversation.autonomy_db_models import InitiativeFeedbackModel, InitiativeRunModel
from yuki.conversation.autonomy_repository import AutonomyRepository
from yuki.conversation.self_initiative import validate_self_initiative
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_schema import budgets, children
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import journal, work
from yuki.services import participation_feedback
from yuki.services.participation_feedback import reconcile_page, sync_scope_effects
from yuki.services.semantic_participation import SemanticParticipationService
from yuki.social.db_models import SocialOperationModel


async def setup(database, *, owner=AutonomyOwner.SEMANTIC):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    if owner is AutonomyOwner.LEGACY:
        binding = await repository.transition(binding, master_enabled=True, external_enabled=False)
    run = (await _accept(repository, scene, binding)).run
    now = time.time()
    scope = Scope(conversation_id=scene.conversation, generation=1)
    controller = Controller(scope, now)
    ref = SourceRef(event_id="event:1", revision=1)
    support = Support(
        kind="observed",
        scope=scope,
        thread="topic",
        target="group",
        observation_id="synthetic",
        covered=(ref,),
        basis=ref,
        issued_at=now,
        valid_until=now + 90,
    )
    controller.state.proposals[run.proposal_id] = Proposal(
        proposal_id=run.proposal_id,
        scope=scope,
        controller_epoch=1,
        kind=CandidateKind.CONVERSATION,
        thread="topic",
        target_hint="group",
        sources=(ref,),
        support=support,
        created_at=now,
        expires_at=now + 90,
    )
    item = SimpleNamespace(
        controller=controller,
        social_effects=None,
        scene=SimpleNamespace(
            conversation_id=scene.conversation,
            generation=1,
            space_id=scene.space,
        ),
    )
    service = SimpleNamespace(
        database=database,
        repository=repository,
        work=WorkRepository(database),
        _sessions={(scene.conversation, 1): item},
        _session_lock=asyncio.Lock(),
        _dispatch=AsyncMock(),
        _save=AsyncMock(),
    )
    lease = await service.work.acquire(scene.conversation, 1)
    task = await service.work.accept(
        lease,
        source_key=f"initiative:{run.run_id}",
        source={
            "origin": "self_initiative",
            "principal_kind": "self",
            "initiative_run_id": run.run_id,
            "conversation_id": run.conversation_id,
            "generation": run.generation,
            "space_id": run.space_id,
            "presence_id": run.presence_id,
        },
        goal="synthetic feedback check",
    )
    await service.work.release(lease)
    return service, item, run, task


async def set_work(database, task, **values):
    async with database.immediate_session() as session:
        await session.execute(update(work).where(work.c.id == task["id"]).values(**values))


async def social(database, run, call, *, turn=None, action="send_message", seconds=0):
    at = datetime.now(UTC) + timedelta(seconds=seconds)
    row = SocialOperationModel(
        id=str(uuid4()),
        source_turn_id=turn or f"{run.conversation_id}:initiative:{run.run_id}",
        tool_call_id=call,
        source_conversation_id=run.conversation_id,
        action=action,
        payload_hash="0" * 64,
        target_kind="space",
        target_id=run.space_id,
        presence_id=run.presence_id,
        status="succeeded",
        created_at=at,
        updated_at=at,
    )
    async with database.immediate_session() as session:
        session.add(row)
    return row


async def test_feedback_preparation_closes_before_writer_and_preserves_original_facts(
    database, monkeypatch
):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=3)
    receipt = await social(database, run, "original-send")
    original_task = await service.work.get(task["id"])
    service._failures = 0
    original_writer = database.immediate_session
    injected = False

    @asynccontextmanager
    async def intervening_writer():
        nonlocal injected
        if not injected:
            injected = True
            async with original_writer() as writer:
                await writer.execute(
                    update(work).where(work.c.id == task["id"]).values(updated=work.c.updated)
                )
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", intervening_writer)
    await SemanticParticipationService._reconcile_outbox(service)
    assert injected and service._failures == 0
    settled = await service.repository.get_run(run.run_id)
    assert settled.state == "completed" and settled.feedback_sequence == 1
    assert await service.work.get(task["id"]) == original_task
    assert item.controller.state.feedback[run.run_id].outcome == "completed"
    assert len(item.controller.state.effects) == 4
    await SemanticParticipationService._reconcile_outbox(service)
    assert len(item.controller.state.effects) == 4
    service._dispatch.assert_not_awaited()
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(InitiativeFeedbackModel)) == 1
        assert (await reader.get(SocialOperationModel, receipt.id)).status == "succeeded"


async def test_feedback_unknown_commit_ack_keeps_receipts_without_rebuilding_short_term_state(
    database, monkeypatch
):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=3)
    await social(database, run, "original-send")
    original_task = await service.work.get(task["id"])
    service._failures = 0
    original = service.repository.record_feedback
    calls = []
    native = sqlite3.OperationalError("commit acknowledgement lost")
    native.sqlite_errorcode = 517
    uncertainty = OperationalError("commit acknowledgement", {}, native)

    async def unknown_ack(identity, **kwargs):
        calls.append((identity, kwargs["sequence"]))
        session = kwargs["session"]
        commit = session.commit

        async def commit_then_lose_ack():
            await commit()
            raise uncertainty

        monkeypatch.setattr(session, "commit", commit_then_lose_ack)
        return await original(identity, **kwargs)

    monkeypatch.setattr(service.repository, "record_feedback", unknown_ack)
    await SemanticParticipationService._reconcile_outbox(service)
    assert service._failures == 1 and calls == [(run.run_id, 1)]
    assert run.run_id not in item.controller.state.feedback
    service._save.assert_not_awaited()
    settled = await service.repository.get_run(run.run_id)
    assert settled.state == "completed" and settled.feedback_sequence == 1
    await SemanticParticipationService._reconcile_outbox(service)
    await SemanticParticipationService._reconcile_outbox(service)
    assert calls == [(run.run_id, 1)] and service._failures == 1
    assert run.run_id not in item.controller.state.feedback
    assert not item.controller.state.effects
    assert await service.work.get(task["id"]) == original_task
    service._dispatch.assert_not_awaited()
    async with database.read_session() as session:
        assert await session.scalar(select(func.count()).select_from(InitiativeFeedbackModel)) == 1


@pytest.mark.asyncio
async def test_snapshot_loss_keeps_120_charges_but_only_new_feedback_updates_participation(
    database,
):
    service, item, run, task = await setup(database)
    original = item.controller.state.model_copy(deep=True)
    await set_work(database, task, state="running", model_requests=120)
    service._save.side_effect = RuntimeError("synthetic snapshot commit failure")
    with pytest.raises(RuntimeError, match="synthetic snapshot commit failure"):
        await reconcile_page(service, (run,))
    assert len(item.controller.state.effects) == 120
    async with database.read_session() as session:
        rows = list(await session.scalars(select(InitiativeFeedbackModel)))
        assert len(rows) == 2
        assert sorted(len(json.loads(row.payload_json)["effects"]) for row in rows) == [56, 64]

    # Losing the short-lived projection does not lose or replay paid requests.
    service._save.side_effect = None
    service._save.reset_mock()
    item.controller = Controller.restore(original, time.time())
    await reconcile_page(service, (run,))
    assert not item.controller.state.effects
    service._save.assert_not_awaited()
    assert (await service.work.get(task["id"]))["model_requests"] == 120

    await set_work(database, task, model_requests=121)
    await reconcile_page(service, (run,))
    assert set(item.controller.state.effects) == {f"work-model:{task['id']}:121"}
    async with database.read_session() as session:
        assert await session.scalar(select(func.count()).select_from(InitiativeFeedbackModel)) == 3
    assert (await service.work.get(task["id"]))["model_requests"] == 121
    service._dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [AutonomyOwner.SEMANTIC, AutonomyOwner.LEGACY])
async def test_terminal_history_is_not_scanned_but_recent_late_send_keeps_original_owner(
    database, monkeypatch, owner
):
    service, item, run, task = await setup(database, owner=owner)
    service._failures = 0
    await set_work(database, task, state="completed", model_requests=120)
    await social(database, run, "old-confirmed-send", seconds=-3600)
    item.controller.advance(
        time.time(), controller_epoch=item.controller.state.epoch, host_available=False
    )
    await reconcile_page(service, (run,))
    original_work = await service.work.get(task["id"])
    service._save.reset_mock()
    read_facts = AsyncMock(side_effect=AssertionError("terminal history must not be loaded"))
    monkeypatch.setattr(participation_feedback, "_read_facts", read_facts)
    for _ in range(2):
        await SemanticParticipationService._reconcile_outbox(service)
    read_facts.assert_not_awaited()
    service._save.assert_not_awaited()

    original_effects = set(item.controller.state.effects)
    await social(database, run, "late-confirmed-send")
    await sync_scope_effects(service, item)
    messages = [
        record
        for identity, record in item.controller.state.effects.items()
        if identity not in original_effects and record.effect.kind == "message"
    ]
    assert len(messages) == 1 and messages[0].run_ref == run.run_id
    assert (await service.repository.get_run(run.run_id)).state == "completed"
    assert await service.work.get(task["id"]) == original_work
    service._dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["suspended", "waiting_user"])
async def test_retained_paused_work_keeps_initiative_until_real_terminal(database, state):
    service, item, run, task = await setup(database)
    await set_work(database, task, state=state)
    await reconcile_page(service, (run,))
    # A retained pause is not an end: the run keeps its legal execution.
    assert (await service.repository.get_run(run.run_id)).state == "running"
    await social(database, run, "late-send")
    await reconcile_page(service, (run,))
    assert (await service.repository.get_run(run.run_id)).state == "running"
    assert len(item.controller.state.effects) == 1
    await set_work(database, task, state="failed")
    await reconcile_page(service, (run,))
    assert (await service.repository.get_run(run.run_id)).state == "interrupted"
    assert item.controller.state.feedback[run.run_id].outcome == "interrupted"
    await set_work(database, task, state=state)
    await reconcile_page(service, (run,))
    # A truly terminal initiative is never revived by later Work rows.
    assert (await service.repository.get_run(run.run_id)).state == "interrupted"
    service._dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["suspended", "waiting_user", "waiting_external"])
async def test_retained_wait_releases_new_opportunity_without_revoking_original_self(
    database, state
):
    service, _item, run, task = await setup(database)
    await set_work(database, task, state=state)
    await reconcile_page(service, (run,))
    binding = await service.repository.get_binding(run.conversation_id, run.generation)
    result = await service.repository.accept_host_proposal(
        proposal_id="next-real-opportunity",
        binding=binding,
        owner=run.owner,
        space_id=run.space_id,
        presence_id=run.presence_id,
        sources=(),
        trigger_kind="intrinsic",
    )
    assert result.outcome == "accepted" and result.run is not None
    assert result.run.run_id != run.run_id
    await validate_self_initiative(
        service.database,
        run.run_id,
        conversation_id=run.conversation_id,
        space_id=run.space_id,
        presence_id=run.presence_id,
    )
    assert (await service.repository.get_run(run.run_id)).state == "running"
    assert (await service.work.get(task["id"]))["state"] == state
    assert set(r.run_id for r in await service.repository.list_active()) == {
        run.run_id,
        result.run.run_id,
    }
    third = await service.repository.accept_host_proposal(
        proposal_id="third-opportunity",
        binding=binding,
        owner=run.owner,
        space_id=run.space_id,
        presence_id=run.presence_id,
        sources=(),
        trigger_kind="intrinsic",
    )
    assert third.outcome == "accepted"
    assert third.run.run_id not in {run.run_id, result.run.run_id}


@pytest.mark.asyncio
async def test_running_child_does_not_block_intrinsic_admission_while_parent_waits(database):
    database.subagents_enabled = True
    service, _item, run, task = await setup(database)
    lease = await service.work.acquire(run.conversation_id, run.generation)
    workers = SubagentRepository(service.work)
    identity = await workers.start(
        lease,
        task["id"],
        "original-child",
        {"goal": "verify original work"},
    )
    child_lease = await workers.acquire(identity)
    assert child_lease is not None and child_lease.work_id == identity
    await service.work.checkpoint(
        child_lease, identity, {"progress": "original"}, models=2, tools=3
    )

    async def validate():
        assert await service.work.valid(lease)

    control = WorkControl(
        service.work,
        lease,
        task["source_key"],
        json.loads(task["source_json"]),
        validate,
        current=await service.work.get(task["id"]),
    )
    waited = json.loads(
        await control.execute(
            "task_control",
            {"action": "wait", "conditions": [{"kind": "owned_run", "run_id": identity}]},
            "wait-original-child",
        )
    )
    assert waited["ok"]
    await control.settle(pending_inputs=False)
    assert (await service.work.get(task["id"]))["state"] == "waiting_external"
    await service.work.release(lease)
    assert not await service.work.valid(lease) and await service.work.valid(child_lease)
    before = await service.work.get(identity)
    child_before = await workers.related(task["id"], identity)
    assert before["state"] == "running"
    await reconcile_page(service, (run,))
    binding = await service.repository.get_binding(run.conversation_id, run.generation)
    result = await service.repository.accept_host_proposal(
        proposal_id="intrinsic-while-child-runs",
        binding=binding,
        owner=run.owner,
        space_id=run.space_id,
        presence_id=run.presence_id,
        sources=(),
        trigger_kind="intrinsic",
    )
    assert result.outcome == "accepted" and result.run is not None
    assert result.run.run_id != run.run_id
    assert await service.work.get(identity) == before
    assert await workers.related(task["id"], identity) == child_before
    assert await service.work.valid(child_lease)
    assert (await service.repository.get_run(run.run_id)).state == "running"
    assert (await service.work.get(task["id"]))["state"] == "waiting_external"
    await service.work.release(child_lease)


@pytest.mark.asyncio
async def test_active_outbox_lists_all_retained_runs_past_former_128_limit(database):
    service, _, original, task = await setup(database)
    await set_work(database, task, state="suspended")
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add_all(
            InitiativeRunModel(
                id=str(uuid4()),
                proposal_id=f"retained-{i}",
                conversation_id=original.conversation_id,
                generation=original.generation,
                owner=original.owner.value,
                controller_epoch=original.controller_epoch_at_acceptance,
                space_id=original.space_id,
                presence_id=original.presence_id,
                payload_hash="0" * 64,
                sources_json="[]",
                support_refs_json="[]",
                trigger_kind="intrinsic",
                state="running",
                feedback_sequence=1,
                created_at=now,
                updated_at=now,
            )
            for i in range(129)
        )
    runs = await service.repository.list_active()
    assert len(runs) == 130
    assert original.run_id in {run.run_id for run in runs}


@pytest.mark.asyncio
async def test_sequence_caption_direct_and_semantic_paths_share_one_logical_effect(database):
    service, item, run, task = await setup(database)
    prefix = hashlib.sha256(b"original-send").hexdigest()[:24]
    first = await social(database, run, f"seq:{prefix}:0", seconds=-2)
    await social(database, run, f"seq:{prefix}:1", seconds=-1)
    await social(
        database, run, "caption", turn=f"social-caption:{first.id}", action="send_file_caption"
    )
    await sync_scope_effects(service, item)
    assert len(item.controller.state.effects) == 1
    await set_work(database, task, state="completed")
    await reconcile_page(service, (run,))
    await sync_scope_effects(service, item)
    assert len(item.controller.state.effects) == 1
    assert item.controller.state.feedback[run.run_id].outcome == "completed"
    await social(database, run, "another-message", turn=f"{run.conversation_id}:event:999")
    await sync_scope_effects(service, item)
    assert len(item.controller.state.effects) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [AutonomyOwner.SEMANTIC, AutonomyOwner.LEGACY])
async def test_only_semantic_run_binds_confirmed_internal_outbound_anchor(
    database, monkeypatch, owner
):
    service, item, run, _ = await setup(database, owner=owner)
    row = await social(database, run, "confirmed-send")
    row.event_id = 123  # Synthetic internal event ID; no platform message ID is used.
    monkeypatch.setattr(participation_feedback, "_scope_social_rows", AsyncMock(return_value=[row]))
    await sync_scope_effects(service, item)
    if owner is AutonomyOwner.SEMANTIC:
        assert item.controller.state.outbound_anchors["event:123"].run_ref == run.run_id
    else:
        assert not item.controller.state.outbound_anchors


@pytest.mark.asyncio
async def test_legacy_self_report_uses_real_charged_run_without_semantic_proposal(database):
    service, item, run, task = await setup(database, owner=AutonomyOwner.LEGACY)
    item.controller.state.proposals.clear()
    await set_work(database, task, state="completed", model_requests=1)
    report = {
        "run_ref": run.run_id,
        "sequence": 1,
        "response_id": "real-response-id",
        "at": time.time(),
        "delta": {"engage": "quiet", "mood": "平静"},
    }
    async with database.immediate_session() as session:
        await session.execute(
            journal.insert().values(
                work_id=task["id"],
                chain_id=str(uuid4()),
                contract="synthetic",
                source_revision=1,
                phase="delivered",
                updated=time.time(),
                payload_json=json.dumps(
                    {
                        "metadata": {
                            "progress": {
                                "self_reports": [
                                    report,
                                    {"bad": "record"},
                                    {**report, "run_ref": "unrelated"},
                                ],
                            }
                        }
                    }
                ),
            )
        )
    await reconcile_page(service, (run,))
    assert item.controller.state.engagement_report.run_ref == run.run_id
    assert item.controller.state.engagement_report.delta.model_dump() == {"engage": "quiet"}
    assert item.controller._willingness(time.time()) < 0
    assert not item.controller.state.proposals
    assert not item.controller.state.feedback
    await reconcile_page(service, (run,))
    assert len(item.controller.state.self_reports) == 1


@pytest.mark.asyncio
async def test_worker_and_root_model_charges_are_paged_without_summing_budget_twice(database):
    service, item, run, task = await setup(database)
    child_id = str(uuid4())
    await set_work(database, task, model_requests=26, state="completed")
    async with database.immediate_session() as session:
        child = {
            **task,
            "id": child_id,
            "parent_work_id": task["id"],
            "source_key": f"child:{child_id}",
            "state": "completed",
            "model_requests": 94,
        }
        await session.execute(work.insert().values(**child))
        await session.execute(
            children.insert().values(
                work_id=child_id,
                source_key=child["source_key"],
                brief_json="{}",
            )
        )
        await session.execute(budgets.insert().values(root_id=task["id"], models=120, tools=0))
    await reconcile_page(service, (run,))
    assert len(item.controller.state.effects) == 120
    assert (
        sum(key.startswith(f"work-model:{task['id']}:") for key in item.controller.state.effects)
        == 26
    )
    assert (
        sum(key.startswith(f"work-model:{child_id}:") for key in item.controller.state.effects)
        == 94
    )
    async with database.read_session() as session:
        assert await session.scalar(select(func.count()).select_from(InitiativeFeedbackModel)) == 2
