"""A pause notification is maintenance of the original episode, not resumed work."""

import json
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from tests.conftest import build_harness, make_settings
from tests.support.runtime_execution import make_work_resumer
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.activation_outcome import WorkActivationHandled, WorkNoProgress
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_scheduler import WorkScheduler
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, work
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _paused(database, tmp_path):
    env = await social_env(database, tmp_path)
    provider = FakeLLMProvider()
    harness = build_harness(
        database, make_settings(database.url, enabled_groups_csv="20001"), provider
    )
    chat = harness.processor._chat
    repository = WorkRepository(database)
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel))
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_user_id": "10001",
        "actor_person_id": env.person,
        "trigger_event_id": event.id,
        "generation": 1,
        "presence_id": env.presence,
        "bot_user_id": "80001",
    }
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "pause-test", source, validate)
    control.current = await repository.accept(
        lease, source_key="pause-test", source=source, goal="keep original work"
    )
    identity = control.current["id"]
    control.session = WorkSession(control, "contract")
    await control.session.restore(TurnTranscript((ChatMessage("user", "original"),)))
    assert await repository.prepare_effect(lease, identity, "business-effect", "tool")
    await repository.record_effect("business-effect", "accepted", {"automation_id": 93})
    await control.session.save("dispatched")
    await repository.checkpoint(lease, identity, None, models=1, tools=1)
    # Historical accounting survives recovery but is no longer updated by execution.
    async with database.sessions() as session, session.begin():
        await session.execute(update(work).where(work.c.id == identity).values(active_seconds=7))
    # A terminal child signal can still be pending when the original source changes.
    await repository.enqueue(
        lease.conversation_id, 1, "terminal-child", kind="completion", work_id=identity
    )
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == lease.conversation_id)
            .values(prompt_source_revision=CanonicalConversationModel.prompt_source_revision + 1)
        )
    await control.recover_failure(WorkConflict("work_journal_source_changed"))
    await repository.release(lease)
    generate = AsyncMock(side_effect=AssertionError("notice must not execute a model turn"))
    resumer = make_work_resumer(
        repository,
        ledger=harness.ledger,
        scopes=chat._conversation_scopes,
        turns=chat._turn_coordinator,
        router=env.router,
        config=chat._runtime_config,
        generate_self=generate,
        generate_wakeup=generate,
        validate_snapshot=chat.validate_turn_snapshot,
        run_effect=chat.run_effect,
        bindings=chat.runtime.bindings,
    )
    scheduler = WorkScheduler(repository, resumer, chat_admission_enabled=False)
    scheduler._last_reclaim = time.monotonic()
    return env, repository, resumer, scheduler, identity, source, generate, provider


async def _facts(database, identity):
    async with database.sessions() as session:
        notice = (await session.execute(select(deliveries))).mappings().all()
        original_recovery = (
            (await session.execute(select(recovery).where(recovery.c.work_id == identity)))
            .mappings()
            .one()
        )
        business = (
            (
                await session.execute(
                    select(effects).where(effects.c.effect_key == "business-effect")
                )
            )
            .mappings()
            .one()
        )
        mailbox = (await session.execute(select(inputs))).mappings().all()
    return [dict(n) for n in notice], dict(original_recovery), dict(business), list(mailbox)


