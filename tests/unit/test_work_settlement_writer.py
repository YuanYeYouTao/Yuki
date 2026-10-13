"""One accepted control and one settlement writer for normal and failure exits."""

import json

import pytest
from sqlalchemy import func, select
from tests.support.runtime_work_helpers import _persisted_tool_receipt
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs, work
from qq_ai_bot.runtime.work_wait import WorkWaitRepository, normalize_conditions


async def _running_work(database, tmp_path, *, interactive=False, goal="answer"):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository,
        lease,
        "settlement-writer",
        {
            "actor_person_id": env.person,
            "principal_kind": "person",
            "origin": "user_message",
            **({} if interactive else {"delivery_contract": "return_to_caller"}),
        },
        validate,
    )
    accepted = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "accept",
                "goal": goal,
                **({"reporting": "interactive"} if interactive else {}),
            },
            "accept",
        )
    )
    assert accepted["ok"]
    return repository, lease, control


async def _late_input(repository, lease, identity, *, ready):
    input_id = await repository.enqueue(
        lease.conversation_id,
        lease.generation,
        f"late-{ready}",
        kind="message",
        work_id=identity,
        ready=False,
    )
    if ready:
        assert await repository.prepare_input(input_id, {"text": "change of plan"})


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
async def test_recovery_after_accepted_complete_retains_late_input(database, tmp_path, ready):
    """T03 counterexample: recover_failure used to UPDATE completed past a new input."""
    repository, lease, control = await _running_work(database, tmp_path)
    result = json.loads(
        await control.execute(
            "task_control", {"action": "complete", "result": "internal answer"}, "complete"
        )
    )
    assert result["ok"] and control.ending == "completed"
    await _late_input(repository, lease, control.current["id"], ready=ready)
    await control.recover_failure(RuntimeError("crash before settlement"))
    row = await repository.get(control.current["id"])
    assert row["state"] == ("queued" if ready else "waiting_external")
    # The candidate never became a replayable success.
    assert "sync_result" not in json.loads(row["checkpoint_json"])


@pytest.mark.asyncio
async def test_recovery_failure_does_not_strand_ready_input_on_failed_work(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute("task_control", {"action": "fail", "reason": "blocked"}, "fail")
    )["ok"]
    await _late_input(repository, lease, control.current["id"], ready=True)
    await control.recover_failure(RuntimeError("crash before settlement"))
    assert (await repository.get(control.current["id"]))["state"] == "queued"


@pytest.mark.asyncio
async def test_recovery_commits_accepted_complete_and_publishes_result_atomically(
    database, tmp_path
):
    repository, _lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute(
            "task_control", {"action": "complete", "result": "internal answer"}, "complete"
        )
    )["ok"]
    await control.recover_failure(RuntimeError("crash after pairing"))
    row = await repository.get(control.current["id"])
    checkpoint = json.loads(row["checkpoint_json"])
    assert row["state"] == "completed"
    assert checkpoint["sync_result"] == "internal answer"
    assert "accepted_control" not in checkpoint


@pytest.mark.asyncio
async def test_exception_without_accepted_control_never_mints_completion(database, tmp_path):
    repository, _lease, control = await _running_work(database, tmp_path)
    await control.recover_failure(RuntimeError("provider failure"))
    assert (await repository.get(control.current["id"]))["state"] == "failed"


@pytest.mark.asyncio
async def test_new_input_withdraws_accepted_control(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute("task_control", {"action": "complete", "result": "stale"}, "complete")
    )["ok"]
    await _late_input(repository, lease, control.current["id"], ready=True)
    assert await control.take_inputs("attempt")
    assert control.ending is None and control.accepted is None


@pytest.mark.asyncio
async def test_host_checkpoint_patch_keeps_accepted_control(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute("task_control", {"action": "fail", "reason": "blocked"}, "fail")
    )["ok"]
    await repository.checkpoint(lease, control.current["id"], {"transcript_ref": "x"})
    row = await repository.get(control.current["id"])
    assert json.loads(row["checkpoint_json"])["accepted_control"]["action"] == "fail"


@pytest.mark.asyncio
@pytest.mark.parametrize("implicit", [False, True])
async def test_answer_completion_does_not_require_send_classification(database, tmp_path, implicit):
    repository, _lease, control = await _running_work(database, tmp_path, interactive=True)
    await _persisted_tool_receipt(
        control,
        "answer-send",
        "send_message",
        json.dumps({"ok": True, "data": {"status": "succeeded"}}),
        arguments=json.dumps({"text": "已经完成分析。"}),
    )
    # The real send is retained, but it does not decide whether the goal is complete.
    assert control.accepted is None
    assert (await repository.get(control.current["id"]))["state"] == "running"
    if implicit:
        await control.complete_final("analysis complete", "final")
    else:
        result = json.loads(
            await control.execute(
                "task_control", {"action": "complete", "result": "analysis complete"}, "complete"
            )
        )
        assert result["ok"]
    await control.settle(pending_inputs=False)
    assert (await repository.get(control.current["id"]))["state"] == "completed"


