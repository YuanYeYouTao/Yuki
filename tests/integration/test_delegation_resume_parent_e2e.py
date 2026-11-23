"""#709: another Work resumes a delegation; the original parent never stays waiting.

Failure cases, written before the fix:

R1  The original parent P accepted complete and is waiting on its delegation D's run
    (``waiting_external/work_owned_execution_pending``); another Work W resumes D. For
    each order of "the resumed run ends" and "P settles again" (run first, P first,
    both at once), and for a P that had already completed:
    - P still receives the resumed run's result notices (it must receive none);
    - P is held by a run it did not start (it must end ``completed``);
    - W cannot wait for the run it resumed (``waiting_requires_owned_execution``) and
      never receives its result (it must, and its wait must be delivered);
    - a run of D stays ``waiting`` in ``sandbox_task_runs`` (none may).
R2  P is deferred on D's run and another Work releases D: the run ends with no result
    notice, so nothing re-settles P (P must end ``completed``).
R3  P is deferred on a Manager run of a child Work that already failed; the run's
    completion goes to that terminal child, so nothing re-settles P (P must end
    ``completed``).

Review round 1 (written before the change that answers it):
R4  P is still live and waits on ``run=D``; D's run ends and another Work resumes D
    before the wait is delivered. Moving D away strands P's wait: P must keep D and
    its wait must be delivered once the resumed run ends.
R5  The resume's dispatch raises before any operation is registered: D must keep its
    state and its parent.
R6  After ``/ai new`` a Work of the next generation resumes a record of the previous
    one: the record must keep its parent (Work trees stay in one generation, as at
    creation).

Real TLS device WebSocket, real SQLite, the real Work control tools and writers; the
native peer is the bridge suite's protocol double and no model is called. P's later
activation is the writer call its scheduler activation makes for an accepted complete
(``transition(..., "completed")``), repeated while P is queued. Each case writes an
evidence JSON (``YUKI_709_ARTIFACT_DIR`` or the test's tmp dir) with every Work, input
and run row it read, so a run is checkable and repeatable.
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    settled,
)
from tests.integration.test_external_agent_bridge import bridge as bridge

from yuki.conversation.hydrate import bump_canonical_generation
from yuki.external_agents.db_models import AgentOperationModel
from yuki.persistence.models import ChatEventModel
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.sandbox.task_repository import SandboxTaskRepository

DEFERRED = ("waiting_external", "work_owned_execution_pending")


@pytest.fixture
def evidence(tmp_path, request):
    root = request.config.rootpath
    directory_path = Path(os.environ.get("YUKI_709_ARTIFACT_DIR", str(tmp_path)))
    directory_path.mkdir(parents=True, exist_ok=True)
    record = {
        "nodeid": request.node.nodeid,
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "uncommitted_src": subprocess.check_output(
            ["git", "diff", "--stat", "HEAD", "--", "src"], cwd=root, text=True
        ).strip(),
        "steps": [],
    }
    try:
        yield record
    finally:
        name = request.node.name.replace("[", "-").replace("]", "")
        (directory_path / f"{name}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )


async def _snapshot(bridge, evidence, step, ids):
    async with bridge.database.read_session() as reader:
        works = [
            dict(row)
            for row in (
                await reader.execute(
                    select(
                        work.c.id,
                        work.c.parent_work_id,
                        work.c.state,
                        work.c.reason,
                        work.c.revision,
                    ).where(work.c.id.in_(ids))
                )
            ).mappings()
        ]
        mail = [
            dict(row)
            for row in (
                await reader.execute(
                    select(inputs.c.work_id, inputs.c.source_key, inputs.c.kind, inputs.c.state)
                    .where(inputs.c.work_id.in_(ids))
                    .order_by(inputs.c.id)
                )
            ).mappings()
        ]
        runs = [
            dict(row)
            for row in (
                await reader.execute(
                    select(
                        SandboxTaskRunModel.request_id,
                        SandboxTaskRunModel.status,
                        func.json_extract(SandboxTaskRunModel.source_json, "$.work_id").label(
                            "work_id"
                        ),
                    ).where(
                        func.json_extract(SandboxTaskRunModel.source_json, "$.work_id").in_(ids)
                    )
                )
            ).mappings()
        ]
    evidence["steps"].append({"step": step, "works": works, "inputs": mail, "runs": runs})


async def _row(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return (await reader.execute(select(work).where(work.c.id == work_id))).mappings().one()


async def _mail(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return list(
            (
                await reader.execute(
                    select(inputs).where(inputs.c.work_id == work_id).order_by(inputs.c.id)
                )
            ).mappings()
        )


async def _waiting_runs(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return list(
            await reader.scalars(
                select(SandboxTaskRunModel.request_id).where(
                    func.json_extract(SandboxTaskRunModel.source_json, "$.work_id") == work_id,
                    SandboxTaskRunModel.status == "waiting",
                )
            )
        )


async def _prompt(bridge, record_id, skip=()):
    async with bridge.database.read_session() as reader:
        rows = list(
            await reader.scalars(
                select(AgentOperationModel)
                .where(
                    AgentOperationModel.work_id == record_id,
                    AgentOperationModel.action == "prompt",
                    AgentOperationModel.id.not_in(skip),
                )
                .order_by(AgentOperationModel.created_at)
            )
        )
    [operation] = rows
    return operation


async def _delegate(bridge, control, instance, session_id):
    outcome = await control.tool_outcome(
        "start_subtask",
        {"goal": "Fix the fixture", "executor": {"agent": instance}, "session": session_id},
        "call-start",
    )
    assert outcome.ok, outcome
    await settled(bridge)
    identity = outcome.data["id"]
    return identity, await _prompt(bridge, identity)


async def _accept_complete(control):
    """What the parent's activation commits after its accepted ``complete``."""
    repository = control.repository
    identity = control.current["id"]
    await repository.accept_control(
        control.lease, identity, {"action": "complete", "call_key": "done", "result": "done"}
    )
    current = await repository.get(identity)
    return await repository.transition(control.lease, identity, current["revision"], "completed")


