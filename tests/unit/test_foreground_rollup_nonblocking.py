"""A real main turn finishes while background Rollup remains inside its model."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.rollup_test_helpers import model_summary
from tests.unit.test_commands_and_chat import inbound
from tests.unit.test_conversation_rollup_370 import _background_worker

from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.ingress import CanonicalIngressResolver
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import estimate_request_tokens
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProfile, ModelRoute, ModelTask
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


class _BlockedBackgroundProvider(FakeLLMProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.summary_requests = []
        self.main_requests = []
        self.summary_in_flight = False

    async def complete(self, request):
        self.requests.append(request)
        if not request.tools:
            self.summary_requests.append(request)
            self.summary_in_flight = True
            self.entered.set()
            try:
                await self.release.wait()
                return ChatResponse(model_summary(request, "background completed"), 0)
            finally:
                self.summary_in_flight = False
        self.main_requests.append(request)
        if len(self.main_requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "main-send-once",
                        ToolFunction(
                            "send_message", json.dumps({"text": "ready before background"})
                        ),
                    ),
                ),
            )
        return ChatResponse("NO_REPLY", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("work_enabled", [False, True], ids=["ordinary", "work-capable-ordinary"])
async def test_hard_fitting_main_turn_sends_while_background_summary_is_blocked(
    database, tmp_path, work_enabled
):
    env = await social_env(database, tmp_path)
    provider = _BlockedBackgroundProvider()
    settings = make_settings(
        database.url,
        enabled_groups_csv="20001",
        runtime_work_enabled=work_enabled,
        context_window_tokens=524288,
        context_compaction_window_tokens=90000,
    )
    harness = build_harness(database, settings, provider)
    processor, chat = harness.processor, harness.processor._chat
    profile = ModelProfile(
        id="shared-offline",
        provider="fake",
        model="fake-model",
        timeout_seconds=60,
        max_retries=0,
        default_temperature=0.5,
        default_max_output_tokens=8192,
        capabilities=frozenset(
            {
                ModelCapability.REASONING,
                ModelCapability.TOOLS,
                ModelCapability.STRUCTURED_OUTPUT,
                ModelCapability.LONG_CONTEXT,
            }
        ),
    )
    models = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={profile.id: provider}),
        max_concurrency=2,
    )
    chat._models = chat.runtime.runner._models = models
    processor._canonical_ingress = CanonicalIngressResolver(database, env.registry, env.router)
    processor._canonical_uow = CanonicalIngressUnitOfWork(database, env.router)
    chat._tools.social_service = env.service
    state = ShortState(WorkspaceStore(tmp_path / "main-state"))
    chat._tools.short_state = state
    chat.runtime.runner.main_contract = MainAgentContract(chat, state)
    # Match the production soft policy; the generic harness historically uses
    # the hard window for its standalone rollup fixture.
    policy = replace(harness.conversation_rollups.config, context_token_budget=90000)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=chat._models, config=policy, timeout_seconds=60)
    chat._context_assembler._rollups = repository
    chat._context_assembler._rollup_service = service
    processor._conversation_rollups = repository
    scope = ConversationScope.group(env.bot.self_id, "20001")
    markers = []
    for index in range(22):
        marker = f"retained-source-{index:02d}:"
        markers.append(marker)
        await harness.scoped_events.append(
            scope=scope,
            platform_message_id=f"older-{index}",
            sender_user_id="10001",
            direction="inbound",
            content=marker + "x" * 15000,
            occurred_at=datetime(2026, 10, 3, tzinfo=UTC) + timedelta(seconds=index),
        )
    original = await repository.load_prompt_snapshot(scope)
    assert not original.raw_complete  # This cannot pass via a fit partial suffix.
    worker = _background_worker(repository, service)
    worker_task = asyncio.create_task(worker._run("blocked-background"))
    main_task = None
    try:
        await asyncio.wait_for(provider.entered.wait(), 20)
        assert len(provider.summary_requests) == 1
        assert not provider.release.is_set()
        sender = MemorySender()
        sender.bot = env.bot
        message = replace(
            inbound(
                "answer now",
                message_id="foreground-over-soft",
                user_id="10001",
                group_id="20001",
                mentions_bot=True,
            ),
            bot_user_id=env.bot.self_id,
        )
        main_task = asyncio.create_task(processor.handle(message, sender))
        result = await asyncio.wait_for(main_task, 20)
        assert result.reason == "chat", result
        assert not provider.release.is_set() and not worker_task.done()
        assert provider.summary_in_flight
        assert len(provider.summary_requests) == 1  # No new foreground auxiliary call.
        assert len(provider.main_requests) == 2
        request = provider.main_requests[0]
        continued = provider.main_requests[1]
        assert continued.tools == request.tools
        assert continued.messages[: len(request.messages)] == request.messages
        assert [
            message.tool_call_id for message in continued.messages if message.role == "tool"
        ] == ["main-send-once"]
        cost = estimate_request_tokens(request)
        runtime = await chat._runtime_config.snapshot(group_id="20001")
        hard = chat._models.capacity(ModelTask.CHAT_AGENT).input_budget(
            runtime.context.window_tokens, output_tokens=request.max_output_tokens
        )
        assert runtime.context.compaction_window_tokens < cost < hard
        body = "\n".join(message.content or "" for message in request.messages)
        assert all(body.count(marker) == 1 for marker in markers)
        assert body.count("answer now") == 1
        sends = [(name, args) for name, args in env.bot.calls if name == "send_group_msg"]
        assert len(sends) == 1
        assert sends[0][1]["message"] == [
            {"type": "text", "data": {"text": "ready before background"}}
        ]
        assert not sender.messages  # Delivery was the real Social tool path.
        async with database.sessions() as reader:
            assert await reader.scalar(select(func.count()).select_from(work)) == 0
        snapshot = await repository.load_prompt_snapshot(scope, token_budget=hard)
        assert snapshot.raw_complete and snapshot.effective_coverage == 0
        assert all(
            event.content.startswith("retained-source-") for event in snapshot.raw_events[1:23]
        )
    finally:
        worker._stop.set()
        worker._wake.set()
        provider.release.set()
        if main_task is not None and not main_task.done():
            main_task.cancel()
            await asyncio.gather(main_task, return_exceptions=True)
        await asyncio.wait_for(worker_task, 20)
        await models.close()