@pytest.mark.asyncio
async def test_reporting_can_be_quiet_without_changing_goal_or_wait(database, tmp_path):
    repository, _lease, control = await _running_work(database, tmp_path, interactive=True)
    assert json.loads(
        await control.execute(
            "task_control", {"action": "need_input", "reason": "which file?"}, "ask"
        )
    )["ok"]
    await control.settle(pending_inputs=False)
    before = await repository.get(control.current["id"])
    result = json.loads(
        await control.execute("task_control", {"action": "update", "reporting": "quiet"}, "quiet")
    )
    assert result["ok"] and control.reporting == "quiet"
    after = await repository.get(control.current["id"])
    assert after["goal"] == before["goal"]
    assert after["state"] == before["state"] == "waiting_user"

    invalid = json.loads(
        await control.execute(
            "task_control",
            {"action": "update", "goal": "must not be written", "reporting": "unsupported"},
            "invalid-reporting",
        )
    )
    assert not invalid["ok"] and invalid["error"] == "work_reporting_invalid"
    assert (await repository.get(control.current["id"]))["goal"] == before["goal"]


@pytest.mark.asyncio
async def test_caller_can_select_reporting_without_changing_delivery_contract(database, tmp_path):
    repository, _lease, control = await _running_work(database, tmp_path)
    result = json.loads(
        await control.execute(
            "task_control", {"action": "update", "reporting": "interactive"}, "reporting"
        )
    )
    assert result["ok"] and control.reporting == "interactive"
    before = await repository.get(control.current["id"])
    assert json.loads(before["source_json"])["delivery_contract"] == "return_to_caller"
    assert json.loads(
        await control.execute(
            "task_control", {"action": "complete", "result": "internal result"}, "complete"
        )
    )["ok"]
    await control.settle(pending_inputs=False)
    row = await repository.get(control.current["id"])
    assert row["state"] == "completed"
    assert json.loads(row["checkpoint_json"])["sync_result"] == "internal result"


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [False, True])
async def test_empty_final_can_complete_answer_without_send_or_result_gate(
    database, tmp_path, interactive
):
    repository, lease, control = await _running_work(database, tmp_path, interactive=interactive)
    await control.complete_final("", "quiet-final")
    await control.settle(pending_inputs=False)
    row = await repository.get(control.current["id"])
    assert row["state"] == "completed"
    assert json.loads(row["checkpoint_json"])["sync_result"] == ""
    await repository.release(lease)


@pytest.mark.asyncio
async def test_committed_mutation_with_empty_final_completes_without_visible_delivery(
    database, tmp_path
):
    repository, lease, control = await _running_work(database, tmp_path)
    await _persisted_tool_receipt(
        control,
        "committed-change",
        "memory_mutate",
        json.dumps({"ok": True, "data": {"status": "succeeded", "mutation_committed": True}}),
        arguments=json.dumps({"change": "confirmed"}),
    )
    await control.complete_final("", "mutation-empty-final")
    await control.settle(pending_inputs=False)
    assert (await repository.get(control.current["id"]))["state"] == "completed"
    await repository.release(lease)


@pytest.mark.asyncio
async def test_successful_send_keeps_work_open_until_explicit_completion(database, tmp_path):
    repository, _lease, control = await _running_work(database, tmp_path, interactive=True)
    await _persisted_tool_receipt(
        control,
        "claimed-success",
        "send_message",
        json.dumps({"ok": True, "data": {"status": "succeeded"}}),
        arguments=json.dumps({"text": "保存好了。"}),
    )
    assert control.accepted is None
    assert (await repository.get(control.current["id"]))["state"] == "running"
    result = json.loads(
        await control.execute("task_control", {"action": "complete", "result": "saved"}, "complete")
    )
    assert result["ok"]
    await control.settle(pending_inputs=False)
    assert (await repository.get(control.current["id"]))["state"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["need_input", "fail"])
@pytest.mark.parametrize("reason", [None, "", "尚需用户确认哪些材料可发布；" * 100])
async def test_detailed_target_and_wait_reason_are_preserved(database, tmp_path, action, reason):
    goal = "详细验收项；" * 4500
    repository, _lease, control = await _running_work(database, tmp_path, goal=goal)
    updated_goal = goal + "新增要求。"
    assert json.loads(
        await control.execute("task_control", {"action": "update", "goal": updated_goal}, "update")
    )["ok"]
    transition_reason = "详细状态变化说明；" * 100
    control.current = await repository.transition(
        control.lease,
        control.current["id"],
        control.current["revision"],
        "running",
        reason=transition_reason,
    )
    assert control.current["reason"] == transition_reason
    identity = control.current["id"]
    assert json.loads(
        await control.execute(
            "task_control",
            {"action": action, **({"reason": reason} if reason is not None else {})},
            "ask",
        )
    )["ok"]
    await control.settle(pending_inputs=False)
    row = await repository.get(control.current["id"])
    assert row["id"] == identity and row["state"] == (
        "waiting_user" if action == "need_input" else "failed"
    )
    assert row["goal"] == updated_goal
    assert json.loads(row["checkpoint_json"]).get("reason") == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subagent", "completion"])
async def test_auxiliary_input_does_not_withdraw_accepted_completion(database, tmp_path, kind):
    from sqlalchemy.dialects.sqlite import insert

    repository, lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute(
            "task_control", {"action": "complete", "result": "settled decision"}, "complete"
        )
    )["ok"]
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(inputs).values(
                conversation_id=lease.conversation_id,
                generation=lease.generation,
                work_id=control.current["id"],
                source_key=f"auxiliary:{kind}",
                kind=kind,
                ready=True,
                payload_json=json.dumps({"text": "original child/run finished"}),
                created=1,
            )
        )
    assert not await control.has_pending_business_inputs()
    assert len(await control.take_inputs("observe-original-result")) == 1
    assert control.accepted["action"] == "complete"
    await control.settle(pending_inputs=False)
    row = await repository.get(control.current["id"])
    assert row["state"] == "completed"
    assert json.loads(row["checkpoint_json"])["sync_result"] == "settled decision"


