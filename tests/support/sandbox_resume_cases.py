"""Actual resume composition, grouped claims, bounded usage, and receipt delivery."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.sandbox.continuation_worker import SandboxContinuationWorker
from qq_ai_bot.sandbox.progress import TaskProgress
from tests.conftest import build_harness, make_settings


async def resume_cases(database, env, tasks, source):
    provider = FakeLLMProvider(lambda _: "下载完成")
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    app = SimpleNamespace(
        database=database,
        sandbox_tasks=tasks,
        ledger=harness.ledger,
        presence_router=env.router,
        runtime_config=chat._runtime_config,
        conversation_scopes=chat._conversation_scopes,
        turn_coordinator=chat._turn_coordinator,
        chat=chat,
    )
    worker = SandboxContinuationWorker(app)
    progress = TaskProgress(5, 6, max_messages=3)
    for name in ("resume-a", "resume-b"):
        await tasks.prepare(name, {"code": "print(1)"}, source)
        await progress.bind(tasks, name)
        run = str(uuid4())
        await tasks.receive(
            {
                "request_id": name,
                "run_id": run,
                "result": {
                    "run_id": run,
                    "status": "succeeded",
                    "pending": False,
                    "output": "done",
                },
            }
        )
    await progress.checkpoint(models=2, tools=2)
    # The source has not finished: no continuation may race its own final answer.
    assert await worker.repository.claim_group("resume-a") is None
    await progress.finish("yielded")
    claims = await asyncio.gather(
        worker.repository.claim_group("resume-a"), worker.repository.claim_group("resume-b")
    )
    assert sum(item is not None for item in claims) == 1
    token, ids = next(item for item in claims if item is not None)
    for name in ids:
        await worker.repository.settle(name, token, state="ready", reason="test_release")
    from qq_ai_bot.domain.conversations import ConversationScope

    await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="after-sandbox-source",
        sender_user_id="10001",
        direction="inbound",
        content="latest-sandbox-context",
    )
    await worker._drain_request("resume-a")
    assert (await worker.repository.get("resume-a")).state == "finished"
    assert (await worker.repository.get("resume-b")).state == "finished"
    assert len(provider.requests) == 1
    assert "latest-sandbox-context" in str(provider.requests[0].messages)
    assert "sandbox_completion" in provider.requests[0].messages[-1].content
    assert env.bot.calls[-1][0] == "send_group_msg"
    assert env.bot.calls[-1][1]["message"] == [{"type": "text", "data": {"text": "下载完成"}}]
    stored = json.loads((await tasks.get("resume-a")).progress_json)
    assert stored["models_used"] == 3 and stored["tools_used"] == 2
    assert stored["messages_used"] == 1
    await worker._drain_request("resume-b")
    assert len(provider.requests) == 1
    async with database.sessions() as session:
        sent = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.content == "下载完成", ChatEventModel.direction == "outbound"
            )
        )
        assert sent is not None and sent.caused_by_event_id is not None
