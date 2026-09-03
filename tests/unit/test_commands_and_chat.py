"""Command behavior, cancellation, and send-failure semantics."""

from __future__ import annotations

import asyncio

import pytest
from tests.conftest import MemorySender, build_harness, make_settings

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
from qq_ai_bot.services.chat import _with_memory_mutation_contract
from qq_ai_bot.services.processor import (
    MENTION_ONLY_CONTEXT,
    ProcessResult,
    _vision_failure_message,
)


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


def test_only_mutation_access_appends_the_write_receipt_contract() -> None:
    messages = (ChatMessage(role="user", content="更新测试配置"),)

    mutation_messages = _with_memory_mutation_contract(messages, True)

    assert len(mutation_messages) == 2
    assert mutation_messages[-1] is messages[-1]
    assert mutation_messages[-2].role == "system"
    assert "真实工具回执" in (mutation_messages[-2].content or "")
    assert "管理员能力" in (mutation_messages[-2].content or "")
    assert _with_memory_mutation_contract(messages, False) is messages


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("media_download_timeout", "图片下载超时"),
        ("get_image_failed", "QQ 网关未能取得图片资源"),
        ("download_failed", "图片资源下载失败"),
        ("private_url", "图片资源下载失败"),
        ("corrupt_image", "图片文件无法解析"),
        ("too_large", "超过处理范围"),
        ("queue_timeout", "图片识别任务较多"),
        ("timeout", "视觉模型响应超时"),
        ("provider_unavailable", "视觉模型暂时不可用"),
    ],
)
def test_visual_failures_have_distinct_user_messages(
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
    assert "可修改运行时配置参数：220 项" in admin_text
    assert "管理员业务接口：44 项，其中修改型 33 项" in admin_text
    assert "conversation.autonomous_batch_limit" in admin_text
    assert "relationship.set_affection" in admin_text
    assert "受保护配置（12 项，不可修改）" in admin_text
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
    assert allowed.reason == "chat"

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
    assert enabled.reason == "chat"

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
async def test_stop_cancels_only_current_task(database: Database) -> None:
    provider = FakeLLMProvider(delay_seconds=5)
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
        assert result.sent_messages == 0
        assert "已取消" in stop_sender.messages[0].text
        assert chat_sender.messages == []
        assert not harness.concurrency.is_processing(chat_key)
        assert harness.concurrency.is_processing(other_key)

        other_result = await other_task
        assert other_result.reason == "chat"
        assert other_result.handled
        assert other_result.sent_messages >= 1
        assert any("FakeLLM" in (message.text or "") for message in other_sender.messages)
        assert not harness.concurrency.is_processing(other_key)
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
    provider = FakeLLMProvider(lambda _request: "已经撤回")
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
    assert sender.messages[0].text == "记忆变更未执行，本轮没有取得任何有效的记忆写入回执。"


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

    assert result.reason == "chat"
    assert len(provider.requests) == 1
    assert sender.messages[0].text == "主 Agent 仍然会回复"


@pytest.mark.asyncio
async def test_ordinary_chat_always_assembles_agent_context(database: Database) -> None:
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

    assert result.reason == "chat"
    assert len(provider.requests) == 1
    assert sender.messages[0].text == "表情也要先走 Main Agent"


@pytest.mark.asyncio
async def test_group_mention_without_text_starts_a_natural_chat_turn(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: "在呢，怎么啦？")
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
    assert sender.messages[0].text == "在呢，怎么啦？"
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
    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("", message_id="image", unsupported=True), sender
    )
    assert result.reason == "vision_not_configured"
    assert "暂时没有识别成功" in sender.messages[0].text
    assert not provider.requests


@pytest.mark.asyncio
async def test_send_failure_is_not_retried_or_persisted_as_assistant(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    sender = MemorySender(fail=True)
    result = await harness.processor.handle(inbound("hello", message_id="send-fail"), sender)
    assert result.reason == "send_or_storage_failure"
    assert sender.calls == 1
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