@pytest.mark.asyncio
async def test_optional_note_reporting_and_null_result_do_not_veto_completion(database, tmp_path):
    repository, _lease, control = await _running_work(database, tmp_path)
    result = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "complete",
                "result": None,
                "context_note": {"facts": [{"text": "draft", "refs": ["goal"]}]},
                "reporting": "interactive",
            },
            "complete-with-optional-fields",
        )
    )
    assert result["ok"]
    await control.settle(pending_inputs=False)
    row = await repository.get(control.current["id"])
    assert row["state"] == "completed"
    checkpoint = json.loads(row["checkpoint_json"])
    assert checkpoint["sync_result"] == ""
    assert "context_note" not in checkpoint  # Only update(context_note) saves it.


@pytest.mark.asyncio
async def test_private_note_keeps_original_work_without_public_publication(database, tmp_path):
    from qq_ai_bot.conversation.observation_models import ContextObservationModel
    from qq_ai_bot.runtime.work_context_note import visible_context_note
    from qq_ai_bot.tool_results.access import access_from_source

    repository, lease, control = await _running_work(database, tmp_path)
    identity = control.current["id"]
    control.bind_context_access(
        access_from_source(
            lease.conversation_id,
            lease.generation,
            {**control.source, "read_scope": json.dumps({"memory": []})},
        )
    )
    result = json.loads(
        await control.execute(
            "task_control",
            {"action": "update", "context_note": {"facts": [{"text": "saved", "refs": ["goal"]}]}},
            "save-optional-note",
        )
    )
    assert result["ok"]
    saved = json.loads((await repository.get(identity))["checkpoint_json"])["context_note"]
    assert saved["payload"]["facts"][0]["text"] == "saved"
    async with database.sessions() as reader:
        assert not await reader.scalar(
            select(ContextObservationModel.id).where(
                ContextObservationModel.source_work_id == identity
            )
        )
    assert await visible_context_note(control) == saved["payload"]
    assert json.loads((await repository.get(identity))["checkpoint_json"])["context_note"] == saved
    assert json.loads(
        await control.execute("task_control", {"action": "complete", "result": "done"}, "done")
    )["ok"]
    await control.settle(pending_inputs=False)
    final = await repository.get(identity)
    assert final["id"] == identity and final["state"] == "completed"
    assert final["model_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["model", "management"])
@pytest.mark.parametrize("prior_state", ["failed", "waiting_external"])
async def test_explicit_resume_keeps_identity_and_retires_replaced_wait(
    database, tmp_path, entry, prior_state
):
    from qq_ai_bot.runtime.work_management import manage_work

    repository, lease, control = await _running_work(database, tmp_path)
    identity = control.current["id"]
    if prior_state == "failed":
        await control.recover_failure(RuntimeError("original execution failed"))
    else:
        assert json.loads(
            await control.execute(
                "task_control",
                {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 300}]},
                "original-timer",
            )
        )["ok"]
        await control.settle(pending_inputs=False)
    original = await repository.get(identity)
    assert original["state"] == prior_state
    await repository.release(lease)
    if entry == "management":
        async with database.immediate_session() as writer:
            await manage_work(writer, identity, original["revision"], "resume")
    else:
        lease = await repository.acquire(lease.conversation_id, lease.generation)
        control = WorkControl(
            repository, lease, "explicit-original-resume", control.source, control.validate
        )

        async def validate():
            assert await repository.valid(lease)

        control.validate = validate
        result = json.loads(
            await control.execute(
                "task_control", {"action": "resume", "work_id": identity}, "resume-original"
            )
        )
        assert result["ok"]
    resumed = await repository.get(identity)
    assert resumed["state"] == "queued"
    assert resumed["source_json"] == original["source_json"]
    assert resumed["model_requests"] == original["model_requests"]
    assert resumed["tool_calls"] == original["tool_calls"]
    waits = WorkWaitRepository(repository)
    if prior_state == "waiting_external":
        binding = await waits.describe(identity)
        assert binding["status"] == "cancelled"
        assert await waits.deliver_due(now=binding["conditions"][0]["due"] + 1) == 0
        assert (await repository.get(identity))["revision"] == resumed["revision"]
    if entry == "model":
        await repository.release(lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "fail", "resume"])
