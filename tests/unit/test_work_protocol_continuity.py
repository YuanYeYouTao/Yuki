"""Explicit compaction task anchors and exact persisted provider request replay."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, update
from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession, invoke_tool

from qq_ai_bot.admin.models import WorkStorageRuntimeConfig
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ProviderContinuation,
    ReasoningEffort,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.identity.db_models import PresenceModel
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelSearchMode,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.protocol_schema import objects, refs, usage
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import JournalUnavailable, encode_transcript
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from qq_ai_bot.services.native_tool_binder import NativeToolBinder
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.social.models import OperationStatus, SocialTarget
from qq_ai_bot.social.repository import SocialOperationRepository
from qq_ai_bot.web.models import WebMode


async def _control(database, tmp_path, *, worker=False):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "anchor-test", {"trigger_event_id": 1}, validate)
    control.current = await repo.accept(
        lease, source_key="anchor-test", source=control.source, goal="retain the actual task"
    )
    if worker:
        from qq_ai_bot.runtime.subagent_repository import SubagentRepository

        children = SubagentRepository(repo)
        identity = await children.start(
            lease,
            control.current["id"],
            "private-protocol-child",
            {"goal": "retain the actual task"},
        )
        await repo.release(lease)
        child_lease = await children.acquire(identity)
        assert child_lease is not None

        async def validate_child():
            assert await repo.valid(child_lease)

        current = await repo.get(identity)
        control = WorkControl(
            repo,
            child_lease,
            current["source_key"],
            json.loads(current["source_json"]),
            validate_child,
        )
        control.current = current
    return control


async def test_new_work_reuses_prepared_object_marked_for_deletion(database, tmp_path):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    original = ProtocolStore(database)
    value = {"items": [], "private": "same source"}
    digest = await original.put(value)
    async with original.publication(identity) as prepared:
        async with database.immediate_session() as writer:
            await original.publish_refs(writer, identity, prepared)
    async with database.immediate_session() as writer:
        await writer.execute(delete(refs).where(refs.c.work_id == identity))
        await writer.execute(
            update(objects).where(objects.c.sha256 == digest).values(deleting=True)
        )
    current = ProtocolStore(
        database, policy=WorkStorageRuntimeConfig(total_max_bytes=1, object_max_bytes=1)
    )
    assert await current.put(value) == digest
    async with current.publication(identity) as prepared:
        async with database.immediate_session() as writer:
            await current.publish_refs(writer, identity, prepared)
    assert await current.get(digest) == value
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(objects.c.deleting).where(objects.c.sha256 == digest))
            is False
        )
        assert (
            await reader.scalar(select(usage.c.byte_size)) == current._path(digest).stat().st_size
        )
    assert await original.cleanup(grace_seconds=0) == 0


async def test_gc_rechecks_object_reused_before_file_fence(database, tmp_path, monkeypatch):
    import qq_ai_bot.runtime.protocol_store as module

    control = await _control(database, tmp_path)
    identity = control.current["id"]
    original = ProtocolStore(database)
    value = {"original": "paid response"}
    digest = await original.put(value)
    async with original.publication(identity) as prepared:
        async with database.immediate_session() as writer:
            await original.publish_refs(writer, identity, prepared)
    async with database.immediate_session() as writer:
        await writer.execute(delete(refs).where(refs.c.work_id == identity))
    marked = asyncio.Event()
    continue_gc = asyncio.Event()
    original_lock = module.timed_lock
    gc_task = None

    @asynccontextmanager
    async def file_fence(lock, phase):
        if asyncio.current_task() is gc_task:
            marked.set()
            await continue_gc.wait()
        async with original_lock(lock, phase):
            yield

    monkeypatch.setattr(module, "timed_lock", file_fence)
    gc_task = asyncio.create_task(original.cleanup(grace_seconds=0))
    try:
        await asyncio.wait_for(marked.wait(), timeout=5)
        current = ProtocolStore(database)
        assert await current.put(value) == digest
        async with current.publication(identity) as prepared:
            async with database.immediate_session() as writer:
                await current.publish_refs(writer, identity, prepared)
        continue_gc.set()
        assert await gc_task == 0
        assert await current.get(digest) == value
        async with database.sessions() as reader:
            assert (
                await reader.scalar(select(refs.c.sha256).where(refs.c.work_id == identity))
                == digest
            )
    finally:
        continue_gc.set()
        await asyncio.gather(gc_task, return_exceptions=True)


async def test_checkpoint_upgrade_removes_fixed_ceiling_and_preserves_media(tmp_path, monkeypatch):
    path = tmp_path / "deployed.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0103")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO runtime_work_media VALUES (?,?)", ("a" * 64, b"original"))
        db.commit()
        original = db.execute("SELECT * FROM runtime_work_media").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE runtime_checkpoint_quota SET bytes=67108865")
        db.rollback()
    await asyncio.to_thread(command.upgrade, config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM runtime_work_media").fetchall() == original
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'quota_runtime_work_%' "
            "OR name = 'runtime_checkpoint_quota'"
        ).fetchall()
        content = b"x" * 67108865
        db.execute("UPDATE runtime_work_media SET content=?", (content,))
        assert db.execute("SELECT length(content) FROM runtime_work_media").fetchone() == (
            len(content),
        )
        assert {row[1] for row in db.execute("PRAGMA table_info(runtime_delivery_intents)")} == {
            "id",
            "work_id",
            "kind",
            "message_count",
            "state",
            "payload_json",
            "receipt_json",
            "created",
            "updated",
        }
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_changed", [False, True])
async def test_root_resume_uses_current_chat_and_task_material(
    database, tmp_path, contract_changed
):
    control = await _control(database, tmp_path)
    task = ChatMessage(
        "user",
        "original task plus its compiled runtime data",
        images=(ChatImage("data:image/png;base64,YXVkaXQ="),),
    )
    initial = (
        ChatMessage("system", "original fixed contract"),
        ChatMessage("user", "old rollup or historical user message"),
        ChatMessage("assistant", "old response"),
        task,
        ChatMessage("user", "transient work status"),
    )
    first = WorkSession(control, "original")
    transcript = await first.restore(TurnTranscript(initial), compaction_brief=task)
    await control.repository.checkpoint(
        control.lease, control.current["id"], None, models=3, tools=2
    )
    control.current = await control.repository.get(control.current["id"])
    key = first.call_key("original-execution")
    await control.repository.prepare_effect(control.lease, control.current["id"], key, "tool")
    await control.repository.record_effect(
        key,
        "accepted",
        {
            "outcome": {
                "tool": "terminal_exec",
                "run_id": "original-execution",
                "pending": True,
                "uncertain": False,
                "side_effecting": True,
                "ok": True,
            },
            "result": "original execution accepted",
        },
    )
    first.record_search_sources(
        [
            (
                "https://example.org/verified-source",
                "Earlier public source",
                "Public search excerpt from the earlier investigation",
            ),
            ("file:///private/secret", "must not migrate"),
        ]
    )
    await first.save("paired")
    control.current = await control.repository.get(control.current["id"])
    fresh_task = ChatMessage("user", "new wakeup and refreshed runtime data")
    fresh_system = ChatMessage(
        "system", "new fixed contract" if contract_changed else initial[0].content
    )
    resumed = WorkSession(control, "new-contract" if contract_changed else "original")
    restored = await resumed.restore(
        TurnTranscript((fresh_system, fresh_task)), compaction_brief=fresh_task
    )
    assert restored.chain_id != transcript.chain_id
    assert restored.request().messages[:2] == (fresh_system, fresh_task)
    assert all(
        task.content not in (message.content or "") for message in restored.request().messages
    )
    material = json.loads(restored.request().messages[-1].content)
    assert material["goal"] == "retain the actual task"
    assert material["original_request"] == {"event_id": 1, "text": "hello"}
    assert material["execution_evidence"][0]["run_id"] == "original-execution"
    if contract_changed:
        source_message = restored.request().messages[2].content
        assert "https://example.org/verified-source" in source_message
        assert "Public search excerpt from the earlier investigation" in source_message
        assert '"truncated": false' in source_message
        assert "file:///private/secret" not in source_message
    for _ in range(20):
        restored.append(ChatMessage("assistant", "Completed public investigation notes. " * 200))
    for _ in range(16):
        restored.append(ChatMessage("assistant", "Recent completed check."))
    from tests.support.work_compaction import session_summary

    compacted = await resumed.compact(await session_summary(resumed))
    assert compacted.chain_id != restored.chain_id
    assert compacted.request().messages[:2] == (fresh_system, fresh_task)
    summary = json.loads(compacted.request().messages[-1].content)
    assert summary["execution_evidence"][0]["run_id"] == "original-execution"
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 3 and row["tool_calls"] == 2
    again = WorkSession(control, resumed.contract)
    await again.restore(TurnTranscript((fresh_system, fresh_task)), compaction_brief=fresh_task)
    for _ in range(20):
        again.transcript.append(ChatMessage("assistant", "Further completed checks. " * 200))
    for _ in range(16):
        again.transcript.append(ChatMessage("assistant", "Recent completed check."))
    twice = await again.compact(await session_summary(again))
    assert twice.request().messages[:2] == (fresh_system, fresh_task)
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_work_changes_from_deepseek_to_gemini_without_replaying_old_effect(
    database, tmp_path
):
    control = await _control(database, tmp_path)
    capabilities = frozenset({ModelCapability.REASONING, ModelCapability.TOOLS})

    def catalog(profile):
        return ModelProfileCatalog(
            profiles={profile.id: profile},
            routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
        )

    old_profile = ModelProfile(
        id="pro",
        provider="deepseek",
        protocol=ModelProtocol.RESPONSES,
        base_url="https://deepseek.invalid",
        api_key_env="TEST_KEY",
        model="deepseek-chat",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.5,
        default_max_output_tokens=8192,
        capabilities=capabilities,
    )
    models = TaskModelExecutor(router=ModelRouter(catalog(old_profile)), pool=ModelClientPool())
    first_contract = models.profile_revision(ModelTask.CHAT_AGENT)
    task = ChatMessage("user", "Finish the original Work")
    first = WorkSession(control, first_contract)
    previous = await first.restore(
        TurnTranscript((ChatMessage("system", "Old DeepSeek contract"), task)),
        compaction_brief=task,
    )
    previous.accept(
        ProviderContinuation("deepseek", "responses", ({"id": "opaque-old-provider"},), "pro")
    )
    await control.repository.checkpoint(control.lease, control.current["id"], None, models=3)
    control.current = await control.repository.get(control.current["id"])
    old_call = ToolCall("already-done", ToolFunction("workspace_read", "{}"))
    executions = 0

    async def execute_once():
        nonlocal executions
        executions += 1
        return '{"ok":true,"data":{"run_id":"run-fixed","status":"succeeded"}}'

    await invoke_tool(first, old_call, execute_once, side_effecting=False)
    first.record_search_sources(
        [
            (
                "https://example.org/source",
                "Public source",
                "Published public excerpt " + "a" * 530,
            ),
            ("http://localhost/private", "Internal source"),
            ("file:///private/data", "Local file"),
        ]
    )
    # Simulate a crash after the effect was accepted, before its result was paired.
    await first.save("response", (old_call,))
    old_id = control.current["id"]

    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": "continuing"}]},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://gemini.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        gemini = GeminiProvider(
            base_url="https://gemini.invalid",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        new_profile = ModelProfile(
            id="gemini",
            provider="gemini",
            protocol=ModelProtocol.GEMINI,
            base_url="https://gemini.invalid",
            api_key_env="TEST_KEY",
            model="gemini-3.8-flash",
            timeout_seconds=2,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=8192,
            search_mode=ModelSearchMode.NATIVE,
            capabilities=capabilities | {ModelCapability.NATIVE_WEB_SEARCH},
        )
        models.apply_catalog(
            catalog(new_profile), ModelClientPool(injected_profiles={"gemini": gemini})
        )
        next_contract = models.profile_revision(ModelTask.CHAT_AGENT)
        assert next_contract != first_contract
        resumed_control = WorkControl(
            control.repository,
            control.lease,
            control.source_key,
            control.source,
            control.validate,
        )
        resumed_control.current = await control.repository.get(old_id)
        resumed = WorkSession(resumed_control, next_contract)
        resumed_control.session = resumed
        fresh_task = ChatMessage("user", "Fresh wakeup")
        restored = await resumed.restore(
            TurnTranscript((ChatMessage("system", "New Gemini contract"), fresh_task)),
            compaction_brief=fresh_task,
        )
        sequence = restored.request()
        assert sequence.continuation is None and not sequence.items
        assert all(message.response_item is None for message in sequence.messages)
        assert sequence.messages[0].content == "New Gemini contract"
        material = json.loads(sequence.messages[-1].content)
        assert material["goal"] == "retain the actual task"
        assert material["original_request"] == {"event_id": 1, "text": "hello"}
        assert not any(task.content in (message.content or "") for message in sequence.messages)
        assert any("run-fixed" in (message.content or "") for message in sequence.messages)
        assert any(
            "https://example.org/source" in (message.content or "") for message in sequence.messages
        )
        assert any(
            '"truncated": true' in (message.content or "")
            and "Published public excerpt" in (message.content or "")
            for message in sequence.messages
        )
        assert all("localhost" not in (message.content or "") for message in sequence.messages)
        assert resumed_control.known_effects[0]["run_id"] == "run-fixed"
        assert executions == 1
        assert resumed_control.current["id"] == old_id
        assert (
            resumed_control.current["model_requests"],
            resumed_control.current["tool_calls"],
        ) == (3, 1)

        common_tools = (
            ChatTool("workspace_read", "Read workspace", {"type": "object"}),
            ChatTool("web_search", "Search externally", {"type": "object"}),
            ChatTool("read_webpage", "Read page externally", {"type": "object"}),
        )
        binder = NativeToolBinder()
        excluded = binder.excluded_function_names(
            protocol=models.protocol(ModelTask.CHAT_AGENT),
            capabilities=models.capabilities(ModelTask.CHAT_AGENT),
            allowed_capabilities=frozenset({"web"}),
            web_mode=WebMode.NATIVE,
            search_mode=models.search_mode(ModelTask.CHAT_AGENT),
        )
        tools = tuple(tool for tool in common_tools if tool.name not in excluded)
        native = binder.bind(
            protocol=models.protocol(ModelTask.CHAT_AGENT),
            capabilities=models.capabilities(ModelTask.CHAT_AGENT),
            allowed_capabilities=frozenset({"web"}),
            web_mode=WebMode.NATIVE,
            web_was_used=False,
            search_mode=models.search_mode(ModelTask.CHAT_AGENT),
        )
        assert [tool.name for tool in tools] == ["workspace_read"]
        await resumed_control.reserve_request()
        await models.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest(
                messages=sequence.messages,
                continuation=sequence.continuation,
                continuation_items=sequence.items,
                model="gemini-3.8-flash",
                tools=tools,
                native_tools=native,
                thinking_enabled=True,
                reasoning_effort=ReasoningEffort.LOW,
                request_chain_id=restored.chain_id,
            ),
        )
    assert executions == 1 and len(captured) == 1
    payload = captured[0]
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert payload["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "workspace_read",
                    "description": "Read workspace",
                    "parametersJsonSchema": {"type": "object"},
                }
            ]
        },
        {"googleSearch": {}},
    ]
    assert "opaque-old-provider" not in json.dumps(payload)
    assert "run-fixed" in json.dumps(payload)
    current = await control.repository.get(old_id)
    assert (current["model_requests"], current["tool_calls"]) == (4, 1)
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_runner_resumes_gemini_work_on_deepseek_without_old_send_or_native_tool(
    database, tmp_path
):
    control = await _control(database, tmp_path)
    old_id = control.current["id"]
    task = ChatMessage("user", "Finish the original task after checking sources")
    first = WorkSession(control, "gemini-contract")
    transcript = await first.restore(
        TurnTranscript((ChatMessage("system", "old Gemini contract"), task)),
        compaction_brief=task,
    )
    transcript.accept(
        ProviderContinuation(
            "gemini",
            "gemini",
            ({"role": "model", "parts": [{"text": "private", "thoughtSignature": "opaque"}]},),
            "gemini-old",
        )
    )
    sent = 0

    async def confirmed_send():
        nonlocal sent
        sent += 1
        return '{"ok":true,"data":{"status":"succeeded","target":"original"}}'

    await invoke_tool(
        first,
        ToolCall("sent-before-cutover", ToolFunction("send_message", '{"text":"delivered"}')),
        confirmed_send,
    )
    assert control.known_effects[-1]["delivered_message"] is True
    first.record_search_sources(
        [
            ("https://example.org/public", "Verified public source", "Public excerpt"),
            ("http://localhost/private", "Never migrate"),
        ]
    )
    await control.repository.checkpoint(control.lease, old_id, None, models=2)
    control.current = await control.repository.get(old_id)
    await first.save("paired")

    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "response-new",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "call-new",
                        "call_id": "call-new",
                        "name": "workspace_read",
                        "arguments": "{}",
                        "status": "completed",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://deepseek.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        deepseek = DeepSeekResponsesProvider(
            base_url="https://deepseek.invalid",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        profile = ModelProfile(
            id="deepseek-new",
            provider="deepseek",
            protocol=ModelProtocol.RESPONSES,
            base_url="https://deepseek.invalid",
            api_key_env="SYNTHETIC_KEY",
            model="deepseek-flash",
            timeout_seconds=2,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=1024,
            capabilities=frozenset({ModelCapability.REASONING, ModelCapability.TOOLS}),
            search_mode=ModelSearchMode.EXTERNAL,
        )
        catalog = ModelProfileCatalog(
            profiles={profile.id: profile},
            routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
        )
        models = TaskModelExecutor(
            router=ModelRouter(catalog),
            pool=ModelClientPool(injected_profiles={profile.id: deepseek}),
        )
        harness = build_harness(
            database,
            make_settings(
                database.url,
                web_enabled=True,
                web_mode=WebMode.BOTH,
                tavily_api_key="synthetic",
            ),
            FakeLLMProvider(),
        )
        chat = harness.processor._chat
        chat.runtime.runner._models = models
        resumed = WorkControl(
            control.repository, control.lease, control.source_key, control.source, control.validate
        )
        resumed.current = await control.repository.get(old_id)
        common_tools = (
            ChatTool("workspace_read", "Read workspace", {"type": "object"}),
            ChatTool("web_search", "Search externally", {"type": "object"}),
        )
        reads = 0

        class Backend(StubAgentBackend):
            def definitions(self, runtime, **kwargs):
                return common_tools

            def parallel_safe(self, *args):
                return False

            def is_side_effecting(self, *args):
                return False

            async def execute_call(self, invocation):
                nonlocal reads
                reads += 1
                return '{"ok":true,"data":{"read":"current"}}'

        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="1001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="cutover-runner",
            current_group_id=None,
            bot_user_id="9999",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset({"web"}),
            max_tool_calls=8,
            max_model_requests=1,
            fixed_tools=common_tools,
            work_control=resumed,
            compaction_brief=task,
        )
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "new DeepSeek contract"), ChatMessage("user", "wakeup")),
            runtime,
            Backend(),
        )

    assert result.work_state == "queued" and result.suppress_delivery
    assert sent == 1 and reads == 1 and len(captured) == 1
    assert resumed.current["id"] == old_id
    payload = captured[0]
    assert [tool["name"] for tool in payload["tools"]] == ["workspace_read", "web_search"]
    assert "opaque" not in json.dumps(payload)
    assert "https://example.org/public" in json.dumps(payload)
    assert "localhost" not in json.dumps(payload)
    assert "delivered_message" in json.dumps(payload)
    row = await control.repository.get(old_id)
    assert (row["model_requests"], row["tool_calls"]) == (3, 2)
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("last_part", ["succeeded", "missing", "uncertain"])
@pytest.mark.parametrize("boundary", ["contract_changed", "source_changed"])
async def test_provider_change_keeps_original_sequence_unknown_and_allows_new_owned_calls(
    database, tmp_path, last_part, boundary
):
    control = await _control(database, tmp_path)
    task = ChatMessage("user", "Continue this Work without sending again")
    first = WorkSession(control, "deepseek-contract")
    await first.restore(
        TurnTranscript((ChatMessage("system", "DeepSeek"), task)),
        compaction_brief=task,
    )
    call = ToolCall("sequence-call", ToolFunction("send_message", '{"text":"four parts"}'))
    effect_key = first.call_key(call.id)
    assert await control.repository.prepare_effect(
        control.lease, control.current["id"], effect_key, "tool"
    )
    async with database.sessions() as session:
        conversation = await session.get(CanonicalConversationModel, control.lease.conversation_id)
        assert conversation is not None and conversation.space_id is not None
        target = SocialTarget(kind="space", id=conversation.space_id)
        presence_id = await session.scalar(select(PresenceModel.id).limit(1))
        assert presence_id is not None
    receipts = SocialOperationRepository(database)
    source_turn_id = f"{control.lease.conversation_id}:event:1"
    chunks = ("part one", "part two", "part three", "part four")
    await receipts.prepare(
        source_turn_id=source_turn_id,
        tool_call_id=call.id,
        source_conversation_id=control.lease.conversation_id,
        action="send_message_sequence",
        target=target,
        payload={"original": {"text": "four parts"}, "chunks": chunks},
    )
    prefix = hashlib.sha256(call.id.encode()).hexdigest()[:24]
    for index, chunk in enumerate(chunks):
        if index == 3 and last_part == "missing":
            continue
        part = await receipts.prepare(
            source_turn_id=source_turn_id,
            tool_call_id=f"seq:{prefix}:{index}",
            source_conversation_id=control.lease.conversation_id,
            action="send_message",
            target=target,
            payload={"text": chunk},
        )
        assert await receipts.claim(part.operation_id, presence_id=presence_id)
        async with database.sessions() as session, session.begin():
            await receipts.finish(
                part.operation_id,
                status=OperationStatus.UNCERTAIN
                if index == 3 and last_part == "uncertain"
                else OperationStatus.SUCCEEDED,
                session=session,
            )
    await first.save("response", (call,))
    original = await control.repository.get(control.current["id"])
    if boundary == "source_changed":
        async with database.sessions() as session, session.begin():
            conversation = await session.get(
                CanonicalConversationModel, control.lease.conversation_id
            )
            conversation.prompt_source_revision += 1

    async def restart():
        restarted_control = WorkControl(
            control.repository,
            control.lease,
            control.source_key,
            control.source,
            control.validate,
        )
        restarted_control.current = await control.repository.get(control.current["id"])
        resumed = WorkSession(
            restarted_control,
            "gemini-contract" if boundary == "contract_changed" else "deepseek-contract",
        )
        transcript = await resumed.restore(
            TurnTranscript((ChatMessage("system", "Gemini"), ChatMessage("user", "wakeup"))),
            compaction_brief=ChatMessage("user", "wakeup"),
        )
        return restarted_control, resumed, transcript

    invoked = 0

    async def new_operation():
        nonlocal invoked
        invoked += 1
        return '{"ok":true}'

    for _ in range(2):
        restarted_control, resumed, transcript = await restart()
        assert transcript.request().continuation is None
        assert any(
            '"status": "unknown"' in (message.content or "")
            and effect_key in (message.content or "")
            for message in transcript.request().messages
        )
        assert any(effect.get("uncertain") for effect in restarted_control.known_effects)
        original_receipt = json.loads(await resumed.journal.effect_result(effect_key))
        assert original_receipt.get("uncertain") is True
        assert await resumed.journal.effect_state(effect_key) == "prepared"
        allowed = await invoke_tool(
            resumed,
            ToolCall("new-operation", ToolFunction("workspace_write", "{}")),
            new_operation,
            side_effecting=True,
            child_ordinal=0,
        )
        assert json.loads(allowed)["ok"] is True
    assert invoked == 2
    current = await control.repository.get(control.current["id"])
    assert (current["model_requests"], current["tool_calls"]) == (
        original["model_requests"],
        original["tool_calls"] + 2,
    )
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_type", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
async def test_native_checkpoint_replays_exact_http_after_sqlite_restart(
    database, tmp_path, provider_type
):
    control = await _control(database, tmp_path, worker=True)
    task = ChatMessage("user", "original task")
    first = WorkSession(control, "unchanged-profile")
    transcript = await first.restore(
        TurnTranscript((ChatMessage("system", "fixed"), task)), compaction_brief=task
    )
    if provider_type is AnthropicMessagesProvider:
        tail = (
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "original-signature"},
                    {"type": "tool_use", "id": "original-call", "name": "read", "input": {}},
                ],
            },
        )
        answer = {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
    elif provider_type is GeminiProvider:
        tail = (
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "read", "args": {}},
                        "thoughtSignature": "original-signature",
                    }
                ],
                "_call_ids": ["original-call"],
            },
        )
        answer = {
            "candidates": [
                {"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": "done"}]}}
            ]
        }
    else:
        tail = (
            {
                "role": "assistant",
                "content": None,
                "reasoning_details": [
                    {"type": "reasoning.encrypted", "data": "original-signature"}
                ],
                "tool_calls": [
                    {
                        "id": "original-call",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
        )
        answer = {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}
    transcript.accept(
        ProviderContinuation(provider_type.provider_name, provider_type.protocol, tail)
    )
    transcript.append_result("original-call", '{"ok":true,"execution_id":"original-execution"}')
    transcript.append(ChatMessage("user", "redirect after receipt"))
    await control.repository.checkpoint(
        control.lease, control.current["id"], None, models=3, tools=1
    )
    control.current = await control.repository.get(control.current["id"])
    captured = []

    def transport(req):
        captured.append(req.content)
        return httpx.Response(200, json=answer)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider_type(
            base_url="https://wire.invalid/v1/",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )

        async def capture(value):
            sequence = value.request()
            await adapter.complete(
                ChatRequest(
                    messages=sequence.messages,
                    continuation=sequence.continuation,
                    continuation_items=sequence.items,
                    model="thinking-model",
                    tools=(ChatTool("read", "Read", {"type": "object"}),),
                    thinking_enabled=True,
                    max_output_tokens=8192,
                    request_chain_id=value.chain_id,
                )
            )

        await capture(transcript)
        await first.save("paired")
        restored = await WorkSession(control, "unchanged-profile").restore(
            TurnTranscript((ChatMessage("user", "new wakeup"),))
        )
        assert restored.chain_id == transcript.chain_id
        await capture(restored)
    assert captured[0] == captured[1]
    assert b"original-signature" in captured[1] and b"original-execution" in captured[1]
    assert b"new wakeup" not in captured[1]
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 3 and row["tool_calls"] == 1
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_changed", [False, True])
@pytest.mark.parametrize(
    "bad_anchor", [[], encode_transcript(TurnTranscript((ChatMessage("invalid", "invalid role"),)))]
)
async def test_corrupt_compaction_anchor_is_unavailable(
    database, tmp_path, contract_changed, bad_anchor
):
    control = await _control(database, tmp_path, worker=True)
    task = ChatMessage("user", "task")
    first = WorkSession(control, "same")
    await first.restore(TurnTranscript((task,)), compaction_brief=task)
    await first.save("paired")
    async with database.sessions() as session, session.begin():
        raw = await session.scalar(
            select(journal.c.payload_json).where(journal.c.work_id == control.current["id"])
        )
        payload = await first.journal.objects.hydrate(json.loads(raw))
        payload["metadata"]["compaction_anchor"] = bad_anchor
        await session.execute(
            update(journal)
            .where(journal.c.work_id == control.current["id"])
            .values(payload_json=json.dumps(payload))
        )
    if contract_changed:
        resumed = WorkSession(control, "changed")
        refreshed = await resumed.restore(TurnTranscript((task,)), compaction_brief=task)
        assert refreshed.request().messages[0] == task
        assert resumed.compaction_anchor.request().messages == (task,)
    else:
        with pytest.raises(JournalUnavailable, match="compaction_anchor_corrupt"):
            await WorkSession(control, "same").restore(
                TurnTranscript((task,)), compaction_brief=task
            )
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_journal_without_task_anchor_resumes_and_soft_compaction_does_not_block(
    database, tmp_path, monkeypatch
):
    control = await _control(database, tmp_path, worker=True)
    first = WorkSession(control, "same")
    transcript = await first.restore(TurnTranscript((ChatMessage("user", "historical input"),)))
    await first.save("paired")
    resumed = WorkSession(control, "same")
    restored = await resumed.restore(
        TurnTranscript((ChatMessage("user", "fresh wakeup"),)),
        compaction_brief=ChatMessage("user", "fresh wakeup"),
    )
    assert restored.request() == transcript.request()
    with pytest.raises(WorkCapacityError, match="compaction_anchor_unavailable"):
        await resumed.compact("Summary cannot invent the original task")
    assert resumed.transcript is restored
    # A real new static contract takes the caller's explicit child brief, not
    # an inferred historical message. Missing legacy anchors do not veto it.
    actual_brief = ChatMessage("user", control.current["goal"])
    changed = WorkSession(control, "changed-contract")
    await changed.restore(TurnTranscript((actual_brief,)), compaction_brief=actual_brief)
    assert changed.compaction_anchor.request().messages == (actual_brief,)
    original = await WorkSession(control, "same").restore(TurnTranscript(()))
    assert original.request() == transcript.request()
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 0 and row["tool_calls"] == 0
    from tests.support.work_compaction_capacity_helpers import _runtime

    provider = FakeLLMProvider(lambda _request: ChatResponse("valid internal final", 0))
    initial = (ChatMessage("system", "fixed"), ChatMessage("user", "current task"))
    runner, runtime = await _runtime(database, control, initial, provider)
    monkeypatch.setattr(runner, "work_contract", lambda *_args, **_kwargs: "same")
    runtime = replace(
        runtime,
        runtime_config=replace(
            runtime.runtime_config,
            context=replace(runtime.runtime_config.context, work_compaction_trigger_ratio=0.00001),
        ),
    )
    result = await runner.run(
        initial,
        runtime,
        StubAgentBackend(
            definitions=lambda *_args, **_kwargs: (),
            finalize=lambda content, _runtime: content,
        ),
    )
    assert result.text == "valid internal final" and len(provider.requests) == 1
    assert control.accepted_ending() == "completed"
    assert control.session.compaction_anchor is None
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [DeepSeekResponsesProvider, OpenAIResponsesProvider])
async def test_responses_journal_replays_identical_http_bytes(database, tmp_path, provider_type):
    control = await _control(database, tmp_path, worker=True)
    task = ChatMessage("user", "task", images=(ChatImage("data:image/png;base64,YXVkaXQ="),))
    first = WorkSession(control, "same")
    transcript = await first.restore(
        TurnTranscript((ChatMessage("system", "fixed"), task)), compaction_brief=task
    )
    transcript.accept(
        ProviderContinuation(
            provider_type.provider_name,
            "responses",
            (
                {
                    "id": "reason-1",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "synthetic"}],
                },
                {
                    "id": "call-1",
                    "type": "function_call",
                    "call_id": "original-call",
                    "name": "read",
                    "arguments": "{}",
                    "status": "completed",
                },
            ),
        )
    )
    transcript.append_result("original-call", '{"ok":true}')
    captured = []

    def transport(request):
        captured.append(request.content)
        return httpx.Response(
            200,
            json={
                "id": "r-next",
                "status": "completed",
                "output": [
                    {
                        "id": "message-next",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://audit.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = provider_type(
            base_url="https://audit.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )

        async def capture(value):
            sequence = value.request()
            await provider.complete(
                ChatRequest(
                    messages=sequence.messages,
                    continuation=sequence.continuation,
                    continuation_items=sequence.items,
                    model="synthetic",
                    request_chain_id=value.chain_id,
                    tools=(
                        ChatTool(
                            "read",
                            "read",
                            {
                                "type": "object",
                                "properties": {"z": {"type": "string"}, "a": {"type": "integer"}},
                            },
                        ),
                    ),
                    tool_choice="auto",
                )
            )

        await capture(transcript)
        await first.save("paired")
        restored = await WorkSession(control, "same").restore(TurnTranscript(()))
        assert restored.chain_id == transcript.chain_id
        await capture(restored)
    assert captured[0] == captured[1]
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_main_entry_captures_current_task_before_work_status(database, tmp_path):
    control = await _control(database, tmp_path)
    runner = SimpleNamespace(run=AsyncMock(return_value=None))
    turns = MainAgentTurnService(
        SimpleNamespace(_settings=SimpleNamespace(runtime_work_enabled=False)), runner
    )
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider())
    chat = harness.processor._chat
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="main",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        work_control=control,
    )
    current = ChatMessage("user", "current task plus dynamic context")
    await turns.run(
        (ChatMessage("system", "fixed"), ChatMessage("user", "history"), current), runtime, None
    )
    messages, prepared, _backend = runner.run.call_args.args
    assert prepared.compaction_brief == current
    assert messages[-1] == current  # Host state freezes later, at first prepare_request.
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_original_trigger_requirements_survive_goal_rewrite_and_business_resume(
    database, tmp_path
):
    from tests.support.work_compaction import summary_json

    from qq_ai_bot.persistence.models import ChatEventModel

    control = await _control(database, tmp_path)
    event_id = control.source["trigger_event_id"]
    original = "Investigate the full source. Do not deploy or resend the existing artifact."
    async with database.immediate_session() as writer:
        event = await writer.get(ChatEventModel, event_id)
        assert event.canonical_conversation_id == control.lease.conversation_id
        event.content = original
    initial = (ChatMessage("system", "fixed"), ChatMessage("user", "current conversation"))
    first = WorkSession(control, "same")
    control.session = first
    await first.restore(TurnTranscript(initial), compaction_brief=initial[-1])
    result = json.loads(
        await control.execute(
            "task_control", {"action": "update", "goal": "Verify the source"}, "rewrite"
        )
    )
    assert result["ok"]
    await first.save("paired")
    fresh = (initial[0], ChatMessage("user", "latest chat, with no copy of the old request"))
    resumed = WorkSession(control, "same")
    control.session = resumed
    restored = await resumed.restore(TurnTranscript(fresh), compaction_brief=fresh[-1])
    assert restored.request().messages[:2] == fresh
    material = json.loads(restored.request().messages[-1].content)
    assert material["goal"] == "Verify the source"
    assert material["original_request"] == {"event_id": event_id, "text": original}
    source = json.loads(await resumed.summary_source())
    reference = f"event:{event_id}"
    assert source["original_request_ref"] == reference
    assert reference in source["source_refs"]
    assert any(
        record.get("original_request_event_id") == event_id and record["content"] == original
        for record in source["records"]
    )
    summary = json.loads(summary_json(source))
    summary["task_directives"] = [{"text": original, "refs": [reference]}]
    for _ in range(20):
        restored.append(ChatMessage("assistant", "Old investigation body. " * 500))
    # Re-capture the source after new records are appended, while retaining the
    # exact originating event as a supported summary reference.
    resumed._compaction_source = None
    await resumed.summary_source()
    candidate = await resumed.compact(json.dumps(summary))
    capsule = json.loads(candidate.request().messages[-1].content)
    assert capsule["task_material"]["directives"][0]["text"] == original
    assert capsule["task_material"]["directives"][0]["refs"] == [reference]
    assert capsule["task_material"]["original_request_ref"] == reference
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("kind", ["typed_unknown", "typed_refused", "legacy_success"])
async def test_pending_recovery_uses_original_evidence_not_display(
    database, tmp_path, monkeypatch, changed, kind
):
    from sqlalchemy import update

    from qq_ai_bot.capabilities.results import ToolExecutionResult
    from qq_ai_bot.runtime.effect_outcomes import execution_evidence
    from qq_ai_bot.runtime.work_schema_v1 import effects

    control = await _control(database, tmp_path)
    initial = (ChatMessage("system", "fixed"), ChatMessage("user", "retain task"))
    first = WorkSession(control, "original")
    await first.restore(TurnTranscript(initial), compaction_brief=initial[-1])
    call = ToolCall("original-call", ToolFunction("terminal_exec", "{}"))
    first.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
    await first.save("response", (call,))
    key = first.call_key(call.id)
    await control.repository.prepare_effect(control.lease, control.current["id"], key, "tool")
    stored = {"result": "{}"}
    run_id = "original-run:" + "opaque-provider-identity" * 8
    if kind == "legacy_success":
        stored = {
            "result": json.dumps({"ok": True, "data": {"status": "succeeded", "run_id": run_id}})
        }
    elif kind != "legacy_empty":
        outcome = ToolExecutionResult(
            ok=False,
            data={"executed": kind != "typed_refused", "run_id": run_id},
            uncertain=kind == "typed_unknown",
            mutation_committed=False if kind == "typed_refused" else None,
        )
        stored = {
            "result": '{"ok":true,"uncertain":false}',
            "outcome": execution_evidence(
                outcome,
                tool="typed_original" if kind == "typed_original_name" else "terminal_exec",
                side_effecting=True,
            ),
        }
    raw = json.dumps(stored)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(effects)
            .where(effects.c.effect_key == key)
            .values(state="accepted", receipt_json=raw)
        )
    observed = []
    observe = type(control).observe_evidence

    def record(owner, fact):
        observed.append(dict(fact))
        observe(owner, fact)

    monkeypatch.setattr(type(control), "observe_evidence", record)
    before = await control.repository.get(control.current["id"])
    resumed = WorkSession(control, "changed" if changed else "original")
    await resumed.restore(TurnTranscript(initial), compaction_brief=initial[-1])
    assert observed
    original = observed[0]
    assert original["run_id"] == run_id
    assert original["tool"] == (
        "typed_original" if kind == "typed_original_name" else "terminal_exec"
    )
    if kind == "legacy_success":
        assert original["ok"] is True and original["uncertain"] is False
    elif kind == "typed_original_name":
        assert original["ok"] is False and original["uncertain"] is False
    elif kind == "typed_refused":
        assert original["executed"] is False and original["uncertain"] is False
    else:
        assert original["uncertain"] is True and original["ok"] is False
    if changed:
        messages = resumed.transcript.request().messages
        audit = next(
            message.content
            for message in messages
            if "旧模型链未配对调用的原始执行状态" in (message.content or "")
        )
        status = (
            "not_dispatched"
            if kind == "typed_refused"
            else "recorded"
            if kind in {"legacy_success", "typed_original_name"}
            else "unknown"
        )
        assert f'"status": "{status}"' in audit
        assert run_id in audit
    after = await control.repository.get(control.current["id"])
    assert after["tool_calls"] == before["tool_calls"]
    assert after["model_requests"] == before["model_requests"]
    assert await resumed.journal.effect_state(key) == "accepted"


@pytest.mark.asyncio
async def test_optional_note_publication_conflict_keeps_normal_resume_composition(
    database, tmp_path, monkeypatch
):
    from tests.conftest import MemorySender
    from tests.support.runtime_execution import make_work_resumer
    from tests.unit.test_history_dispatch_ownership import _scene, _tool

    from qq_ai_bot.conversation.observation_models import ContextObservationModel
    from qq_ai_bot.conversation.observations import ContextObservationRepository
    from qq_ai_bot.conversation.projections import ProjectionConflict
    from qq_ai_bot.runtime.work_schema_v1 import work

    provider = FakeLLMProvider()

    def respond(_request):
        count = len(provider.requests)
        if count == 1:
            return _tool(
                "task_control", {"action": "accept", "goal": "continue actual work"}, "accept"
            )
        if count == 2:
            return _tool(
                "task_control",
                {
                    "action": "update",
                    "context_note": {
                        "version": 1,
                        "facts": [{"text": "optional sourced note", "refs": ["goal"]}],
                        "unresolved": [],
                        "next_steps": [],
                    },
                },
                "optional-note",
            )
        assert count == 3, "paid request was replayed"
        return ChatResponse("actual internal result", 0)

    provider._responder = respond
    publication = AsyncMock(side_effect=ProjectionConflict("fixture conflict"))
    monkeypatch.setattr(ContextObservationRepository, "publish_note", publication)
    env, harness, chat, _state, inbound = await _scene(
        database, tmp_path, provider, request_limit=2
    )
    await harness.processor.handle(inbound, MemorySender())
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        identity = await reader.scalar(select(work.c.id))
    queued = await repository.get(identity)
    assert queued["state"] == "queued" and queued["model_requests"] == 2
    saved = json.loads(queued["checkpoint_json"])["context_note"]
    resumer = make_work_resumer(
        repository,
        ledger=harness.ledger,
        scopes=chat._conversation_scopes,
        turns=chat._turn_coordinator,
        router=env.router,
        config=chat._runtime_config,
        generate_self=chat.generate_self_initiative,
        generate_wakeup=chat.generate_main_agent_wakeup,
        bindings=chat.runtime.bindings,
    )
    assert await resumer.resume(queued) is None
    completed = await repository.get(identity)
    assert completed["state"] == "completed" and len(provider.requests) == 3
    assert json.loads(completed["checkpoint_json"])["context_note"] == saved
    assert publication.await_count >= 2
    async with database.sessions() as reader:
        observations = (await reader.scalars(select(ContextObservationModel))).all()
    assert not any(observation.source_key.startswith("work-note:") for observation in observations)
