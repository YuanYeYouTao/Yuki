"""Actual resume composition, grouped claims, bounded usage, and receipt delivery."""

from types import SimpleNamespace
from uuid import uuid4

from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.sandbox.continuation_worker import SandboxContinuationWorker
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
    before_calls = len(env.bot.calls)
    worker = SandboxContinuationWorker(app)
    for name in ("resume-a", "resume-b"):
        await tasks.prepare(name, {"code": "print(1)"}, source)
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
    await worker.drain_once()
    for name in ("resume-a", "resume-b"):
        receipt = await worker.repository.get(name)
        assert receipt.state == "blocked"
        assert receipt.reason == "legacy_continuation_retired"
    await worker.drain_once()
    assert not provider.requests
    assert len(env.bot.calls) == before_calls