async def _activate(bridge, work_id):
    """The scheduler's activation of a queued Work with an accepted complete: it buys no
    model request and commits ``completed`` through the one state writer."""
    repository = WorkRepository(bridge.database)
    conversation = bridge.env.context.conversation_id
    while (current := await repository.get(work_id))["state"] == "queued":
        lease = await repository.acquire(conversation, 1)
        try:
            current = await repository.get(work_id)
            await repository.transition(lease, work_id, current["revision"], "completed")
        finally:
            await repository.release(lease)
    return current


@pytest.mark.parametrize("order", ["run_first", "parent_first", "together", "parent_completed"])
async def test_resumed_delegation_reports_to_its_resumer_and_never_holds_the_parent(
    bridge, order, evidence
):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            parent = control.current["id"]
            record, first = await _delegate(bridge, control, instance, session_id)
            if order == "parent_completed":
                await computer.event(first.id, "completed", {"status": "completed", "text": "1"})
            settled_parent = await _accept_complete(control)
        if order == "parent_completed":
            assert settled_parent["state"] == "completed"
        else:
            # P accepted complete while its own delegation still runs: deferred (§4.3).
            assert (settled_parent["state"], settled_parent["reason"]) == DEFERRED
            await computer.event(first.id, "completed", {"status": "completed", "text": "1"})
            assert (await _row(bridge, parent))["state"] == "queued"
        before = [item["source_key"] for item in await _mail(bridge, parent)]
        await _snapshot(bridge, evidence, "before_resume", [parent, record])

        async with accepted_work(bridge) as resumer:
            other = resumer.current["id"]
            resumed = await resumer.tool_outcome(
                "manage_task", {"action": "resume", "id": record, "text": "continue"}, "resume-1"
            )
            assert resumed.ok, resumed
            await settled(bridge)
            second = await _prompt(bridge, record, skip=[first.id])
            # The resumer waits for the run it started (its own execution now).
            waited = await resumer.tool_outcome("wait", {"for": [{"run": record}]}, "wait-1")
            assert waited.ok, waited
            current = await resumer.repository.get(other)
            await resumer.repository.transition(
                resumer.lease, other, current["revision"], "waiting_external"
            )
        await _snapshot(bridge, evidence, "after_resume", [parent, record, other])

        async def run_ends():
            await computer.event(second.id, "completed", {"status": "completed", "text": "2"})

        if order == "run_first":
            await run_ends()
            await _activate(bridge, parent)
        elif order == "parent_first":
            await _activate(bridge, parent)
            await run_ends()
        else:
            await asyncio.gather(_activate(bridge, parent), run_ends())
        await _activate(bridge, parent)
        delivered = await WorkWaitRepository(WorkRepository(bridge.database)).deliver_due()
        await _snapshot(bridge, evidence, "settled", [parent, record, other])

        record_row = await _row(bridge, record)
        assert record_row["state"] == "completed"
        assert record_row["parent_work_id"] == other
        parent_row = await _row(bridge, parent)
        assert parent_row["state"] == "completed", dict(parent_row)
        # The original parent got nothing from a run it did not start.
        assert [item["source_key"] for item in await _mail(bridge, parent)] == before
        results = [
            item
            for item in await _mail(bridge, other)
            if item["source_key"] == f"worker-result:{record}:{record_row['revision']}"
        ]
        assert len(results) == 1
        assert results[0]["conversation_id"] == (await _row(bridge, other))["conversation_id"]
        assert delivered == 1
        assert (await _row(bridge, other))["state"] == "queued"
        assert await _waiting_runs(bridge, record) == []
    finally:
        await computer.close()