async def test_model_can_manage_a_derived_child_without_replacing_current_work(
    database, tmp_path, action
):
    repository, lease, control = await _running_work(database, tmp_path)
    parent = control.current["id"]
    derived = json.loads(
        await control.execute("task_control", {"action": "derive", "goal": "child goal"}, "derive")
    )
    assert derived["ok"]
    child = derived["derived_work_id"]
    if action == "resume":
        row = await repository.get(child)
        await repository.transition(lease, child, row["revision"], "suspended")
    result = json.loads(
        await control.execute("task_control", {"action": action, "work_id": child}, action)
    )
    assert result["ok"]
    assert result["state"] == {"cancel": "cancelled", "fail": "failed", "resume": "queued"}[action]
    assert control.current["id"] == parent
    assert control.ending is None
    assert (await repository.get(parent))["state"] == "running"
    assert (await repository.get(child))["parent_work_id"] == parent


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["fail", "cancel"])
async def test_model_stop_accepts_live_descendants_and_preserves_unknown_receipt(
    database, tmp_path, action
):
    from qq_ai_bot.runtime.subagent_repository import SubagentRepository
    from qq_ai_bot.runtime.work_schema_v1 import effects

    repository, lease, control = await _running_work(database, tmp_path)
    identity = control.current["id"]
    child = await repository.derive(lease, identity, "original-derived-child", "child goal")
    workers = SubagentRepository(repository)
    worker = await workers.start(lease, child["id"], "original-worker", {"goal": "worker goal"})
    worker_lease = await workers.acquire(worker)
    assert worker_lease and await control.has_owned_execution()
    await _late_input(repository, lease, child["id"], ready=True)
    waits = WorkWaitRepository(repository)
    await waits.register(
        lease,
        work_id=child["id"],
        source=control.source,
        call_key="original-child-timer",
        mode="any",
        conditions=[{"kind": "time_due", "after_seconds": 300}],
        deadline_at=None,
    )
    key = "original-unknown-external-write"
    await repository.prepare_effect(lease, identity, key, "tool")
    await repository.record_effect(
        key, "unknown", {"outcome": {"uncertain": True, "executed": True}, "error": "receipt lost"}
    )
    async with database.sessions() as reader:
        original_effect = dict(
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )
    assert json.loads(await control.execute("task_control", {"action": action}, "stop-tree"))["ok"]
    assert (await repository.get(worker))["state"] == "running"
    assert (await repository.get(identity))["state"] == "running"
    await control.settle(pending_inputs=False)
    assert (await repository.get(identity))["state"] == (
        "failed" if action == "fail" else "cancelled"
    )
    stopped_child = await repository.get(child["id"])
    assert stopped_child["state"] == "cancelled"
    assert (await repository.get(worker))["state"] == "cancelled"
    assert not await repository.valid(worker_lease)
    binding = await waits.describe(child["id"])
    assert binding["status"] == "cancelled"
    assert await waits.deliver_due(now=binding["conditions"][0]["due"] + 1) == 0
    assert await repository.get(child["id"]) == stopped_child
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(inputs.c.state).where(inputs.c.work_id == child["id"]))
            == "cancelled"
        )
        retained = dict(
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )
    assert retained == original_effect


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize(
    "seconds,expired", [(0, False), (-0.5, False), (0.25, False), (31_536_001, False), (60, True)]
)
async def test_existing_timer_can_resolve_all_conditions_without_delay_policy(
    database, tmp_path, seconds, expired, child
):
    from qq_ai_bot.control_plane.paging import PageRequest
    from qq_ai_bot.persistence.control_work_query import ControlWorkQueryAdapter

    repository, lease, control = await _running_work(database, tmp_path)
    if child:
        from qq_ai_bot.runtime.subagent_repository import SubagentRepository

        workers = SubagentRepository(repository)
        identity = await workers.start(
            lease,
            control.current["id"],
            "timer-child",
            {"goal": "wait then answer"},
        )
        lease = await workers.acquire(identity)
        row = await repository.get(identity)

        async def validate():
            assert await repository.valid(lease)

        control = WorkControl(
            repository,
            lease,
            row["source_key"],
            json.loads(row["source_json"]),
            validate,
            current=row,
        )
    conditions = [{"kind": "time_due", "after_seconds": seconds}] * (300 if seconds == 0 else 9)
    if not child and seconds == 0:
        wait_repository = WorkWaitRepository(repository)
        registration = dict(
            work_id=control.current["id"],
            source=control.source,
            call_key="long-source-key-" * 100,
            mode="all",
            conditions=conditions,
            deadline_at=None,
            accepted={"action": "wait", "call_key": "long-source-key-" * 100},
        )
        registered = await wait_repository.register(lease, **registration)
        assert (await wait_repository.register(lease, **registration))["id"] == registered["id"]
        control.current = await repository.get(control.current["id"])
    else:
        result = json.loads(
            await control.execute(
                "task_control",
                {
                    "action": "wait",
                    "conditions": conditions,
                    "wait_mode": "all",
                    **({"deadline_at": "2000-01-01T00:00:00+00:00"} if expired else {}),
                },
                "wait",
            )
        )
        assert result["ok"]
    history = await ControlWorkQueryAdapter(database.sessions).list_work_history(
        PageRequest(),
        work_id=control.current["id"],
        section="waits",
        include_content=True,
    )
    assert len(history.items) == 1
    assert len(history.items[0].fields["conditions"]) == len(conditions)
    await control.settle(pending_inputs=False)
    wait_repository = WorkWaitRepository(repository)
    wait = await wait_repository.describe(control.current["id"])
    assert len(wait["conditions"]) == len(conditions)
    due = wait["registered_at"] if expired else wait["conditions"][0]["due"]
    assert await wait_repository.deliver_due(now=due) == 1
    assert await wait_repository.deliver_due(now=due) == 0
    resumed = await repository.get(control.current["id"])
    assert resumed["state"] == "queued"
    assert len(await repository.pending(lease, work_id=resumed["id"])) == 1
    assert (await wait_repository.describe(resumed["id"]))["status"] == (
        "expired" if expired else "delivered"
    )
    if child:
        await repository.release(lease)
        lease = await workers.acquire(resumed["id"])
        control = WorkControl(
            repository,
            lease,
            resumed["source_key"],
            json.loads(resumed["source_json"]),
            validate,
            current=await repository.get(resumed["id"]),
        )
        assert len(await control.take_inputs("timer-wakeup")) == 1
        await control.confirm_inputs()
        result = json.loads(
            await control.execute(
                "task_control",
                {"action": "complete", "result": "timer woke and task completed"},
                "done",
            )
        )
        assert result["ok"]
        await control.settle(pending_inputs=False)
        await workers.finish(lease)
        assert (await repository.get(resumed["id"]))["state"] == "completed"
        delivered = await workers.related(resumed["parent_work_id"], resumed["id"])
        assert json.loads(delivered["result_json"])["text"] == "timer woke and task completed"
    for invalid in (True, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="invalid_wait_delay"):
            normalize_conditions([{"kind": "time_due", "after_seconds": invalid}], due)