@pytest.mark.asyncio
async def test_notice_with_pending_input_and_changed_source_never_requeues_execution(
    database, tmp_path
):
    env, repo, _, scheduler, identity, _, generate, provider = await _paused(database, tmp_path)
    before = await repo.get(identity)
    async with database.sessions() as session:
        budget_before = (await session.execute(select(budgets))).mappings().one()
    planned, original_recovery, business, mailbox = await _facts(database, identity)
    await database.close()  # Original persisted key survives physical connection reopening.
    await scheduler.drain_once()
    await scheduler.drain_once()
    after = await repo.get(identity)
    notices, saved_recovery, saved_business, saved_mailbox = await _facts(database, identity)
    assert len(notices) == 1 and notices[0]["id"] == planned[0]["id"]
    assert notices[0]["state"] == "accepted"
    assert len([c for c in env.bot.calls if c[0] == "send_group_msg"]) == 1
    for key in (
        "state",
        "reason",
        "revision",
        "model_requests",
        "tool_calls",
        "active_seconds",
        "checkpoint_json",
    ):
        assert after[key] == before[key]
    assert after["state"] == "suspended" and after["sent_messages"] == before["sent_messages"] + 1
    assert saved_recovery == original_recovery and saved_business == business
    assert saved_mailbox == mailbox and mailbox[0]["state"] == "pending"
    async with database.sessions() as session:
        assert (await session.execute(select(budgets))).mappings().one() == budget_before
    generate.assert_not_awaited()
    assert not provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["unknown_send", "ledger_after_acceptance", "intent_after_acceptance", "scene_validation"],
)
async def test_notice_failure_is_fail_closed_without_new_pause_or_dispatch(
    database, tmp_path, monkeypatch, failure
):
    env, repo, resumer, scheduler, identity, _, generate, _ = await _paused(database, tmp_path)
    _, original_recovery, business, _ = await _facts(database, identity)
    original = await repo.get(identity)
    if failure == "unknown_send":
        from qq_ai_bot.adapters.onebot.sender import OneBotRouteSender

        attempted = AsyncMock(side_effect=TimeoutError())
        monkeypatch.setattr(OneBotRouteSender, "send", attempted)
    elif failure == "ledger_after_acceptance":
        monkeypatch.setattr(resumer.services.ledger, "append", AsyncMock(side_effect=OSError()))
    elif failure == "intent_after_acceptance":
        import qq_ai_bot.runtime.delivery_intents as module

        original_record = module.record
        failed = False

        async def record(control, key, state, receipt):
            nonlocal failed
            if state == "accepted" and not failed:
                failed = True
                raise OSError("receipt publication failed")
            await original_record(control, key, state, receipt)

        monkeypatch.setattr(module, "record", record)
    else:
        import qq_ai_bot.services.work_resume as module

        attempted = AsyncMock(side_effect=ValueError("invalid source"))
        monkeypatch.setattr(module, "recover_source", attempted)
    await scheduler.drain_once()
    await scheduler.drain_once()
    notices, saved_recovery, saved_business, _ = await _facts(database, identity)
    current = await repo.get(identity)
    assert len(notices) == 1
    assert (
        notices[0]["state"]
        == {
            "unknown_send": "unknown",
            "ledger_after_acceptance": "accepted",
            "intent_after_acceptance": "accepted",
            "scene_validation": "failed",
        }[failure]
    )
    assert saved_recovery == original_recovery and saved_business == business
    assert (
        current["state"] == "suspended" and current["active_seconds"] == original["active_seconds"]
    )
    sends = [c for c in env.bot.calls if c[0] == "send_group_msg"]
    assert len(sends) == int(failure in {"ledger_after_acceptance", "intent_after_acceptance"})
    if failure in {"unknown_send", "scene_validation"}:
        assert attempted.await_count == 1
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_episode_reuses_key_but_real_execution_failure_creates_new_notice(
    database, tmp_path
):
    _, repo, _, scheduler, identity, source, _, _ = await _paused(database, tmp_path)
    await scheduler.drain_once()
    notices, original_recovery, _, _ = await _facts(database, identity)
    old_key = notices[0]["id"]
    lease = await repo.acquire((await repo.get(identity))["conversation_id"], 1)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "pause-test", source, validate)
    control.current = await repo.get(identity)
    await control.recover_failure(WorkNoProgress("still paused"))
    await repo.release(lease)
    repeated, saved, _, _ = await _facts(database, identity)
    assert len(repeated) == 1 and repeated[0]["id"] == old_key
    assert saved["activation_id"] == original_recovery["activation_id"]

    # An explicit execution activation is a new episode, unlike notice maintenance.
    async def valid():
        return None

    with pytest.raises(WorkActivationHandled):
        async with activate_work(
            repo,
            control.current["conversation_id"],
            1,
            "pause-test",
            source,
            valid,
            work_id=identity,
        ):
            raise WorkNoProgress("new resumed execution failed")
    renewed, _, _, _ = await _facts(database, identity)
    assert len(renewed) == 2
    assert {n["id"] for n in renewed} > {old_key}
    assert next(n for n in renewed if n["id"] == old_key)["state"] == "accepted"
    await scheduler.drain_once()
    renewed, _, _, _ = await _facts(database, identity)
    assert all(n["state"] == "accepted" for n in renewed)


@pytest.mark.asyncio
@pytest.mark.parametrize("effect_state", ["accepted", "prepared", "unknown", "failed"])
async def test_existing_effect_acceptance_closes_original_planned_notice_without_send(
    database, tmp_path, effect_state
):
    env, repo, _, scheduler, identity, _, _, _ = await _paused(database, tmp_path)
    notices, original_recovery, _, _ = await _facts(database, identity)
    key = notices[0]["id"]
    current = await repo.get(identity)
    lease = await repo.acquire(current["conversation_id"], 1)
    assert await repo.prepare_effect(lease, identity, key, "progress")
    if effect_state != "prepared":
        await repo.record_effect(
            key,
            effect_state,
            {"transport_accepted": True, "message_id": "original"}
            if effect_state == "accepted"
            else {},
        )
    await repo.release(lease)
    await scheduler.drain_once()
    await scheduler.drain_once()
    notices, saved, _, _ = await _facts(database, identity)
    assert len(notices) == 1 and notices[0]["id"] == key
    assert notices[0]["state"] == ("unknown" if effect_state == "prepared" else effect_state)
    if effect_state == "accepted":
        assert json.loads(notices[0]["receipt_json"])["message_id"] == "original"
    assert saved == original_recovery
    assert not [c for c in env.bot.calls if c[0] == "send_group_msg"]