async def test_release_by_another_work_lets_the_deferred_parent_complete(bridge, evidence):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            parent = control.current["id"]
            record, _ = await _delegate(bridge, control, instance, session_id)
            settled_parent = await _accept_complete(control)
        assert (settled_parent["state"], settled_parent["reason"]) == DEFERRED
        async with accepted_work(bridge) as other:
            released = await other.tool_outcome(
                "manage_task", {"action": "release", "id": record}, "release-1"
            )
            assert released.ok, released
            await settled(bridge)
        await _snapshot(bridge, evidence, "released", [parent, record])
        assert (await _row(bridge, record))["state"] == "cancelled"
        assert await _waiting_runs(bridge, record) == []
        final = await _activate(bridge, parent)
        await _snapshot(bridge, evidence, "settled", [parent, record])
        assert final["state"] == "completed", dict(final)
    finally:
        await computer.close()


async def test_manager_run_of_a_failed_child_ending_lets_the_deferred_parent_complete(
    bridge, evidence
):
    database = bridge.database
    repository = WorkRepository(database)
    tasks = SandboxTaskRepository(database)
    async with accepted_work(bridge) as control:
        parent = control.current["id"]
        database.subagents_enabled = True
        child = await SubagentRepository(repository).start(
            control.lease, parent, "child-with-run", {"goal": "run the build"}
        )
        request_id = f"run:{uuid4()}"
        run_id = str(uuid4())
        await tasks.prepare(
            request_id,
            {"command": "make"},
            {
                **control.source,
                "conversation_id": control.lease.conversation_id,
                "generation": control.lease.generation,
                "work_id": child,
            },
            run_id=run_id,
        )
        current = await repository.get(child)
        await repository.transition(control.lease, child, current["revision"], "failed")
        assert (await _row(bridge, child))["state"] == "failed"
        settled_parent = await _accept_complete(control)
    assert (settled_parent["state"], settled_parent["reason"]) == DEFERRED
    await tasks.receive(
        {
            "request_id": request_id,
            "run_id": run_id,
            "result": {"run_id": run_id, "status": "cancelled", "pending": False},
        }
    )
    await _snapshot(bridge, evidence, "run_ended", [parent, child])
    final = await _activate(bridge, parent)
    await _snapshot(bridge, evidence, "settled", [parent, child])
    assert final["state"] == "completed", dict(final)