@pytest.mark.asyncio
async def test_retained_work_and_pending_inputs_do_not_block_new_intents(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    original = control.current["id"]
    source = control.source
    created = []
    for index in range(129):
        created.append(
            await repository.accept(
                lease,
                source_key=f"independent-{index}",
                source=source,
                goal=f"task {index}",
                initial_state="queued",
            )
        )
        await repository.enqueue(
            lease.conversation_id,
            lease.generation,
            f"input-{index}",
            kind="message",
            work_id=original,
            ready=False,
        )
    replay = await repository.accept(
        lease, source_key="independent-128", source=source, goal="task 128"
    )
    assert replay["id"] == created[-1]["id"]
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(work)) == 130
        assert (
            await session.scalar(
                select(func.count()).select_from(inputs).where(inputs.c.work_id == original)
            )
            == 129
        )
    # A prompt excerpt cannot deny a known original Work beyond its first page.
    target = created[-1]
    control.current = None
    control.source_key = "resume-known-original"
    assert len(await control.available_work()) == 16
    assert target["id"] not in {row["work_id"] for row in await control.available_work()}
    resumed = json.loads(
        await control.execute(
            "task_control", {"action": "resume", "work_id": target["id"]}, "resume-last"
        )
    )
    assert resumed["resumed_work_id"] == target["id"]
    assert (await repository.get(target["id"]))["state"] == "queued"
    assert (await repository.get(target["id"]))["model_requests"] == target["model_requests"]
    foreign = await repository.accept(
        lease,
        source_key="foreign-source",
        source={**source, "actor_person_id": "foreign-person"},
        goal="different owner's task",
        initial_state="queued",
    )
    from qq_ai_bot.runtime.work_queries import WorkQueries

    # The global read directory is still readable, but doesn't grant mutation authority.
    assert await WorkQueries(repository).get(lease, source, foreign["id"]) is not None
    refused = json.loads(
        await control.execute(
            "task_control", {"action": "resume", "work_id": foreign["id"]}, "foreign-resume"
        )
    )
    assert refused["error"] == "resume_work_not_authorized"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
@pytest.mark.parametrize("mode,shorthand", [("any", False), ("all", False), ("any", True)])
async def test_wait_conditions_follow_mode_with_failed_member(
    database, tmp_path, terminal, mode, shorthand
):
    from qq_ai_bot.runtime.subagent_repository import SubagentRepository

    repository, lease, control = await _running_work(database, tmp_path)
    workers = SubagentRepository(repository)
    branch = await repository.derive(
        lease, control.current["id"], "wait-branch", "wait for the actual nested result"
    )
    child = await workers.start(lease, branch["id"], "failed-member", {"goal": "work"})
    child_lease = await workers.acquire(child)
    row = await repository.get(child)
    await repository.transition(child_lease, child, row["revision"], terminal)
    await repository.release(child_lease)
    conditions = [
        {"kind": "owned_run", "run_id": child},
        {"kind": "time_due", "after_seconds": 3600},
    ]
    assert json.loads(
        await control.execute(
            "task_control",
            {"action": "wait", "run_id": child}
            if shorthand
            else {"action": "wait", "conditions": conditions, "wait_mode": mode},
            "wait-all",
        )
    )["ok"]
    await control.settle(pending_inputs=False)
    waits = WorkWaitRepository(repository)
    registered = await waits.describe(control.current["id"])
    assert await waits.deliver_due(now=registered["registered_at"] + 1) == (
        1 if mode == "any" else 0
    )
    observed = await waits.describe(control.current["id"])
    assert observed["conditions"][0]["matched"]["status"] == terminal
    if not shorthand:
        assert observed["conditions"][1]["matched"] is None
    if mode == "all":
        assert observed["status"] == "active"
        assert (await repository.get(control.current["id"]))["state"] == "waiting_external"
        assert await waits.deliver_due(now=observed["conditions"][1]["due"]) == 1
        observed = await waits.describe(control.current["id"])
        assert all(condition["matched"] is not None for condition in observed["conditions"])
    assert observed["status"] == "delivered"
    pending = await repository.pending(lease, work_id=control.current["id"])
    signal = json.loads(json.loads(pending[0]["payload_json"])["text"])
    assert signal["conditions"][0]["matched"]["status"] == terminal
    assert await waits.deliver_due(now=registered["registered_at"] + 7200) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["uncertain", "unknown"])
