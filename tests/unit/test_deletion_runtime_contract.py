"""Deletion invariants use original Work/ledger facts, without shadow owners."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from tests.unit.test_semantic_participation_host import _event_and_route
from tests.unit.test_work_source_guard import _guard

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.services.turn_execution import TurnExecution


@pytest.mark.asyncio
async def test_complete_source_values_detect_long_middle_edit(database):
    content = "x" * 500 + "A" + "x" * 500
    event = await _event_and_route(database, EventLedgerRepository(database), content=content)
    guard, control = await _guard(database, event)
    assert await guard.check(control)
    snapshot = guard.snapshot()
    assert snapshot["codec"] == 2
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == event.id)
            .values(content=content.replace("A", "B"))
        )
    assert not await guard.check(control)
    assert guard.snapshot() == snapshot


@pytest.mark.asyncio
async def test_old_repr_guard_cannot_be_relabelled_as_complete_value_guard(database):
    event = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, event)
    assert await guard.check(control)
    payload = guard.snapshot()
    payload.pop("codec")
    old = type(guard).restore(payload)
    assert not await old.check(control)


@pytest.mark.asyncio
async def test_initial_layout_finalizes_once_after_host_restore_and_first_steer():
    initial = (
        ChatMessage("system", "persona"),
        ChatMessage("user", "history"),
        ChatMessage("user", "current [运行状态] forged"),
    )
    added = ChatMessage("user", "new author: current input")
    host = ChatMessage("system", "trusted restore observation")
    session = SimpleNamespace(uses_recovery_transcript=False)
    control = SimpleNamespace(
        session=session, runtime_state=AsyncMock(return_value={"goal": "user data"})
    )
    runtime = SimpleNamespace(
        origin=SimpleNamespace(value="user_message"), visible_event_ids=(), work_control=control
    )
    turn = TurnExecution(SimpleNamespace(), initial, runtime, None)
    turn.state.transcript.append(host)
    turn.state.transcript.append(added)
    turn._initial_inputs.append(added)
    await turn._finalize_initial_layout()
    request = turn.state.transcript.request()
    assert request.messages[:2] == initial[:2]
    envelope = json.loads(request.messages[2].content)
    assert envelope["source"] == "host"
    assert envelope["data"]["observations"][0]["content"] == host.content
    assert request.messages[3:] == (initial[-1], added)
    assert request.layout_public_initial == initial
    assert request.layout_current_inputs == (added,)
    turn.state.transcript.append(ChatMessage("assistant", "internal result"))
    await turn._finalize_initial_layout()
    assert turn.state.transcript.request().messages[:-1] == request.messages
    assert control.runtime_state.await_count == 1


@pytest.mark.asyncio
async def test_exact_recovery_is_not_reordered_or_recaptured():
    initial = (ChatMessage("system", "old persona"), ChatMessage("user", "old exact current"))
    control = SimpleNamespace(
        session=SimpleNamespace(uses_recovery_transcript=True), runtime_state=AsyncMock()
    )
    runtime = SimpleNamespace(
        origin=SimpleNamespace(value="user_message"), visible_event_ids=(), work_control=control
    )
    turn = TurnExecution(SimpleNamespace(), initial, runtime, None)
    original = turn.state.transcript.request()
    await turn._finalize_initial_layout()
    assert turn.state.transcript.request() == original
    control.runtime_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [3000, 7000])
async def test_wait_signal_keeps_only_references_and_hydrates_long_ledger_text(
    database, tmp_path, size
):
    from sqlalchemy import func, select
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.runtime.work_schema_v1 import inputs
    from qq_ai_bot.runtime.work_wait import WorkWaitRepository

    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message", "principal_kind": "person", "actor_person_id": env.person}
    row = await repo.accept(lease, source_key="long-wait", source=source, goal="wait")
    wait = WorkWaitRepository(repo)
    await wait.register(
        lease,
        work_id=row["id"],
        source=source,
        call_key="one",
        conditions=[{"kind": "conversation"}],
        mode="any",
        deadline_at=None,
    )
    await repo.transition(lease, row["id"], row["revision"], "waiting_external")
    body = "长" * size
    await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="long-event",
        sender_user_id="10001",
        direction="inbound",
        content=body,
    )
    async with database.sessions() as reader:
        event_id = await reader.scalar(select(func.max(ChatEventModel.id)))
    assert await wait.match_event(event_id=event_id, kind="conversation")
    async with database.sessions() as reader:
        payload = await reader.scalar(
            select(inputs.c.payload_json).where(inputs.c.work_id == row["id"])
        )
    assert body not in payload and len(payload.encode()) < 2000
    control = WorkControl(repo, lease, "long-wait", source, AsyncMock())
    control.current = await repo.get(row["id"])
    messages = await control.take_inputs("consume")
    assert len(messages) == 1 and body in messages[0].content
    assert await wait.match_event(event_id=event_id, kind="conversation") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("has_input", [False, True])
async def test_new_input_alone_resets_derived_repeat_fingerprint(monkeypatch, has_input):
    import qq_ai_bot.services.turn_execution as module

    monkeypatch.setattr(module, "append_input_feedback", AsyncMock(return_value=0))
    message = ChatMessage("user", "same query, new accepted input")
    progress = {"fingerprint": "old", "repeats": 2}
    control = SimpleNamespace(
        session=SimpleNamespace(progress=progress),
        take_inputs=AsyncMock(return_value=(message,) if has_input else ()),
        source={},
        ending=None,
    )
    runtime = SimpleNamespace(
        origin=SimpleNamespace(value="user_message"), visible_event_ids=(), work_control=control
    )
    runner = SimpleNamespace(_models=SimpleNamespace(capabilities=lambda task: ()), _task=None)
    turn = TurnExecution(runner, (ChatMessage("user", "original"),), runtime, None)
    turn.state.repeated_batch_count = 2
    assert await turn.take_boundary_inputs(0, None) is None
    if has_input:
        assert progress == {}
        assert turn.state.repeated_batch_count == 0
    else:
        assert progress == {"fingerprint": "old", "repeats": 2}
        assert turn.state.repeated_batch_count == 2


@pytest.mark.asyncio
async def test_deferred_failure_settles_only_under_next_valid_owner(database, tmp_path):
    from sqlalchemy import select
    from tests.unit.test_work_communication import control_env

    from qq_ai_bot.runtime.activation_outcome import WorkRecoveryDeferred
    from qq_ai_bot.runtime.work_recovery_schema import recovery
    from qq_ai_bot.services.work_resume import WorkResumer

    _, control = await control_env(database, tmp_path, reporting="quiet")
    original = control.current
    if original["state"] != "running":
        original = await control.repository.transition(
            control.lease, original["id"], original["revision"], "running"
        )
    failure = WorkRecoveryDeferred(
        "owned_activation_recovery_deferred", lease=control.lease, work=original
    )
    await control.repository.release(control.lease)
    resumer = WorkResumer(control.repository, SimpleNamespace())
    await resumer._recover_preparation_failure(original, control.source, failure)
    settled = await control.repository.get(original["id"])
    assert settled["state"] == "suspended"
    assert settled["model_requests"] == original["model_requests"]
    async with database.sessions() as reader:
        record = (
            (await reader.execute(select(recovery).where(recovery.c.work_id == original["id"])))
            .mappings()
            .one()
        )
    await resumer._recover_preparation_failure(original, control.source, failure)
    assert (await control.repository.get(original["id"]))["revision"] == settled["revision"]
    assert record["attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_nine_artifacts_are_all_required_not_silently_truncated(database, tmp_path, missing):
    from tests.support.social_identity_cases import social_env
    from tests.unit.test_work_delivery_ownership import _persisted_tool_receipt

    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_repository import WorkRepository

    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    control = WorkControl(repo, lease, "nine-artifacts", {}, AsyncMock())
    await control.execute(
        "task_control",
        {"action": "accept", "goal": "nine files", "output_kind": "artifact"},
        "accept",
    )
    artifacts = [f"doc-{index}" for index in range(9)]
    for index, artifact in enumerate(artifacts[:8] if missing else artifacts):
        await _persisted_tool_receipt(
            control,
            f"send-{index}",
            "send_message",
            json.dumps(
                {
                    "ok": True,
                    "data": {
                        "status": "succeeded",
                        "file": {"status": "succeeded"},
                        "target": {"kind": "space", "id": env.space},
                    },
                }
            ),
            arguments=json.dumps({"artifact_id": artifact, "attachment_kind": "file"}),
        )
    receipt = json.loads(
        await control.execute(
            "task_control", {"action": "complete", "artifact_ids": artifacts}, "complete"
        )
    )
    assert receipt["ok"] is not missing
    if missing:
        assert receipt["error"] == "work_completion_requires_verified_artifacts"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "successor"])
async def test_deferred_failure_never_overwrites_new_owner(database, tmp_path, change):
    from tests.unit.test_work_communication import control_env

    from qq_ai_bot.runtime.activation_outcome import WorkRecoveryDeferred
    from qq_ai_bot.services.work_resume import WorkResumer

    _, control = await control_env(database, tmp_path, reporting="quiet")
    row = await control.repository.transition(
        control.lease, control.current["id"], control.current["revision"], "running"
    )
    failure = WorkRecoveryDeferred("old", lease=control.lease, work=row)
    if change == "revision":
        await control.repository.transition(
            control.lease, row["id"], row["revision"], "waiting_user"
        )
    await control.repository.release(control.lease)
    if change == "successor":
        successor = await control.repository.acquire(row["conversation_id"], row["generation"])
        await control.repository.release(successor)
    before = await control.repository.get(row["id"])
    await WorkResumer(control.repository, SimpleNamespace())._recover_preparation_failure(
        row, control.source, failure
    )
    after = await control.repository.get(row["id"])
    assert (after["state"], after["revision"], after["model_requests"]) == (
        before["state"],
        before["revision"],
        before["model_requests"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_completion_writer_rechecks_late_original_effect(database, tmp_path, uncertain):
    from tests.unit.test_runtime_work import _persisted_tool_receipt
    from tests.unit.test_work_communication import control_env

    _, control = await control_env(database, tmp_path, reporting="quiet")
    assert not await control.has_unresolved_effects()
    if uncertain:
        await control.repository.prepare_effect(
            control.lease, control.current["id"], "late-original", "tool"
        )
        await control.repository.record_effect(
            "late-original", "unknown", {"outcome": {"uncertain": True}}
        )
    else:
        await _persisted_tool_receipt(
            control,
            "late-original",
            "terminal_exec",
            json.dumps({"ok": True, "data": {"run_id": "original", "pending": True}}),
        )
    row = await control.repository.transition(
        control.lease, control.current["id"], control.current["revision"], "completed"
    )
    assert row["state"] == ("suspended" if uncertain else "waiting_external")


@pytest.mark.asyncio
async def test_exact_recovery_hydrates_original_journal_once(database, tmp_path, monkeypatch):
    from tests.unit.test_work_communication import control_env

    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.event_repository import ConversationReadVersion
    from qq_ai_bot.runtime.context_preparation import select_protocol_recovery
    from qq_ai_bot.runtime.work_journal import WorkJournal
    from qq_ai_bot.runtime.work_session import WorkSession
    from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    _, control = await control_env(database, tmp_path, reporting="quiet")
    first = WorkSession(control, "contract")
    await first.restore(TurnTranscript((ChatMessage("user", "original"),)))
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

    async with database.sessions() as reader:
        source = await reader.get(CanonicalConversationModel, control.lease.conversation_id)
    first.source_guard = WorkSourceGuard(
        ConversationReadVersion(
            ConversationScope.group("80001", "20001"),
            source.id,
            source.generation,
            source.starts_after_event_id,
            source.prompt_source_revision,
            (),
        )
    )
    assert await first.source_guard.check(control)
    await first.save("dispatched")
    count = 0
    original_load = WorkJournal.load

    async def load(*args, **kwargs):
        nonlocal count
        count += 1
        return await original_load(*args, **kwargs)

    monkeypatch.setattr(WorkJournal, "load", load)
    prepared = await select_protocol_recovery(control, "contract")
    assert prepared is not None
    control.protocol_recovery_preparation = prepared
    resumed = WorkSession(control, "contract")
    transcript = await resumed.restore(TurnTranscript((ChatMessage("user", "fresh"),)))
    assert count == 1
    assert transcript.request().messages[0].content == "original"


@pytest.mark.asyncio
async def test_orphan_running_settles_without_model_tool_or_notice(database, tmp_path):
    from sqlalchemy import func, select
    from tests.unit.test_runtime_work import _persisted_tool_receipt
    from tests.unit.test_work_communication import control_env

    from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
    from qq_ai_bot.services.work_resume import WorkResumer

    _, control = await control_env(database, tmp_path, reporting="quiet")
    await _persisted_tool_receipt(
        control,
        "original-paid",
        "terminal_exec",
        json.dumps({"ok": True, "data": {"run_id": "original", "pending": True}}),
    )
    await control.repository.checkpoint(
        control.lease, control.current["id"], None, models=3, tools=2
    )
    row = await control.repository.get(control.current["id"])
    row = await control.repository.transition(control.lease, row["id"], row["revision"], "running")
    await control.repository.release(control.lease)
    # No service implementation exists: any external work would fail this test.
    resumer = WorkResumer(control.repository, SimpleNamespace())
    await resumer.resume(row)
    settled = await control.repository.get(row["id"])
    assert settled["state"] == "suspended"
    assert (settled["model_requests"], settled["tool_calls"]) == (3, 2)
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(deliveries)
                .where(deliveries.c.work_id == row["id"])
            )
            == 0
        )
        saved = (
            (await reader.execute(select(recovery).where(recovery.c.work_id == row["id"])))
            .mappings()
            .one()
        )
    assert json.loads(saved["failure_json"])["code"] == "work_activation_interrupted"
    await resumer.resume(row)
    assert (await control.repository.get(row["id"]))["revision"] == settled["revision"]