async def test_live_parent_waiting_on_its_delegation_keeps_it_and_gets_its_wait(bridge, evidence):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            parent = control.current["id"]
            record, first = await _delegate(bridge, control, instance, session_id)
            waited = await control.tool_outcome("wait", {"for": [{"run": record}]}, "wait-p")
            assert waited.ok, waited
            current = await control.repository.get(parent)
            await control.repository.transition(
                control.lease, parent, current["revision"], "waiting_external"
            )
        await computer.event(first.id, "completed", {"status": "completed", "text": "1"})
        async with accepted_work(bridge) as resumer:
            resumed = await resumer.tool_outcome(
                "manage_task", {"action": "resume", "id": record, "text": "continue"}, "resume-1"
            )
            assert resumed.ok, resumed
            await settled(bridge)
            second = await _prompt(bridge, record, skip=[first.id])
            refused = await resumer.tool_outcome("wait", {"for": [{"run": record}]}, "wait-w")
            assert refused.error_code == "waiting_requires_owned_execution"
        await _snapshot(bridge, evidence, "after_resume", [parent, record])
        assert (await _row(bridge, record))["parent_work_id"] == parent
        await computer.event(second.id, "completed", {"status": "completed", "text": "2"})
        delivered = await WorkWaitRepository(WorkRepository(bridge.database)).deliver_due()
        await _snapshot(bridge, evidence, "settled", [parent, record])
        assert delivered == 1
        assert (await _row(bridge, parent))["state"] == "queued"
    finally:
        await computer.close()


async def test_resume_refused_before_registration_keeps_state_and_parent(
    bridge, evidence, monkeypatch
):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            parent = control.current["id"]
            record, first = await _delegate(bridge, control, instance, session_id)
            await computer.event(first.id, "completed", {"status": "completed", "text": "1"})
            await _accept_complete(control)
        before = await _row(bridge, record)
        original = bridge.service.execute

        async def refuse(args, request_id, source, **kwargs):
            if args.get("resume"):
                raise ValueError("connector_outdated")
            return await original(args, request_id, source, **kwargs)

        monkeypatch.setattr(bridge.service, "execute", refuse)
        async with accepted_work(bridge) as resumer:
            resumed = await resumer.tool_outcome(
                "manage_task", {"action": "resume", "id": record, "text": "continue"}, "resume-1"
            )
            assert not resumed.ok
        await _snapshot(bridge, evidence, "refused", [parent, record])
        after = await _row(bridge, record)
        assert (after["state"], after["parent_work_id"]) == ("completed", parent)
        assert after["revision"] > before["revision"]
    finally:
        await computer.close()


async def test_next_generation_resume_keeps_the_previous_generation_parent(bridge, evidence):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            parent = control.current["id"]
            record, first = await _delegate(bridge, control, instance, session_id)
            await computer.event(first.id, "completed", {"status": "completed", "text": "1"})
            await _accept_complete(control)
        conversation = bridge.env.context.conversation_id
        async with bridge.database.read_session() as reader:
            event = await reader.scalar(select(ChatEventModel))
        async with bridge.database.immediate_session() as writer:
            generation = await bump_canonical_generation(
                writer, conversation, event_id=event.id, force=True
            )
        repository = WorkRepository(bridge.database)
        lease = await repository.acquire(conversation, generation)

        async def valid():
            assert await repository.valid(lease)

        source = {
            **bridge.source,
            "principal_kind": "person",
            "origin": "user_message",
            "generation": generation,
            "trigger_event_id": event.id,
            "bot_user_id": bridge.env.bot.self_id,
            "presence_id": bridge.env.presence,
        }
        resumer = WorkControl(repository, lease, "external-owner", source, valid)
        resumer.current = await repository.accept(
            lease, source_key=f"external-owner:{uuid4()}", source=source, goal="Continue"
        )
        token = current_work_control.set(resumer)
        try:
            resumed = await resumer.tool_outcome(
                "manage_task", {"action": "resume", "id": record, "text": "continue"}, "resume-1"
            )
            assert resumed.ok, resumed
            await settled(bridge)
        finally:
            current_work_control.reset(token)
            await repository.release(lease)
        await _snapshot(bridge, evidence, "resumed", [parent, record, resumer.current["id"]])
        assert (await _row(bridge, record))["parent_work_id"] == parent
    finally:
        await computer.close()
