"""Command behavior, cancellation, and send-failure semantics."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract

from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    InboundMessage,
    SenderIdentity,
)
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.enums import (
    MemoryScopeType,
    MemorySourceType,
)
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.runtime.resolver import MemoryStructuredCommand
from qq_ai_bot.memory.runtime.turn_session import apply_memory_tool_groups
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.runtime.contracts import MemoryCapabilityView
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.processor import (
    MENTION_ONLY_CONTEXT,
    ProcessResult,
    _vision_failure_message,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,failure",
    [("send_message", "artifact_transfer_unavailable"), ("poke_person", "route_paused")],
)
async def test_delivery_failure_keeps_normal_answer(
    database: Database, tmp_path, tool_name: str, failure: str
) -> None:
    from dataclasses import replace

    from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
    from qq_ai_bot.domain.messages import ToolCall, ToolFunction
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
    from qq_ai_bot.social.models import SocialError

    calls = 0
    model_calls = 0

    def respond(request: ChatRequest) -> str | ChatResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="delivery",
                        function=ToolFunction(
                            name=tool_name,
                            arguments=json.dumps(
                                {
                                    **({"text": "hello"} if tool_name == "send_message" else {}),
                                }
                            ),
                        ),
                    ),
                ),
            )
        if model_calls == 2:
            results = [m.content or "" for m in request.messages if m.role == "tool"]
            assert any(failure in r for r in results), results
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "lookup-after-failure",
                        ToolFunction("request_tools", json.dumps({"query": tool_name})),
                    ),
                ),
            )
        if model_calls >= 3:
            if model_calls == 3:
                result = json.loads([m.content for m in request.messages if m.role == "tool"][-1])
                assert result.get("error", result.get("error_code")) != "tools_closed", result
                assert result.get("ok") is True, result
            return ChatResponse("", 0)

    class Delivery:
        def __init__(self):
            self.database = database

        async def execute(self, *args: object):
            nonlocal calls
            calls += 1
            if failure == "uncertain":
                return {"status": "uncertain", "operation_id": "test-operation"}
            raise SocialError(failure)

    harness = build_harness(database, make_settings(database.url), FakeLLMProvider(respond))
    bind_main_contract(harness, tmp_path)
    harness.processor._chat._tools.social_service = Delivery()
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "1001")
        presence = await ensure_presence(session, "9999")
        conversation = await ensure_canonical_conversation(
            session, kind="private", primary_scope_key="private:9999:1001", person_id=person
        )
    message = replace(
        inbound("发给我", message_id="send-failure"),
        conversation_id=conversation.conversation_id,
        legacy_conversation_key="private:9999:1001",
        person_id=person,
        presence_id=presence,
    )
    sender = MemorySender()
    result = await harness.processor.handle(message, sender)
    assert result.reason == "chat" and calls == 1
    assert not sender.messages
    assert all(not m.text.startswith("操作未完成：") for m in sender.messages)
    following = MemorySender()
    await harness.processor.handle(
        replace(message, text="聊聊天", message_id="after-failure"), following
    )
    assert calls == 1 and not following.messages


def inbound(
    text: str,
    *,
    message_id: str,
    user_id: str = "1001",
    group_id: str | None = None,
    mentions_bot: bool = False,
    unsupported: bool = False,
) -> InboundMessage:
    from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment

    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id),
        text=text,
        bot_user_id="9999",
        group_id=group_id,
        mentions_bot=mentions_bot,
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image"),) if unsupported else (),
    )


def test_capability_view_owns_first_round_memory_scope() -> None:
    from qq_ai_bot.config import Settings

    defaults = make_settings("sqlite+aiosqlite:///:memory:")
    assert "get_group_memories" in defaults.tooling_first_round_pin_ids
    explicit = Settings(_env_file=None, tooling_first_round_pin_ids_csv="get_self_memories")
    assert explicit.tooling_first_round_pin_ids == ("get_self_memories",)
    requested = frozenset({"memory", "memory.read", "web"})
    passive = MemoryCapabilityView(
        eager_namespaces=(),
        requestable_namespaces=("memory.state.write",),
        hidden_namespaces=(),
        exclusive_namespace=None,
        transition_revision=1,
    )
    eager = MemoryCapabilityView(
        eager_namespaces=("memory.person.read",),
        requestable_namespaces=("memory.state.write",),
        hidden_namespaces=(),
        exclusive_namespace=None,
        transition_revision=1,
    )
    exclusive = MemoryCapabilityView(
        eager_namespaces=("memory.state.write",),
        requestable_namespaces=(),
        hidden_namespaces=(),
        exclusive_namespace="memory.state.write",
        transition_revision=1,
    )

    assert apply_memory_tool_groups(passive, requested) == frozenset({"web"})
    assert apply_memory_tool_groups(eager, frozenset({"web"})) == frozenset({"memory", "web"})
    assert apply_memory_tool_groups(exclusive, frozenset({"admin", "web"})) == frozenset(
        {"admin", "memory", "web"}
    )


@pytest.mark.asyncio
async def test_only_mutation_access_appends_the_write_receipt_contract(database) -> None:
    from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
    from qq_ai_bot.prompting.contracts import CORE_CONTRACT
    from qq_ai_bot.prompting.serializer import strip_dynamic_prefix
    from qq_ai_bot.services.context_assembler import AssembledContext, ContextMetrics

    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    runtime = await chat._runtime_config.snapshot()
    for history in ((), (ChatMessage(role="assistant", content="past"),)):
        context = AssembledContext(
            metadata_payload={},
            history_messages=history,
            current_message=ChatMessage(role="user", content="更新测试配置"),
            recent_delivery=(),
            current_time=chat._time.current_default(),
            current_relationship=None,
            metrics=ContextMetrics(0, 0, len(history), 6, False),
        )
        variants = []
        for exclusive in (False, True):
            composed = await chat._main_turns.compose(
                inbound=None,
                context=context,
                runtime=runtime,
                visual_observation=None,
                visual_failure=False,
                memory_exclusive_write=exclusive,
            )
            variants.append(composed.messages)
            tail = composed.messages[-1].content
            assert strip_dynamic_prefix(tail) == context.current_message.content
            assert ('"exclusive_write":true' in tail) == exclusive
            assert "runtime.automation_intent" not in tail
            assert composed.metrics.total_characters == sum(
                len(message.content or "") for message in composed.messages
            )
        instructions = [DeepSeekResponsesProvider._convert_messages(v)[0] for v in variants]
        assert all(text == instructions[0] for text in instructions)
        assert CORE_CONTRACT in instructions[0]
        assert all(messages[1:-1] == history for messages in variants)
        assert "真实工具回执" in CORE_CONTRACT
        assert "管理员能力" in CORE_CONTRACT


def test_visual_failures_have_distinct_user_messages() -> None:
    for error_code, expected in [
        ("media_download_timeout", "图片下载超时"),
        ("get_image_failed", "QQ 网关未能取得图片资源"),
        ("private_url", "图片资源下载失败"),
        ("corrupt_image", "图片文件无法解析"),
        ("too_large", "超过处理范围"),
        ("queue_timeout", "图片识别任务较多"),
        ("timeout", "视觉模型响应超时"),
        ("provider_unavailable", "视觉模型暂时不可用"),
    ]:
        _check_visual_failures_have_distinct_user_messages(error_code, expected)


def _check_visual_failures_have_distinct_user_messages(
    error_code: str,
    expected: str,
) -> None:
    assert expected in _vision_failure_message(error_code, reply_only=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("help", "QQ AI 助手命令"),
        ("status", "服务版本"),
        ("ping", "pong"),
        ("stop", "当前没有正在处理"),
    ],
)
async def test_basic_commands(
    database: Database,
    command: str,
    expected: str,
) -> None:
    harness = build_harness(database, make_settings(database.url))
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound(f"/ai {command}", message_id=f"cmd-{command}"), sender
    )
    assert result.handled and result.sent_messages == 1
    assert expected in sender.messages[0].text


@pytest.mark.asyncio
async def test_capabilities_reports_complete_range_for_current_real_qq(
    database: Database,
) -> None:
    harness = build_harness(database, make_settings(database.url))

    user_sender = MemorySender()
    await harness.processor.handle(
        inbound("/ai capabilities", message_id="user-capabilities"),
        user_sender,
    )
    user_text = user_sender.messages[0].text
    assert "当前权限：普通用户" in user_text
    assert "可修改运行时配置参数：0 项" in user_text
    assert "本人确定性自助接口：37 项，其中修改型 17 项" in user_text
    assert "memory.add" in user_text
    assert "conversation.autonomous_batch_limit" not in user_text

    admin_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai capabilities",
            message_id="admin-capabilities",
            user_id="9000",
        ),
        admin_sender,
    )
    admin_text = admin_sender.messages[0].text
    assert "当前权限：超级管理员" in admin_text
    assert "可修改运行时配置参数：231 项" in admin_text
    assert "管理员业务接口：44 项，其中修改型 33 项" in admin_text
    assert "conversation.autonomous_batch_limit" in admin_text
    assert "relationship.set_affection" in admin_text
    assert "受保护配置（13 项，不可修改）" in admin_text
    assert "asr.enabled" in admin_text
    assert "asr.api_key" in admin_text
    assert "QQ/OneBot Provider 通用全接口网关：1 项" in admin_text
    assert "call_onebot_api:any_public_action" in admin_text


@pytest.mark.asyncio
async def test_superuser_memory_search_and_index_diagnostics(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    fact = await MemoryFactService(MemoryFactRepository(database)).remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="10001",
            kind="fact",
            memory_key="plan:travel",
            category="plan",
            content="计划去杭州旅行",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    search_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai memory search person 10001 杭州旅行",
            message_id="memory-search-admin",
            user_id="9000",
        ),
        search_sender,
    )
    assert f"{fact.id}. [lexical_match] 计划去杭州旅行" in search_sender.messages[0].text

    status_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai memory index status",
            message_id="memory-index-admin",
            user_id="9000",
        ),
        status_sender,
    )
    assert "缺失 0，孤儿 0" in status_sender.messages[0].text


@pytest.mark.asyncio
async def test_new_changes_only_the_current_scope_generation(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    first = ConversationScope.private("9999", "1001")
    second = ConversationScope.private("9999", "1002")
    await harness.ledger.append(
        bot_user_id="9999",
        platform_message_id="old-first",
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id="1001",
        sender_user_id="1001",
        direction="inbound",
        content="one",
    )
    await harness.ledger.append(
        bot_user_id="9999",
        platform_message_id="old-second",
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id="1002",
        sender_user_id="1002",
        direction="inbound",
        content="two",
    )
    sender = MemorySender()
    await harness.processor.handle(inbound("/ai new", message_id="new-1"), sender)
    first_snapshot = await harness.conversation_rollups.load_prompt_snapshot(first)
    second_snapshot = await harness.conversation_rollups.load_prompt_snapshot(second)
    assert all(event.content != "one" for event in first_snapshot.raw_events)
    assert [event.content for event in second_snapshot.raw_events] == ["two"]


@pytest.mark.asyncio
async def test_superuser_on_off_and_permission(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    super_sender = MemorySender()
    await harness.processor.handle(
        inbound("/ai on", message_id="on", user_id="9000", group_id="2999"),
        super_sender,
    )
    assert (await harness.groups.get("2999")).enabled  # type: ignore[union-attr]
    await harness.processor.handle(
        inbound("/ai off", message_id="off", user_id="9000", group_id="2999"),
        super_sender,
    )
    assert not (await harness.groups.get("2999")).enabled  # type: ignore[union-attr]

    denied_sender = MemorySender()
    await harness.processor.handle(
        inbound("/ai on", message_id="denied", user_id="1001", group_id="2001"),
        denied_sender,
    )
    assert "权限不足" in denied_sender.messages[0].text


@pytest.mark.asyncio
async def test_superuser_can_persistently_toggle_private_users(database: Database) -> None:
    harness = build_harness(
        database,
        make_settings(database.url),
    )

    enabled_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai private 12345678 on",
            message_id="private-on",
            user_id="9000",
        ),
        enabled_sender,
    )
    assert enabled_sender.messages[0].text == "已开启指定 QQ 用户的私聊权限。"
    assert "12345678" not in enabled_sender.messages[0].text

    target_sender = MemorySender()
    allowed = await harness.processor.handle(
        inbound("hello", message_id="new-private-user", user_id="12345678"),
        target_sender,
    )
    assert allowed.reason == "llm_failure"

    disabled_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai private 10010001 off",
            message_id="private-off",
            user_id="9000",
        ),
        disabled_sender,
    )
    denied = await harness.processor.handle(
        inbound("hello", message_id="env-user-disabled", user_id="10010001"),
        MemorySender(),
    )
    assert not denied.handled and denied.reason == "private_not_allowed"


@pytest.mark.asyncio
async def test_superuser_can_toggle_any_group_by_id(database: Database) -> None:
    harness = build_harness(
        database,
        make_settings(database.url, enabled_groups_csv="20010001"),
    )

    await harness.processor.handle(
        inbound(
            "/ai group 29999999 on",
            message_id="target-group-on",
            user_id="9000",
        ),
        MemorySender(),
    )
    enabled = await harness.processor.handle(
        inbound(
            "hello",
            message_id="new-group-message",
            group_id="29999999",
            mentions_bot=True,
        ),
        MemorySender(),
    )
    assert enabled.reason == "llm_failure"

    await harness.processor.handle(
        inbound(
            "/ai group 20010001 off",
            message_id="target-group-off",
            user_id="9000",
        ),
        MemorySender(),
    )
    disabled = await harness.processor.handle(
        inbound(
            "hello",
            message_id="env-group-disabled",
            group_id="20010001",
            mentions_bot=True,
        ),
        MemorySender(),
    )
    assert not disabled.handled and disabled.reason == "group_disabled"


@pytest.mark.asyncio
async def test_access_commands_validate_permission_target_and_switch(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))

    non_admin_sender = MemorySender()
    await harness.processor.handle(
        inbound("/ai private 12345678 on", message_id="not-admin"),
        non_admin_sender,
    )
    assert "权限不足" in non_admin_sender.messages[0].text
    unchanged = await harness.private_users.get("12345678")
    assert unchanged is not None and unchanged.enabled is True

    invalid_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai group not-a-group maybe",
            message_id="invalid-group",
            user_id="9000",
        ),
        invalid_sender,
    )
    assert "格式错误" in invalid_sender.messages[0].text

    protected_harness = build_harness(
        database,
        make_settings(database.url, superusers_csv="90000"),
    )
    protected_sender = MemorySender()
    await protected_harness.processor.handle(
        inbound(
            "/ai private 90000 off",
            message_id="protected-superuser",
            user_id="90000",
        ),
        protected_sender,
    )
    assert protected_sender.messages[0].text == "不能关闭超级用户的私聊权限。"
    protected_setting = await protected_harness.private_users.get("90000")
    assert protected_setting is not None and protected_setting.enabled


def _arm_provider_entry(provider: FakeLLMProvider) -> tuple[asyncio.Event, dict[str, int]]:
    """Watch FakeLLM.complete entry; ``run_llm`` has already registered is_processing."""

    entered = asyncio.Event()
    started = {"count": 0}
    original_complete = provider.complete

    async def complete(request: ChatRequest) -> ChatResponse:
        started["count"] += 1
        entered.set()
        return await original_complete(request)

    provider.complete = complete  # type: ignore[method-assign]
    return entered, started


async def _wait_provider_requests(
    started: dict[str, int],
    entered: asyncio.Event,
    count: int,
    *tasks: asyncio.Task[ProcessResult],
) -> None:
    """Wait until FakeLLM.complete has been entered ``count`` times, or a turn dies."""

    while started["count"] < count:
        entered.clear()
        if started["count"] >= count:
            return
        request_wait = asyncio.create_task(entered.wait())
        try:
            done, _pending = await asyncio.wait(
                {request_wait, *tasks},
                timeout=15,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if request_wait in done:
                await request_wait
                continue
            for task in tasks:
                if task.done():
                    await task
            raise AssertionError(f"FakeLLM did not accept {count} in-flight request(s)")
        finally:
            if not request_wait.done():
                request_wait.cancel()
                await asyncio.gather(request_wait, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_cancels_only_current_task(
    database: Database, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from qq_ai_bot.memory.runtime.turn_session import TurnMemorySession

    closed_sessions = []
    original_close = TurnMemorySession.close

    async def track_close(session: TurnMemorySession) -> None:
        await original_close(session)
        closed_sessions.append(session)

    monkeypatch.setattr(TurnMemorySession, "close", track_close)

    caplog.set_level("INFO", logger="qq_ai_bot.services.evidence_observation")
    provider = FakeLLMProvider(lambda _request: ChatResponse("", 0), delay_seconds=5)
    entered, started = _arm_provider_entry(provider)
    harness = build_harness(database, make_settings(database.url), provider)
    chat_sender = MemorySender()
    other_sender = MemorySender()
    chat_message = inbound("slow", message_id="slow")
    other_message = inbound("other", message_id="other", user_id="1002")
    coordinator = harness.processor._turn_coordinator
    chat_key = coordinator.key_for(chat_message)
    other_key = coordinator.key_for(other_message)
    assert chat_key == ConversationScope.private("9999", "1001").key
    assert other_key == ConversationScope.private("9999", "1002").key
    assert chat_key != other_key
    chat_task = asyncio.create_task(harness.processor.handle(chat_message, chat_sender))
    other_task: asyncio.Task[ProcessResult] | None = None
    try:
        await _wait_provider_requests(started, entered, 1, chat_task)
        assert provider.requests
        assert harness.concurrency.is_processing(chat_key)
        other_task = asyncio.create_task(harness.processor.handle(other_message, other_sender))
        await _wait_provider_requests(started, entered, 2, chat_task, other_task)
        assert harness.concurrency.is_processing(chat_key)
        assert harness.concurrency.is_processing(other_key)

        stop_sender = MemorySender()
        await harness.processor.handle(inbound("/ai stop", message_id="stop"), stop_sender)
        result = await chat_task
        assert result.reason == "cancelled"
        assert any(s._inbound.message_id == "slow" and s._state.closed for s in closed_sessions)
        assert result.sent_messages == 0
        assert "已取消" in stop_sender.messages[0].text
        assert chat_sender.messages == []
        assert not harness.concurrency.is_processing(chat_key)
        assert harness.concurrency.is_processing(other_key)

        other_result = await other_task
        assert other_result.reason == "chat"
        assert other_result.handled
        assert other_result.sent_messages == 0
        assert not other_sender.messages
        assert not harness.concurrency.is_processing(other_key)
        observations = [
            json.loads(record.getMessage().removeprefix("agent_evidence "))
            for record in caplog.records
            if record.name == "qq_ai_bot.services.evidence_observation"
        ]
        prepared = {
            item["correlation_id"] for item in observations if item["phase"] == "request_prepared"
        }
        received = {
            item["correlation_id"] for item in observations if item["phase"] == "response_received"
        }
        assert len(prepared) == 2 and len(received) == 1
    finally:
        leftover = [
            task for task in (chat_task, other_task) if task is not None and not task.done()
        ]
        for task in leftover:
            task.cancel()
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)


@pytest.mark.asyncio
async def test_empty_model_response_is_user_safe(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: "   ")
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()
    result = await harness.processor.handle(inbound("hello", message_id="empty"), sender)
    assert result.reason == "empty_llm_response"
    assert "空内容" in sender.messages[0].text

    # History mention annotations are not transport instructions. Correct once
    # within the existing request budget; never leak the placeholder as a fake @.
    for repair in (False, True):

        def mention_response(request: ChatRequest, repair: bool = repair) -> str | ChatResponse:
            if any("上一段最终正文没有发送" in str(m.content) for m in request.messages):
                return ChatResponse("", 0)
            if repair and any("已拦截且未发送" in str(m.content) for m in request.messages):
                return "请先确认要提醒的具体账号。"
            return "[提及ICE] 喊你呢\n\n@完了"

        mention_provider = FakeLLMProvider(mention_response)
        mention_harness = build_harness(database, make_settings(database.url), mention_provider)
        mention_sender = MemorySender()
        await mention_harness.processor.handle(
            inbound("at ice", message_id=f"mention-placeholder-{repair}"), mention_sender
        )
        assert len(mention_provider.requests) == (3 if repair else 2)
        assert mention_provider.requests[0].tools == mention_provider.requests[1].tools
        assert all("[提及" not in str(message.text) for message in mention_sender.messages)
        assert all("@完了" not in str(message.text) for message in mention_sender.messages)
        assert any("AI 服务暂时不可用" in message.text for message in mention_sender.messages) is (
            not repair
        )


@pytest.mark.asyncio
async def test_keyerror_during_chat_sends_retry_text(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider("ok"))

    async def boom(*_args: object, **_kwargs: object) -> int:
        raise KeyError("call_01_J1dFmYdl1DWqA3sSWJug1179")

    harness.processor._chat.handle_turn = boom  # type: ignore[method-assign]
    sender = MemorySender()
    result = await harness.processor.handle(inbound("点麦当劳", message_id="keyerror"), sender)
    assert result.reason == "internal_failure"
    assert result.handled is True
    assert "请稍后重试" in sender.messages[0].text


@pytest.mark.asyncio
async def test_ordinary_chat_keeps_generic_tool_request_gateway(
    database: Database,
) -> None:
    provider = FakeLLMProvider(lambda _request: "我会按工具回执确认是否记住。")
    harness = build_harness(database, make_settings(database.url), provider)
    harness.processor._chat._tools._memory_mutations = object()  # type: ignore[assignment]

    await harness.processor.handle(
        inbound("请记住我喜欢美式咖啡", message_id="memory-scope-fallback"),
        MemorySender(),
    )

    assert provider.requests
    tool_names = {tool.name for tool in provider.requests[-1].tools}
    assert "request_tools" in tool_names


@pytest.mark.asyncio
async def test_mutation_turn_uses_auto_with_only_write_tool_and_receipt_contract(
    database: Database,
) -> None:
    provider = FakeLLMProvider(lambda _request: ChatResponse("", 0))
    harness = build_harness(database, make_settings(database.url), provider)
    harness.processor._chat._tools._memory_mutations = object()  # type: ignore[assignment]
    sender = MemorySender()
    message = inbound("撤回一条测试配置", message_id="mutation-auto-write-only")

    await harness.processor._chat.respond(
        message,
        message.scope(),
        UserProfileSnapshot(user_id="1001", scope_type=ScopeType.PRIVATE, nickname="tester"),
        "撤回一条测试配置",
        sender,
        turn_token=(
            token := await harness.processor._turn_coordinator.notify_message(
                message.scope().key,
                TurnOrigin.USER_MESSAGE,
            )
        ),
        turn_snapshot=ConversationTurnSnapshot(
            scope_id=(
                appended := await harness.processor._scoped_events.append_inbound(message)
            ).scope.id,
            scope_key=message.scope().key,
            generation=appended.scope.generation,
            trigger_event_id=appended.event.id,
            coordinator_version=token.version,
        ),
        structured_memory_command=MemoryStructuredCommand.WRITE,
    )

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.tool_choice == "auto"
    tool_names = {tool.name for tool in request.tools}
    assert "memory_change" in tool_names
    assert "request_tools" in tool_names
    assert any("真实工具回执" in (message.content or "") for message in request.messages)
    assert not sender.messages


@pytest.mark.asyncio
async def test_unused_planner_fallback_no_longer_blocks_the_agent(
    database: Database,
) -> None:
    provider = FakeLLMProvider(lambda _request: "主 Agent 仍然会回复")
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()

    result = await harness.processor.handle(
        inbound("请记住一个测试配置", message_id="no-planner-fail-closed"),
        sender,
    )

    # This fixture has no SocialService transport, so the explicit-send contract
    # correctly rejects the provider's unsent final after context assembly.
    assert result.reason == "llm_failure"
    assert len(provider.requests) == 2
    assert sender.messages[0].text == "AI 服务暂时不可用，请稍后重试。"


@pytest.mark.asyncio
async def test_ordinary_chat_always_assembles_agent_context(
    database: Database,
) -> None:
    provider = FakeLLMProvider(lambda _request: "表情也要先走 Main Agent")
    harness = build_harness(
        database,
        make_settings(database.url, emoji_enabled=True),
        provider,
    )
    sender = MemorySender()

    result = await harness.processor.handle(
        inbound("发个表情", message_id="emoji-still-calls-agent"),
        sender,
    )

    assert result.reason == "llm_failure"
    assert len(provider.requests) == 2
    assert sender.messages[0].text == "AI 服务暂时不可用，请稍后重试。"
    request = provider.requests[0]
    assert "event_bound_memory_refs" in request.messages[-1].content
    assert "available_memory_subjects" not in request.messages[-1].content
    assert any(
        message.role == "system" and "不是可查询人物名单或权限白名单" in (message.content or "")
        for message in request.messages
    )


@pytest.mark.asyncio
async def test_group_mention_without_text_starts_a_natural_chat_turn(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: ChatResponse("", 0))
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()

    result = await harness.processor.handle(
        inbound(
            "",
            message_id="mention-only",
            group_id="2001",
            mentions_bot=True,
        ),
        sender,
    )

    assert result.reason == "chat"
    assert not sender.messages
    request = provider.requests[0]
    assert request.messages[-1].role == "user"
    assert request.messages[-1].content.endswith(MENTION_ONLY_CONTEXT)
    events = await EventLedgerRepository(database).list_scope_recent(
        ConversationScope.group("9999", "2001"),
        limit=10,
    )
    inbound_event = next(row for row in events if row.direction == "inbound")
    assert inbound_event.content == ""


@pytest.mark.asyncio
async def test_unsupported_message_degrades_without_calling_llm(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: ChatResponse("", 0))
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("", message_id="image", unsupported=True), sender
    )
    assert result.reason == "vision_not_configured"
    assert "暂时没有识别成功" in sender.messages[0].text
    assert not provider.requests


@pytest.mark.asyncio
async def test_native_images_use_full_chat_without_external_vision(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    import io
    from dataclasses import replace
    from pathlib import Path

    from PIL import Image

    from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment
    from qq_ai_bot.services.attachment_inputs import AttachmentInputService
    from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
    from qq_ai_bot.services.media_resolver import MediaResolver

    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(stream, format="PNG")
    image_data = "base64://" + base64.b64encode(stream.getvalue()).decode()
    provider = FakeLLMProvider(lambda _request: ChatResponse("", 0))
    harness = build_harness(database, make_settings(database.url), provider)
    resolver = MediaResolver()
    harness.processor._native_images = AttachmentInputService(
        resolver,
        ImagePreprocessor(),
        concurrency=1,
        pending_limit=2,
        timeout=2,
        max_bytes=100_000,
    )
    sender = MemorySender()
    message = replace(
        inbound("", message_id="native-image"),
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image", file=image_data),),
    )
    try:
        result = await harness.processor.handle(message, sender)
        assert result.reason == "chat"
        request = provider.requests[0]
        assert request.messages[0].role == "system"
        assert request.messages[-1].images
        assert all(not item.images for item in request.messages[:-1])
        assert "base64" not in repr(request)
        # An actual private URL is rejected locally, not forwarded to the model.
        unsafe = replace(
            message,
            message_id="unsafe-image",
            attachments=(
                MessageAttachment(AttachmentKind.IMAGE, "image", url="http://127.0.0.1/secret"),
            ),
        )
        count = len(provider.requests)
        failure = await harness.processor.handle(unsafe, sender)
        assert failure.reason == "vision_resource_unavailable"
        assert len(provider.requests) == count

        decoded_paths: list[Path] = []

        async def decoder(*args: str) -> bytes:
            decoded_paths.append(Path(args[-1]))
            assert "-protocol_whitelist" in args and "-format_whitelist" in args
            if args[0] == "ffprobe":
                return b'{"streams":[{"width":32,"height":32}],"format":{"duration":"4"}}'
            assert "out_range=full,format=yuvj420p" in args[args.index("-vf") + 1]
            Image.new("RGB", (32, 32), "blue").save(Path(args[-1]), format="JPEG")
            return b""

        monkeypatch.setattr("qq_ai_bot.services.video_frames._run", decoder)
        video_data = "base64://" + base64.b64encode(b"\x00\x00\x00\x18ftypisom").decode()
        from tempfile import TemporaryDirectory

        import httpx

        from qq_ai_bot.services.media_resolver import MediaResolutionError
        from qq_ai_bot.vision.models import MediaReference

        reference = MediaReference(file=video_data, source="current")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "download.mp4"
            await resolver.download_attachment(reference, path, max_download_bytes=12)
            assert path.stat().st_size == 12
            with pytest.raises(MediaResolutionError):
                await resolver.download_attachment(reference, path, max_download_bytes=11)
        assert (await resolver.resolve(reference)).byte_size == 12

        class VideoChunks(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[no-untyped-def]
                yield b"x" * 65536
                yield b"y" * 100

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=VideoChunks()))
        ) as client:
            bounded = MediaResolver(
                client=client,
                max_download_bytes=16,
                host_resolver=lambda *_: ["93.184.216.34"],
            )
            remote = MediaReference(url="https://example.com/video.mp4", source="current")
            with TemporaryDirectory() as directory:
                path = Path(directory) / "video.mp4"
                await bounded.download_attachment(remote, path, max_download_bytes=65636)
                assert path.stat().st_size == 65636
                with pytest.raises(MediaResolutionError):
                    await bounded.download_attachment(remote, path, max_download_bytes=65635)
                with pytest.raises(MediaResolutionError):
                    await bounded.resolve(remote)
        video = replace(
            message,
            message_id="native-video",
            attachments=(MessageAttachment(AttachmentKind.VIDEO, "video", file=video_data),),
        )
        assert (await harness.processor.handle(video, sender)).reason == "chat"
        frames = provider.requests[-1].messages[-1].images
        assert frames and frames[0].video_timestamp_seconds == 0
        assert "没有音频" in (provider.requests[-1].messages[-1].content or "")
        replied = replace(
            video,
            message_id="reply-video",
            attachments=(),
            reply_attachments=(replace(video.attachments[0], source="reply"),),
        )
        assert (await harness.processor.handle(replied, sender)).reason == "chat"
        assert all(i.source == "reply" for i in provider.requests[-1].messages[-1].images)
        from qq_ai_bot.adapters.onebot.normalizer import project_serialized_segments

        file_projection = project_serialized_segments(
            ({"type": "file", "data": {"name": "movie.mp4", "file": video_data}},),
            yuki_account_ids=frozenset(),
            source="current",
        )
        assert file_projection.attachments[0].filename == "movie.mp4"
        file_video = replace(
            video,
            message_id="file-video",
            attachments=file_projection.attachments,
        )
        assert (await harness.processor.handle(file_video, sender)).reason == "chat"
        assert provider.requests[-1].messages[-1].images
        file_reply = replace(
            file_video,
            message_id="file-reply",
            attachments=(),
            reply_attachments=(replace(file_video.attachments[0], source="reply"),),
        )
        assert (await harness.processor.handle(file_reply, sender)).reason == "chat"
        assert all(i.source == "reply" for i in provider.requests[-1].messages[-1].images)
        text_file = replace(
            file_video,
            message_id="text-file",
            attachments=(
                MessageAttachment(
                    AttachmentKind.FILE,
                    "file",
                    filename="notes.txt",
                    file="base64://"
                    + base64.b64encode("a unique attachment fact 你好吗".encode()).decode(),
                ),
            ),
        )
        harness.processor._native_images.images_enabled = False
        assert (await harness.processor.handle(text_file, sender)).reason == "chat"
        assert "a unique attachment fact" in provider.requests[-1].messages[-1].content
        assert not provider.requests[-1].messages[-1].images
        assert "不可信资料" in provider.requests[-1].messages[-1].content
        assert all(not path.parent.exists() for path in decoded_paths)

        from tests.support.forwarded_input_cases import check_forwarded_inputs

        snapshot = await harness.processor._runtime_config.snapshot()
        await check_forwarded_inputs(
            harness.processor._native_images, message, snapshot.vision, image_data
        )

        import asyncio

        from qq_ai_bot.services.video_frames import sample_video
        from qq_ai_bot.vision.models import DownloadedMedia

        media = DownloadedMedia(
            content=b"\x00\x00\x00\x18ftypisom",
            content_type="video/mp4",
            content_hash="test",
            byte_size=12,
        )
        sparse = await sample_video(media, source="current", maximum=16)
        dense = await sample_video(media, source="current", maximum=16, sample_interval_seconds=1)
        capped = await sample_video(media, source="current", maximum=3, sample_interval_seconds=1)
        assert len(sparse) == 2 and len(dense) == 5 and len(capped) == 3
        assert capped[-1].video_timestamp_seconds == pytest.approx(3.0)

        async def longer_audio_decoder(*args: str) -> bytes:
            if args[0] == "ffprobe":
                return (
                    b'{"streams":[{"width":32,"height":32,"duration":"3"}],'
                    b'"format":{"duration":"4"}}'
                )
            return await decoder(*args)

        monkeypatch.setattr("qq_ai_bot.services.video_frames._run", longer_audio_decoder)
        video_end = await sample_video(media, source="current", maximum=3)
        assert video_end[-1].video_timestamp_seconds == pytest.approx(2.0)
        monkeypatch.setattr("qq_ai_bot.services.video_frames._run", decoder)
        assert all(not path.parent.exists() for path in decoded_paths)
        from qq_ai_bot.services.vision_service import VisionProcessingError

        with pytest.raises(VisionProcessingError):
            await sample_video(media, source="current", maximum=4, max_duration_seconds=3)
        assert all(not path.parent.exists() for path in decoded_paths)

        entered = asyncio.Event()

        async def cancelled_decoder(*args: str) -> bytes:
            decoded_paths.append(Path(args[-1]))
            entered.set()
            await asyncio.Event().wait()
            return b""

        monkeypatch.setattr("qq_ai_bot.services.video_frames._run", cancelled_decoder)
        task = asyncio.create_task(sample_video(media, source="current", maximum=4))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(not path.parent.exists() for path in decoded_paths)
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_silent_final_never_tries_transport_or_persists_assistant(database: Database) -> None:
    harness = build_harness(
        database,
        make_settings(database.url),
        FakeLLMProvider(lambda _request: ChatResponse("", 0)),
    )
    sender = MemorySender(fail=True)
    result = await harness.processor.handle(inbound("hello", message_id="send-fail"), sender)
    assert result.reason == "chat"
    assert sender.calls == 0
    identity = ConversationScope.private("9999", "1001")
    history = await harness.conversation_rollups.load_prompt_snapshot(identity)
    assert [(item.direction, item.content) for item in history.raw_events] == [("inbound", "hello")]


@pytest.mark.asyncio
async def test_status_command_labels_semantic_when_no_overlay(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    await harness.processor.handle(inbound("hello", message_id="status-plain"), MemorySender())
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("/ai status", message_id="status-no-overlay"), sender
    )
    assert result.handled
    text = sender.messages[0].text
    lines = text.splitlines()
    assert "紧急 overlay：无" in lines
    assert "紧急 overlay coverage：无" in lines
    assert "rewrite_pending：否" in lines
    assert any(line.startswith("语义 Rollup coverage：") for line in lines)
    assert any(line.startswith("有效 Prompt coverage：") for line in lines)
    assert any(line.startswith("语义未覆盖事件数：") for line in lines)
    assert not any(line.startswith("Rollup coverage：") for line in lines)
    assert not any(line.startswith("未覆盖事件数：") for line in lines)


@pytest.mark.asyncio
async def test_status_command_labels_overlay_and_omits_summary_text(database: Database) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import select

    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationModel,
        CanonicalConversationRollupEmergencyOverlayModel,
        ConversationLegacyAliasModel,
    )

    harness = build_harness(database, make_settings(database.url))
    await harness.processor.handle(inbound("hello", message_id="status-ov-1"), MemorySender())
    await harness.processor.handle(inbound("again", message_id="status-ov-2"), MemorySender())
    scope = ConversationScope.private("9999", "1001")
    snapshot = await harness.conversation_rollups.load_prompt_snapshot(scope)
    cover = snapshot.raw_events[0].id
    secret = "SECRET_OVERLAY_SUMMARY_MUST_NOT_APPEAR"
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        alias = await session.scalar(
            select(ConversationLegacyAliasModel).where(
                ConversationLegacyAliasModel.scope_key == scope.key
            )
        )
        assert alias is not None
        row = await session.get(CanonicalConversationModel, alias.conversation_id)
        assert row is not None
        session.add(
            CanonicalConversationRollupEmergencyOverlayModel(
                conversation_id=row.id,
                generation=row.generation,
                covered_through_event_id=cover,
                summary_text=secret,
                source_fingerprint="a" * 64,
                base_semantic_revision=0,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("/ai status", message_id="status-with-overlay"), sender
    )
    assert result.handled
    text = sender.messages[0].text
    lines = text.splitlines()
    assert secret not in text
    assert "紧急 overlay：有" in lines
    assert f"紧急 overlay coverage：{cover}" in lines
    assert "rewrite_pending：是" in lines
    assert any(line.startswith("语义未覆盖事件数：") for line in lines)
    assert any(line.startswith("有效 Prompt coverage：") for line in lines)
    assert f"有效 Prompt coverage：{cover}" in lines
    assert not any(line.startswith("Rollup coverage：") for line in lines)
    assert not any(line.startswith("未覆盖事件数：") for line in lines)