async def test_wait_shorthand_keeps_long_owned_run_id_and_completed_uncertainty(
    database, tmp_path, terminal
):
    from datetime import UTC, datetime

    from jsonschema import validate

    from qq_ai_bot.runtime.work_control import work_control_tools
    from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository

    repository, lease, control = await _running_work(database, tmp_path)
    identity = control.current["id"]
    branch = await repository.derive(lease, identity, "sandbox-branch", "retain original execution")
    run_id = "manager/original/" + "opaque." * 40
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            SandboxTaskRunModel(
                request_id="original-paid-terminal-call",
                source_conversation_id=lease.conversation_id,
                source_json=json.dumps({"work_id": branch["id"]}),
                payload_hash="0" * 64,
                run_id=run_id,
                status="completed",
                completion_json=json.dumps({"status": terminal, "pending": False}),
                created_at=now,
                updated_at=now,
            )
        )
    tasks = SandboxTaskRepository(database)
    args = {"action": "wait", "run_id": run_id}
    validate(args, work_control_tools()[0].parameters)
    result = json.loads(await control.execute("task_control", args, "wait-original-run"))
    assert result["ok"]
    await control.settle(pending_inputs=False)
    waits = WorkWaitRepository(repository)
    registered = await waits.describe(identity)
    assert registered["conditions"][0]["run_id"] == run_id
    assert await waits.deliver_due(now=registered["registered_at"] + 1) == 1
    signal = json.loads(json.loads((await control.pending())[0]["payload_json"])["text"])
    assert signal["conditions"][0]["matched"]["status"] == terminal
    assert signal["conditions"][0]["run_id"] == run_id
    assert (await tasks.by_run(run_id)).completion_json == json.dumps(
        {"status": terminal, "pending": False}
    )


async def test_batched_waits_keep_owner_tuple_and_sandbox_self_scope(database, tmp_path):
    from datetime import UTC, datetime

    from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel

    repository, lease, control = await _running_work(database, tmp_path)
    owner = control.current["id"]
    other = await repository.accept(
        lease, source_key="other-root", source=control.source, goal="another real root"
    )
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            SandboxTaskRunModel(
                request_id="same-work-original-run",
                source_conversation_id=lease.conversation_id,
                source_json=json.dumps({"work_id": owner}),
                payload_hash="0" * 64,
                run_id=owner,
                status="completed",
                completion_json=json.dumps({"status": "uncertain"}),
                created_at=now,
                updated_at=now,
            )
        )
    waits = WorkWaitRepository(repository)
    for identity in (owner, other["id"]):
        await waits.register(
            lease,
            work_id=identity,
            source=control.source,
            call_key=f"tuple-wait:{identity}",
            mode="any",
            conditions=[{"kind": "owned_run", "run_id": owner}],
            deadline_at=None,
        )
    assert await waits.deliver_due() == 1
    matched = await waits.describe(owner)
    assert matched["conditions"][0]["matched"]["status"] == "uncertain"
    assert (await waits.describe(other["id"]))["status"] == "active"
    assert (await waits.describe(other["id"]))["conditions"][0]["matched"] is None


async def test_abandoned_batch_repairs_original_inputs_and_advances_work_once(database, tmp_path):
    import time
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import update

    from qq_ai_bot.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
    from qq_ai_bot.identity.db_models import PresenceModel
    from qq_ai_bot.persistence.models import ChatEventModel

    repository, lease, control = await _running_work(database, tmp_path)
    identity = control.current["id"]
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        presence = await writer.scalar(
            select(PresenceModel).where(PresenceModel.external_account_id == "80001")
        )
        event_ids = []
        for index in range(2):
            event = ChatEventModel(
                bot_user_id="80001",
                platform_message_id=f"repair-{index}",
                scope_type="group",
                group_id="20001",
                sender_user_id="10001",
                direction="inbound",
                content="original text" * 1000,
                occurred_at=now,
                observed_at=now,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=lease.conversation_id,
                author_kind="person",
                author_person_id=control.source["actor_person_id"],
                ingress_presence_id=presence.id,
                suppression_status="keeper",
            )
            writer.add(event)
            await writer.flush()
            event_ids.append(event.id)
    repaired = [
        await repository.enqueue(
            lease.conversation_id,
            lease.generation,
            f"repair:{event_id}",
            kind="message",
            event_id=event_id,
            work_id=identity,
            ready=False,
        )
        for event_id in event_ids
    ]
    missing = await repository.enqueue(
        lease.conversation_id,
        lease.generation,
        "repair:missing",
        kind="message",
        work_id=identity,
        ready=False,
    )
    live = await repository.enqueue(
        lease.conversation_id,
        lease.generation,
        "repair:live",
        kind="message",
        event_id=event_ids[0],
        work_id=identity,
        ready=False,
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(inputs)
            .where(inputs.c.id.in_([*repaired, missing]))
            .values(prepare_owner="abandoned-process")
        )
        await writer.execute(
            update(inputs)
            .where(inputs.c.id == live)
            .values(prepare_owner="current-process", created=time.time() - 1000)
        )
        await writer.execute(
            update(work).where(work.c.id == identity).values(state="waiting_external")
        )
    await repository.repair_abandoned_inputs("current-process")
    async with database.sessions() as reader:
        rows = {row["id"]: row for row in (await reader.execute(select(inputs))).mappings()}
        admissions = list((await reader.scalars(select(OrdinaryTurnAdmissionModel))).all())
    for input_id in repaired:
        assert rows[input_id]["ready"]
        assert json.loads(rows[input_id]["payload_json"])["text"].endswith("original text" * 1000)
    assert {(a.event_id, a.input_id, a.work_id) for a in admissions} == set(
        zip(event_ids, repaired, [identity] * 2, strict=True)
    )
    assert rows[missing]["state"] == "cancelled"
    assert not rows[live]["ready"] and rows[live]["state"] == "pending"
    after = await repository.get(identity)
    assert after["state"] == "queued"
    await repository.repair_abandoned_inputs("current-process")
    assert await repository.get(identity) == after