@pytest.mark.asyncio
async def test_legacy_fourteen_accepted_and_one_planned_keep_all_original_keys(database, tmp_path):
    env, _, _, scheduler, identity, _, generate, _ = await _paused(database, tmp_path)
    now = time.time()
    legacy = [
        dict(
            id=f"notice:{identity}:legacy-{index}",
            work_id=identity,
            kind="notice",
            target_key="original",
            state="accepted",
            payload_json='{"text":"old notice"}',
            receipt_json=json.dumps({"transport_accepted": True, "message_id": str(index)}),
            created=now,
            updated=now,
        )
        for index in range(14)
    ]
    async with database.sessions() as session, session.begin():
        await session.execute(insert(deliveries), legacy)
    notices, saved_recovery, _, _ = await _facts(database, identity)
    before_accepted = {n["id"]: n for n in notices if n["state"] == "accepted"}
    planned_key = next(n["id"] for n in notices if n["state"] == "planned")
    await scheduler.drain_once()
    await scheduler.drain_once()
    notices, final_recovery, _, _ = await _facts(database, identity)
    assert len(notices) == 15 and all(n["state"] == "accepted" for n in notices)
    assert {n["id"]: n for n in notices if n["id"] != planned_key} == before_accepted
    assert saved_recovery == final_recovery
    assert len([c for c in env.bot.calls if c[0] == "send_group_msg"]) == 1
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_suspended_candidate_cannot_suspend_new_explicit_resume(database, tmp_path):
    env, repo, resumer, _, identity, _, generate, _ = await _paused(database, tmp_path)
    stale = await repo.get(identity)
    _, original_recovery, _, _ = await _facts(database, identity)
    lease = await repo.acquire(stale["conversation_id"], 1)
    queued = await repo.transition(
        lease, identity, stale["revision"], "queued", reason="explicit_resume"
    )
    await repo.release(lease)
    await resumer.resume(stale)
    assert await repo.get(identity) == queued
    notices, current_recovery, _, _ = await _facts(database, identity)
    assert len(notices) == 1 and notices[0]["state"] == "planned"
    assert current_recovery == original_recovery
    assert not [c for c in env.bot.calls if c[0] == "send_group_msg"]
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("before_dispatch", [False, True])
async def test_notice_reconciliation_failure_preserves_original_cause(
    database, tmp_path, monkeypatch, before_dispatch
):
    _, repo, resumer, _, identity, _, _, _ = await _paused(database, tmp_path)
    item = await repo.get(identity)
    _, original_recovery, business, _ = await _facts(database, identity)
    original = OSError("original receipt or scene failure")
    monkeypatch.setattr(
        resumer, "_record_notice_failure", AsyncMock(side_effect=TypeError("repair failure"))
    )
    if before_dispatch:
        import qq_ai_bot.services.work_resume as module

        monkeypatch.setattr(module, "recover_source", AsyncMock(side_effect=original))
        with pytest.raises(OSError) as caught:
            await resumer.resume(item)
        assert caught.value is original
    else:
        from qq_ai_bot.adapters.onebot.sender import OneBotRouteSender

        sent = AsyncMock(side_effect=original)
        monkeypatch.setattr(OneBotRouteSender, "send", sent)
        with pytest.raises(WorkActivationHandled) as caught:
            await resumer._resume(item, json.loads(item["source_json"]))
        assert caught.value.__cause__ is original
        # Dispatching is not a definitely unsubmitted plan and cannot be selected again.
        scheduler = WorkScheduler(repo, resumer, chat_admission_enabled=False)
        scheduler._last_reclaim = time.monotonic()
        await scheduler.drain_once()
        assert sent.await_count == 1
    assert any(
        "reconciliation deferred" in n or "recovery deferred" in n for n in original.__notes__
    )
    notices, saved_recovery, saved_business, _ = await _facts(database, identity)
    assert len(notices) == 1
    assert notices[0]["state"] == ("planned" if before_dispatch else "dispatching")
    assert saved_recovery == original_recovery and saved_business == business
    assert (await repo.get(identity))["state"] == "suspended"
