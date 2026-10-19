"""Cancellation joins the original scheduler activation's admitted local delivery."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.test_acp_runtime_integration import acp_runtime as acp_runtime
from tests.integration.test_acp_runtime_integration import new_session, selected, settled
from tests.unit.test_client_api import bearer

import yuki.client_api.prompts as prompt_service
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.gateway.providers.app.social import AppSocialOperations
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import work


def scripted(steps):
    responses = iter(steps)

    def respond(_request):
        name, arguments = next(responses)
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
        )

    return respond


async def prepare_resumable_work(env, *, queue_input=True):
    session = await new_session(env)
    request_id = str(uuid4())
    env.provider._responder = scripted(
        [
            ("task_control", {"action": "accept", "goal": "Preserve original ACP cancellation"}),
            ("task_control", {"action": "need_input", "reason": "Wait for the city"}),
        ]
    )
    submitted = await env.http.post(
        "/client/v1/messages",
        headers=selected(env, session),
        json={"request_id": request_id, "text": "Start the original ACP Work"},
    )
    assert submitted.status_code == 200, submitted.text
    original = await settled(env, session, request_id)
    work_id = original["work"]["work_id"]
    assert original["work"]["state"] == "waiting_user"
    if queue_input:
        accepted = await env.http.post(
            f"/client/v1/works/{work_id}/inputs",
            headers=selected(env, session),
            json={"request_id": str(uuid4()), "text": "Use Taipei"},
        )
        assert accepted.status_code == 200, accepted.text
    env.provider._responder = scripted(
        [
            ("send_message", {"text": "Already claimed ACP result"}),
            ("task_control", {"action": "complete", "result": "Done"}),
        ]
    )
    return session, request_id, work_id


@pytest.mark.parametrize("cancel_waiter", [False, True])
@pytest.mark.parametrize("work_kind", ["root", "child", "completed_parent"])
async def test_cancel_joins_original_work_tree_and_flushes_already_claimed_send(
    acp_runtime, database, monkeypatch, cancel_waiter, work_kind
):
    env = acp_runtime
    session, request_id, work_id = await prepare_resumable_work(
        env, queue_input=work_kind != "completed_parent"
    )
    claimed, release = asyncio.Event(), asyncio.Event()
    send = AppSocialOperations.send

    async def wait_after_claim(operations, handle, target, message):
        claimed.set()
        await release.wait()
        return await send(operations, handle, target, message)

    monkeypatch.setattr(AppSocialOperations, "send", wait_after_claim)
    cancelled = asyncio.Event()
    manage, commit = prompt_service.manage_work, AsyncSession.commit

    async def mark_cancel(writer, *args, **kwargs):
        result = await manage(writer, *args, **kwargs)
        writer.info["original_acp_work_cancelled"] = True
        return result

    async def committed(writer):
        await commit(writer)
        if writer.info.pop("original_acp_work_cancelled", False):
            cancelled.set()

    monkeypatch.setattr(prompt_service, "manage_work", mark_cancel)
    monkeypatch.setattr(AsyncSession, "commit", committed)
    execution_id = work_id
    repository = WorkRepository(database)
    if work_kind != "root":
        parent = await repository.get(work_id)
        lease = await repository.acquire(parent["conversation_id"], parent["generation"])
        database.subagents_enabled = True
        execution_id = await SubagentRepository(repository).start(
            lease, work_id, str(uuid4()), {"goal": "Send the original ACP child result"}
        )
        if work_kind == "completed_parent":
            await repository.accept_control(
                lease,
                work_id,
                {"action": "complete", "call_key": "parent-done", "result": "retained"},
            )
            parent = await repository.get(work_id)
            parent = await repository.transition(lease, work_id, parent["revision"], "completed")
        await repository.release(lease)
        await env.app.subagent_execution.prepare()
        owners = [asyncio.create_task(env.app.subagent_execution.run(execution_id))]
    else:
        owners = await env.app.work_scheduler.dispatch_once()
    assert len(owners) == 1
    cancellation = None
    try:
        async with asyncio.timeout(15):
            await claimed.wait()
            if work_kind != "root":
                assert not env.app.runtime.bindings.completions(work_id)
                assert env.app.runtime.bindings.completions(execution_id)
            cancellation = asyncio.create_task(
                env.http.post(
                    f"/client/v1/acp/sessions/{session['session_id']}/requests/{request_id}/cancel",
                    headers=bearer(env.device["token"]),
                )
            )
            await cancelled.wait()
            pending = await env.http.get(
                f"/client/v1/requests/{request_id}", headers=selected(env, session)
            )
            facts = pending.json()["data"]
            assert facts["turn"]["state"] == "ended"
            expected_state = "completed" if work_kind == "completed_parent" else "cancelled"
            assert facts["work"]["state"] == expected_state
            assert facts["work"]["active"] is True
            assert not cancellation.done()
            assert not facts["result_event_ids"]
            completions = env.app.runtime.bindings.completions(execution_id)
            if cancel_waiter:
                cancellation.cancel()
            assert all(not completed.done() for completed in completions)
            release.set()
            if cancel_waiter:
                with pytest.raises(asyncio.CancelledError):
                    await cancellation
                response = await env.http.post(
                    f"/client/v1/acp/sessions/{session['session_id']}/requests/{request_id}/cancel",
                    headers=bearer(env.device["token"]),
                )
            else:
                response = await cancellation
            assert response.status_code == 200, response.text
            finished = response.json()["data"]
            assert finished["cancelled"]
            assert finished["work"]["state"] == expected_state
            assert finished["work"]["active"] is False
            assert len(finished["result_event_ids"]) == 1
            await asyncio.gather(*owners)
            assert all(completed.done() and not completed.cancelled() for completed in completions)
            assert not env.app.runtime.bindings.completions(work_id)
            assert not env.app.runtime.bindings.completions(execution_id)
            assert (await repository.get(execution_id))["state"] == "cancelled"
            if work_kind == "completed_parent":
                unchanged = await repository.get(work_id)
                assert unchanged["state"] == parent["state"]
                assert unchanged["revision"] == parent["revision"]
            repeated = await env.http.post(
                f"/client/v1/acp/sessions/{session['session_id']}/requests/{request_id}/cancel",
                headers=bearer(env.device["token"]),
            )
            assert repeated.json()["data"] == finished
    finally:
        release.set()
        await asyncio.gather(*owners)
        if cancellation is not None and not cancellation.cancelled():
            await cancellation


async def test_cancel_completed_parent_stops_only_its_child_model(
    acp_runtime, database, monkeypatch
):
    env = acp_runtime
    repository = WorkRepository(database)
    prompts = []
    for _ in range(2):
        session, request_id, work_id = await prepare_resumable_work(env, queue_input=False)
        parent = await repository.get(work_id)
        lease = await repository.acquire(parent["conversation_id"], parent["generation"])
        database.subagents_enabled = True
        child_id = await SubagentRepository(repository).start(
            lease, work_id, str(uuid4()), {"goal": "Keep this original child's model separate"}
        )
        await repository.accept_control(
            lease, work_id, {"action": "complete", "call_key": "parent-done", "result": "retained"}
        )
        parent = await repository.get(work_id)
        parent = await repository.transition(lease, work_id, parent["revision"], "completed")
        await repository.release(lease)
        prompts.append((session, request_id, parent, child_id))
    entered = {child_id: asyncio.Event() for _, _, _, child_id in prompts}
    cancelled = {child_id: asyncio.Event() for _, _, _, child_id in prompts}
    release = asyncio.Event()

    async def wait_in_model(request):
        env.provider.requests.append(request)
        child_id = next(identity for identity in entered if identity in str(request.messages))
        entered[child_id].set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled[child_id].set()
            raise
        return ChatResponse(content="Completed the independent child model", latency_seconds=0)

    monkeypatch.setattr(env.provider, "complete", wait_in_model)
    await env.app.subagent_execution.prepare()
    owners = [
        asyncio.create_task(env.app.subagent_execution.run(child_id))
        for _, _, _, child_id in prompts
    ]
    try:
        async with asyncio.timeout(15):
            await asyncio.gather(*(event.wait() for event in entered.values()))
            request_count = len(env.provider.requests)
            session, request_id, parent, child_id = prompts[0]
            response = await env.http.post(
                f"/client/v1/acp/sessions/{session['session_id']}/requests/{request_id}/cancel",
                headers=bearer(env.device["token"]),
            )
            assert response.status_code == 200, response.text
            facts = response.json()["data"]
            assert facts["cancelled"]
            assert facts["work"]["state"] == "completed"
            assert facts["work"]["active"] is False
            assert not facts["result_event_ids"]
            assert cancelled[child_id].is_set()
            assert len(env.provider.requests) == request_count
            assert (await repository.get(child_id))["state"] == "cancelled"
            unchanged = await repository.get(parent["id"])
            assert unchanged["state"] == parent["state"]
            assert unchanged["revision"] == parent["revision"]
            other_session, other_request_id, _, other_child = prompts[1]
            assert not owners[1].done() and not cancelled[other_child].is_set()
            assert (await repository.get(other_child))["state"] == "running"
            other = await env.http.get(
                f"/client/v1/requests/{other_request_id}", headers=selected(env, other_session)
            )
            assert other.json()["data"]["work"]["active"] is True
            assert not release.is_set()
            release.set()
            await asyncio.gather(*owners)
            assert (await repository.get(other_child))["state"] == "completed"
    finally:
        release.set()
        await asyncio.gather(*owners)


async def test_results_use_a_new_snapshot_after_the_original_activation_exits(
    acp_runtime, monkeypatch
):
    env = acp_runtime
    session, request_id, work_id = await prepare_resumable_work(env)
    claimed, release = asyncio.Event(), asyncio.Event()
    send = AppSocialOperations.send

    async def wait_after_claim(operations, handle, target, message):
        claimed.set()
        await release.wait()
        return await send(operations, handle, target, message)

    monkeypatch.setattr(AppSocialOperations, "send", wait_after_claim)
    owners = await env.app.work_scheduler.dispatch_once()
    assert len(owners) == 1
    projection = env.app.client_api.prompts.projection

    async def finish_after_snapshot(*args, **kwargs):
        # sqlite3 legacy SELECTs do not open a read transaction by themselves.
        # Exercise an actual retained WAL snapshot before the send can commit.
        reader = args[0]
        await reader.execute(text("BEGIN"))
        assert await reader.scalar(select(work.c.state).where(work.c.id == work_id)) == "running"
        release.set()
        await asyncio.gather(*owners)
        facts = await projection(*args, **kwargs)
        assert facts["work"]["work_id"] == work_id
        assert facts["work"]["active"] is False
        return facts

    try:
        async with asyncio.timeout(15):
            await claimed.wait()
            monkeypatch.setattr(env.app.client_api.prompts, "projection", finish_after_snapshot)
            response = await env.http.get(
                f"/client/v1/requests/{request_id}", headers=selected(env, session)
            )
            assert response.status_code == 200, response.text
            facts = response.json()["data"]
            assert facts["work"]["active"] is False
            assert len(facts["result_event_ids"]) == 1
    finally:
        release.set()
        await asyncio.gather(*owners)