async def test_due_wait_after_unchanged_first_page_delivers_to_original_work(
    database, tmp_path, monkeypatch
):
    from uuid import UUID

    from sqlalchemy import update

    from qq_ai_bot.runtime import work_wait
    from qq_ai_bot.runtime.work_wait_schema import waits

    repository, lease, control = await _running_work(database, tmp_path)
    wait_ids = iter(UUID(int=index + 1, version=4) for index in range(129))
    monkeypatch.setattr(work_wait, "uuid4", lambda: next(wait_ids))
    waiting_work = []
    for index in range(129):
        row = await repository.accept(
            lease,
            source_key=f"paged-wait:{index}",
            source=control.source,
            goal=f"continue original work {index}",
            initial_state="queued",
        )
        binding = await WorkWaitRepository(repository).register(
            lease,
            work_id=row["id"],
            source=control.source,
            call_key=f"paged-wait-call:{index}",
            mode="any",
            conditions=[{"kind": "time_due", "after_seconds": 0 if index == 128 else 86400}],
            deadline_at=None,
        )
        waiting_work.append(row["id"])
    tail_work = waiting_work[-1]
    async with database.immediate_session() as writer:
        # Equal creation times exercise the ID component of the page cursor.
        await writer.execute(update(waits).values(created=0))
        await writer.execute(
            update(work).where(work.c.id.in_(waiting_work)).values(state="waiting_external")
        )
    due = binding["conditions"][0]["due"]
    reader = WorkWaitRepository(repository)
    assert await reader.deliver_due(now=due) == 1
    async with database.sessions() as session:
        bindings = (
            (await session.execute(select(waits).order_by(waits.c.created, waits.c.id)))
            .mappings()
            .all()
        )
        states = dict(
            (
                await session.execute(
                    select(work.c.id, work.c.state).where(work.c.id.in_(waiting_work))
                )
            )
            .tuples()
            .all()
        )
    assert [row["work_id"] for row in bindings] == waiting_work
    assert all(row["status"] == "active" for row in bindings[:-1])
    assert all(json.loads(row["conditions_json"])[0]["matched"] is None for row in bindings[:-1])
    assert bindings[-1]["status"] == "delivered"
    assert states[tail_work] == "queued"
    assert all(states[identity] == "waiting_external" for identity in waiting_work[:-1])
    pending = await repository.pending(lease, work_id=tail_work)
    assert len(pending) == 1 and pending[0]["ready"]
    signal = json.loads(json.loads(pending[0]["payload_json"])["text"])
    assert signal["wait_id"] == binding["id"] and signal["reason"] == "signal"
    assert signal["conditions"][0]["matched"]["due"] == due
    assert await reader.deliver_due(now=due) == 0
    assert await repository.pending(lease, work_id=tail_work) == pending


@pytest.mark.parametrize("discard_first", [False, True])
@pytest.mark.parametrize("action", ["complete", "fail", None])
async def test_preparing_discard_keeps_original_control_and_unparks(
    database, tmp_path, discard_first, action
):
    repository, lease, control = await _running_work(database, tmp_path)
    if action:
        assert json.loads(
            await control.execute(
                "task_control",
                {"action": action, "result": "original answer"}
                if action == "complete"
                else {"action": action, "reason": "original failure"},
                "end",
            )
        )["ok"]
    identity = control.current["id"]
    input_id = await repository.enqueue(
        lease.conversation_id,
        lease.generation,
        "preparing",
        kind="message",
        work_id=identity,
        ready=False,
    )
    control.ending = "work_input_preparing"
    if discard_first:
        await repository.discard_input(input_id)
    await control.settle(pending_inputs=False)
    if not discard_first:
        row = await repository.get(identity)
        assert row["state"] == "waiting_external" and row["reason"] == "work_input_preparing"
        if action:
            assert json.loads(row["checkpoint_json"])["accepted_control"]["action"] == action
        await repository.discard_input(input_id)
        if action:
            control.current = await repository.get(identity)
            control.settled = False
            control.ending = None
            await control.settle(pending_inputs=False)
    row = await repository.get(identity)
    assert row["state"] == ({"complete": "completed", "fail": "failed"}.get(action, "queued"))
    if action == "complete":
        assert json.loads(row["checkpoint_json"])["sync_result"] == "original answer"


async def test_fulfilled_wait_before_first_settlement_retires_original_decision(database, tmp_path):
    repository, _lease, control = await _running_work(database, tmp_path)
    assert json.loads(
        await control.execute(
            "task_control",
            {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 0}]},
            "wait",
        )
    )["ok"]
    wait = await WorkWaitRepository(repository).describe(control.current["id"])
    assert await WorkWaitRepository(repository).deliver_due(now=wait["conditions"][0]["due"]) == 1
    await control.settle(pending_inputs=False)
    assert control.current["state"] == "queued" and control.accepted is None
    messages = await control.take_inputs("signal", observed_event_ids=frozenset())
    assert len(messages) == 1 and wait["wait_id"] in messages[0].content


