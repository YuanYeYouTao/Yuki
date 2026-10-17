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

import qq_ai_bot.client_api.prompts as prompt_service
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.gateway.providers.app.social import AppSocialOperations
from qq_ai_bot.runtime.work_schema_v1 import work


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


async def prepare_resumable_work(env):
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
async def test_cancel_joins_original_scheduler_work_and_flushes_already_claimed_send(
    acp_runtime, monkeypatch, cancel_waiter
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
    owners = await env.app.work_scheduler.dispatch_once()
    assert len(owners) == 1
    cancellation = None
    try:
        async with asyncio.timeout(15):
            await claimed.wait()
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
            assert facts["work"]["state"] == "cancelled"
            assert facts["work"]["active"] is True
            assert not cancellation.done()
            assert not facts["result_event_ids"]
            completions = env.app.runtime.bindings.completions(work_id)
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
            assert finished["work"]["active"] is False
            assert len(finished["result_event_ids"]) == 1
            await asyncio.gather(*owners)
            assert all(completed.done() and not completed.cancelled() for completed in completions)
            assert not env.app.runtime.bindings.completions(work_id)
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