async def test_ordinary_publication_failure_rolls_back_ready_admission_and_wake(database, tmp_path):
    from sqlalchemy import update

    from qq_ai_bot.conversation.ordinary_admission import OrdinaryAdmissionRepository
    from qq_ai_bot.persistence.event_repository import EventLedgerRepository
    from qq_ai_bot.persistence.models import ChatEventModel

    repository, lease, control = await _running_work(database, tmp_path)
    identity = control.current["id"]
    async with database.sessions() as reader:
        event_id = await reader.scalar(select(ChatEventModel.id))
    event = await EventLedgerRepository(database).get_event(event_id)
    ordinary = OrdinaryAdmissionRepository(database)
    prepared = ordinary.prepare(event, lease.generation, 0)
    input_id = await repository.enqueue(
        lease.conversation_id,
        lease.generation,
        "atomic-publish",
        kind="message",
        work_id=identity,
        event_id=event_id,
        ready=False,
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == identity)
            .values(state="waiting_external", reason="work_input_preparing")
        )
    publish = ordinary.publisher(prepared)

    async def failed_publish(writer, selected):
        await publish(writer, selected)
        raise RuntimeError("synthetic publication interruption")

    with pytest.raises(RuntimeError, match="synthetic publication interruption"):
        await repository.prepare_input(
            input_id, {"text": "original"}, before_publish=failed_publish
        )
    async with database.sessions() as reader:
        assert not await reader.scalar(select(inputs.c.ready).where(inputs.c.id == input_id))
    assert await ordinary.get(event_id) is None
    assert (await repository.get(identity))["state"] == "waiting_external"
    assert await repository.prepare_input(input_id, {"text": "original"}, before_publish=publish)
    assert (await ordinary.get(event_id)).input_id == input_id
    assert (await repository.get(identity))["state"] == "queued"
    await repository.discard_input(input_id)
    async with database.sessions() as reader:
        row = (await reader.execute(select(inputs).where(inputs.c.id == input_id))).mappings().one()
        assert row["ready"] and row["state"] == "pending"


async def test_late_conversation_delivery_keeps_its_id_and_all_readable_signal_refs(
    database, tmp_path
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.plugin_host.db_models import (
        PluginBackgroundTargetGrantModel,
        PluginInstallationModel,
    )

    repository, lease, control = await _running_work(database, tmp_path)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        original = await writer.scalar(select(ChatEventModel))
        values = {
            column.name: getattr(original, column.name)
            for column in ChatEventModel.__table__.columns
            if column.name != "id"
        }
        conversation = await writer.get(CanonicalConversationModel, lease.conversation_id)
        writer.add(
            PluginInstallationModel(
                plugin_id="synthetic-wait",
                name="synthetic",
                version="1",
                plugin_api="2.0",
                yuki_requires="*",
                manifest_hash="0" * 64,
                entrypoint="unused",
                status="approved",
                enabled=True,
                discovered_at=now,
                updated_at=now,
            )
        )
        await writer.flush()
        writer.add(
            PluginBackgroundTargetGrantModel(
                plugin_id="synthetic-wait",
                target_type="group",
                target_id="20001",
                bot_user_id="80001",
                enabled=True,
                created_by_user_id="10001",
                canonical_target_space_id=conversation.space_id,
                canonical_created_by_person_id=original.author_person_id,
                canonical_presence_id=original.ingress_presence_id,
                created_at=now,
                updated_at=now,
            )
        )
    wait = WorkWaitRepository(repository)
    registered = await wait.register(
        lease,
        work_id=control.current["id"],
        source=control.source,
        call_key="late-message-original-wait",
        mode="all",
        conditions=[
            {"kind": "conversation"},
            {"kind": "plugin_event", "plugin_id": "synthetic-wait", "event_type": "done"},
        ],
        deadline_at=None,
    )
    async with database.immediate_session() as writer:
        late = ChatEventModel(
            **{
                **values,
                "canonical_event_id": str(uuid4()),
                "platform_message_id": "late-human",
                "content": "late human body",
            }
        )
        writer.add(late)
        await writer.flush()
        late_id = late.id
        early = ChatEventModel(
            **{
                **values,
                "canonical_event_id": str(uuid4()),
                "platform_message_id": "early-plugin",
                "content": "early plugin body",
                "event_kind": "external_event",
                "direction": "external",
                "author_kind": "system",
                "author_person_id": None,
                "origin": "plugin_background",
                "source_plugin_id": "synthetic-wait",
                "external_source": "synthetic",
                "external_event_key": "done-1",
                "external_event_type": "done",
                "external_payload_json": "{}",
                "external_target_id": "20001",
                "external_resume_wait": True,
            }
        )
        writer.add(early)
        await writer.flush()
        early_id = early.id
    assert late_id < early_id
    assert await wait.match_event(event_id=early_id, kind="plugin_event") == 0
    selected = await wait.match_event(event_id=late_id, kind="conversation")
    assert selected > 0
    pending = await repository.pending(lease, work_id=control.current["id"])
    assert pending[0]["event_id"] == late_id
    messages = await control.take_inputs("actual-signal", observed_event_ids=frozenset({late_id}))
    assert len(messages) == 1
    signal = json.loads(messages[0].content.split("\n", 1)[1])
    assert signal["wait_id"] == registered["id"] and signal["reason"] == "signal"
    assert {c["matched"]["event_id"] for c in signal["conditions"]} == {late_id, early_id}
    assert {e["text"] for e in signal["events"]} == {"late human body", "early plugin body"}
