"""Plugin Main uses real domain approval without a second tool-name list."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.runtime.work_repository import WorkConflict, WorkRepository
from yuki.runtime.work_schema_v1 import work
from yuki.services.agent_tools import ToolRuntime
from yuki.services.execution_sources import recover_source
from yuki.web.models import WebMode, WebSearchResponse, WebSearchSource
from yuki_plugin_sdk.permissions import PluginPermission


async def source_host(database, tmp_path, provider, permissions):
    env, harness, chat, _, _ = await _scene(database, tmp_path, provider)
    chat._tools.social_service = env.service
    async with database.sessions() as session:
        event_id = await session.scalar(select(ChatEventModel.id))
    event = await harness.ledger.get_event(event_id)
    recovered = await recover_source(
        database,
        env.context.conversation_id,
        {
            "origin": "user_message",
            "generation": 1,
            "trigger_event_id": event.id,
            "actor_person_id": env.person,
            "presence_id": env.presence,
            "bot_user_id": "80001",
        },
        request_id="original-plugin-source",
    )
    source = ToolRuntime(
        inbound=recovered.inbound(event, legacy_conversation_key="bot:80001:group:20001"),
        runtime_config=await chat._runtime_config.snapshot(),
    )
    host = HostPluginContext(
        plugin_id="test.main-domain-grants",
        approved_permissions=(PluginPermission.AGENT_RUN, *permissions),
        services=PluginFacadeServices(
            approval_revision="one-real-approved-manifest",
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    return env, harness, chat, source, host


@pytest.mark.parametrize("permission", [PluginPermission.WEB_SEARCH, PluginPermission.WEB_READ])
async def test_main_web_search_and_page_read_keep_separate_live_grants(
    database, tmp_path, permission
):
    provider = FakeLLMProvider()
    _, harness, chat, source, host = await source_host(database, tmp_path, provider, (permission,))
    harness.settings.web_mode = WebMode.TAVILY
    page = WebSearchSource(
        "original", "Original", "https://example.com/old", "example.com", "", "body"
    )
    web = SimpleNamespace(
        name="original-provider",
        search=AsyncMock(return_value=WebSearchResponse("query", (page,), "original-request", 0)),
        extract=AsyncMock(return_value=page),
    )
    chat._tools._web_provider = web
    replies = {}

    def respond(request):
        if len(provider.requests) == 1:
            return _tool("web_search", {"query": "query"}, "search")
        if len(provider.requests) == 2:
            replies["search"] = json.loads(request.messages[-1].content)
            return _tool("read_webpage", {"url": page.url}, "page")
        replies["page"] = json.loads(request.messages[-1].content)
        return _tool("task_control", {"action": "complete", "result": "done"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Use only the approved research domain")
        assert result.ok and result.data["state"] == "completed"
        allowed = "search" if permission is PluginPermission.WEB_SEARCH else "page"
        denied = "page" if allowed == "search" else "search"
        assert replies[allowed]["ok"], replies
        assert replies[denied]["error_code"] == "permission_denied", replies
        assert web.search.await_count == int(allowed == "search")
        assert web.extract.await_count == int(allowed == "page")
        row = await WorkRepository(database).get(result.data["work_id"])
        assert "allowed_tools" not in json.loads(row["source_json"])
    finally:
        await host.close_host_resources()


async def test_main_approved_send_uses_original_receipt_and_live_revocation(database, tmp_path):
    provider = FakeLLMProvider()
    env, _, _, source, host = await source_host(
        database, tmp_path, provider, (PluginPermission.MESSAGE_GROUP_SEND,)
    )
    receipts = []

    def respond(request):
        if len(provider.requests) == 1:
            return _tool("send_message", {"text": "One original approved message"}, "first")
        if len(provider.requests) == 2:
            receipts.append(json.loads(request.messages[-1].content))
            host._approved_permissions = frozenset({PluginPermission.AGENT_RUN})
            return _tool("send_message", {"text": "revoked message"}, "revoked")
        receipts.append(json.loads(request.messages[-1].content))
        return _tool("task_control", {"action": "complete", "result": "done"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Use the approved original source once")
            original = await WorkRepository(database).get(result.data["work_id"])
            restored = await host.agent.result(result.data["work_id"])
        assert result.ok and restored.data["text"] == result.data["text"] == "done"
        assert receipts[0]["ok"], receipts
        assert receipts[0]["data"]["status"] == "succeeded", receipts
        assert receipts[1]["error_code"] == "permission_denied", receipts
        assert [name for name, _ in env.bot.calls].count("send_group_msg") == 1
        assert await WorkRepository(database).get(result.data["work_id"]) == original
        assert len(provider.requests) == 3
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_cold_retry_keeps_original_work_after_retired_name_subset_is_removed(
    database, tmp_path, ambiguous
):
    provider = FakeLLMProvider()
    _, _, chat, source, host = await source_host(database, tmp_path, provider, ())
    sandbox = SimpleNamespace(execute=AsyncMock(return_value={"ready": True}))
    chat._tools.sandbox_client = sandbox
    provider._responder = lambda _: (
        _tool("environment_status", {}, "original-status")
        if len(provider.requests) == 1
        else _tool(
            "task_control", {"action": "complete", "result": "original paid result"}, "finish"
        )
    )
    with host.invocation_scope(host.plugin_id, source):
        first = await host.agent.run("Inspect the original environment")
    repository = WorkRepository(database)
    original = await repository.get(first.data["work_id"])
    saved = json.loads(original["source_json"])
    # An actual pre-454 caller could submit this optional subset. Preserve its
    # historical boundary codec and all accepted Work/operation/model facts.
    saved["allowed_tools"] = ["environment_status", "workspace_read"]
    saved["execution_boundary"] = hashlib.sha256(
        json.dumps(
            [
                saved["origin"],
                original["conversation_id"],
                saved["parent_execution_id"],
                saved["allowed_tools"],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(work)
            .where(work.c.id == original["id"])
            .values(
                source_json=json.dumps(saved, ensure_ascii=False, separators=(",", ":")),
                source_key=f"invocation:{saved['execution_boundary']}",
            )
        )
    original = await repository.get(original["id"])
    alternative = None
    if ambiguous:
        # Before 454 a second explicit subset could create another accepted
        # boundary for this same source. Neither original is silently selected.
        other = {**saved, "allowed_tools": [*saved["allowed_tools"], "workspace_write"]}
        other["execution_boundary"] = hashlib.sha256(
            json.dumps(
                [
                    other["origin"],
                    original["conversation_id"],
                    other["parent_execution_id"],
                    other["allowed_tools"],
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        lease = await repository.acquire(original["conversation_id"], original["generation"])
        try:
            alternative = await repository.accept(
                lease,
                source_key=f"invocation:{other['execution_boundary']}",
                source=other,
                goal=original["goal"],
                initial_state="queued",
            )
        finally:
            await repository.release(lease)
    await host.close_host_resources()
    await database.close()
    restored = HostPluginContext(
        plugin_id=host.plugin_id,
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=host._services,
    )
    try:
        with restored.invocation_scope(restored.plugin_id, source):
            if alternative is not None:
                with pytest.raises(WorkConflict, match="plugin_invocation_ambiguous") as error:
                    await restored.agent.run("Inspect the original environment")
                assert original["id"] in str(error.value)
                assert alternative["id"] in str(error.value)
                assert await repository.get(alternative["id"]) == alternative
                assert len(provider.requests) == 2
                assert await repository.get(original["id"]) == original
                return
            replay = await restored.agent.run("Inspect the original environment")
        assert replay.data["text"] == first.data["text"] == "original paid result"
        assert replay.data["work_id"] == first.data["work_id"]
        assert len(provider.requests) == 2
        sandbox.execute.assert_awaited_once()
        assert await repository.get(original["id"]) == original
    finally:
        await restored.close_host_resources()


async def test_notification_entry_keeps_its_own_approval_and_environment_needs_agent_run(
    database, tmp_path
):
    from yuki.plugin_host.facades import _agent_dependencies
    from yuki.plugin_host.main_turn import run_plugin_main_turn

    provider = FakeLLMProvider()
    _, harness, chat, source, host = await source_host(database, tmp_path, provider, ())
    host._approved_permissions = frozenset(
        {PluginPermission.NOTIFICATION_AGENT, PluginPermission.WEB_READ}
    )
    harness.settings.web_mode = WebMode.TAVILY
    page = WebSearchSource(
        "original", "Original", "https://example.com/old", "example.com", "", "body"
    )
    web = SimpleNamespace(name="reader", extract=AsyncMock(return_value=page))
    sandbox = SimpleNamespace(execute=AsyncMock(return_value={"ready": True}))
    chat._tools._web_provider = web
    chat._tools.sandbox_client = sandbox
    replies = []

    def respond(request):
        if len(provider.requests) == 1:
            return _tool("environment_status", {}, "environment")
        replies.append(json.loads(request.messages[-1].content))
        if len(provider.requests) == 2:
            return _tool("read_webpage", {"url": page.url}, "page")
        return _tool("task_control", {"action": "complete", "result": "notified"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            invocation = host._invocation()
            _, runtime = await _agent_dependencies(host, invocation)
            result = await run_plugin_main_turn(
                host,
                invocation,
                instruction="Read an approved page from the original notification entry",
                context_data="",
                runtime=runtime,
                tools=None,
                permission=PluginPermission.NOTIFICATION_AGENT,
            )
        assert result.text == "notified" and result.work_state == "completed"
        assert replies[0]["error_code"] == "permission_denied", replies
        assert replies[1]["ok"], replies
        sandbox.execute.assert_not_awaited()
        web.extract.assert_awaited_once()
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize(
    "permission", [PluginPermission.AGENT_RUN, PluginPermission.NOTIFICATION_AGENT]
)
async def test_main_clock_does_not_require_automation_approval(database, tmp_path, permission):
    from yuki.automation.repository import AutomationRepository
    from yuki.automation.service import AutomationService
    from yuki.automation.tools import AutomationToolService
    from yuki.plugin_host.facades import _agent_dependencies
    from yuki.plugin_host.main_turn import run_plugin_main_turn
    from yuki.time.service import TimeContextService

    provider = FakeLLMProvider()
    _, harness, chat, source, host = await source_host(database, tmp_path, provider, ())
    host._approved_permissions = frozenset({permission})
    harness.settings.automation_enabled = True
    chat.set_automation_tools(
        AutomationToolService(
            AutomationService(
                settings=harness.settings,
                repository=AutomationRepository(database),
                time_service=TimeContextService(),
            )
        )
    )
    replies = []

    def respond(request):
        if len(provider.requests) == 1:
            return _tool("time_get_current", {}, "original-time")
        replies.append(json.loads(request.messages[-1].content))
        return _tool("task_control", {"action": "complete", "result": "time read"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            invocation = host._invocation()
            _, runtime = await _agent_dependencies(host, invocation)
            result = await run_plugin_main_turn(
                host,
                invocation,
                instruction="Read the real current time",
                context_data="",
                runtime=runtime,
                tools=None,
                permission=permission,
            )
        assert result.text == "time read" and result.work_state == "completed"
        assert replies[0]["ok"], replies
        assert replies[0]["data"]["timezone"] == "Asia/Shanghai"
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("write_approved", [False, True])
async def test_self_main_memory_write_and_delete_keep_existing_sdk_approvals(
    database, tmp_path, write_approved
):
    from dataclasses import replace

    from tests.integration.test_self_common_capabilities import runtime, self_actor
    from tests.unit.test_self_initiative_memory import _service

    from yuki.tool_results.recorder import ToolReceiptRepository

    provider = FakeLLMProvider()
    env, harness, chat, _, _ = await _scene(database, tmp_path, provider)
    harness.settings.self_memory_enabled = True
    mutations, facts, _, _ = _service(database, self_memory_enabled=True)
    chat._tools._memory_mutations = mutations
    chat._tools._memories = facts
    chat._tools.social_service = env.service
    chat._tool_receipts = ToolReceiptRepository(database)
    actor = await self_actor(database, env)
    source = replace(runtime(actor, env), runtime_config=await chat._runtime_config.snapshot())
    permissions = {PluginPermission.AGENT_RUN, PluginPermission.GROUP_MEMBERS_READ}
    if write_approved:
        permissions.add(PluginPermission.MEMORY_WRITE)
    host = HostPluginContext(
        plugin_id="test.main-memory-grants",
        approved_permissions=permissions,
        services=PluginFacadeServices(
            approval_revision="original-memory-approval",
            ledger=harness.ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    replies = []
    created = None

    def respond(request):
        nonlocal created
        count = len(provider.requests)
        if count == 1:
            return _tool("get_group_members", {}, "original-evidence")
        replies.append(json.loads(request.messages[-1].content))
        if count == 2:
            return _tool(
                "memory_change",
                {
                    "operation": "create",
                    "target": {"subject_ref": "self", "scope_type": "self"},
                    "new_content": "我在真实工具调用中查询了 member 的群成员资料。",
                    "memory_key": "self_episode:plugin-domain",
                    "category": "self_episode",
                    "kind": "episode",
                    "evidence_quote": "member",
                },
                "write",
            )
        if write_approved and count == 3:
            assert replies[-1]["ok"], replies
            created = replies[-1]["data"]["new_fact_id"]
            return _tool(
                "memory_change",
                {"operation": "invalidate", "fact_id": created, "evidence_quote": "member"},
                "delete-denied",
            )
        if write_approved and count == 4:
            assert replies[-1]["error_code"] == "permission_denied", replies
            host._approved_permissions |= {PluginPermission.MEMORY_DELETE}
            return _tool(
                "memory_change",
                {"operation": "invalidate", "fact_id": created, "evidence_quote": "member"},
                "delete-approved",
            )
        return _tool("task_control", {"action": "complete", "result": "checked"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Keep actual Memory write and delete approvals")
        assert result.ok and result.data["state"] == "completed"
        assert replies[0]["ok"], replies
        if write_approved:
            assert replies[1]["ok"] and replies[3]["ok"], replies
            assert (await facts.get_fact(created)).status.value == "invalidated"
        else:
            assert replies[1]["error_code"] == "permission_denied", replies
            assert created is None
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("send_approved", [False, True])
async def test_main_recall_needs_existing_send_approval_and_original_social_receipt(
    database, tmp_path, send_approved
):
    provider = FakeLLMProvider()
    permissions = (PluginPermission.MESSAGE_GROUP_SEND,) if send_approved else ()
    env, _, _, source, host = await source_host(database, tmp_path, provider, permissions)
    original = await env.service.execute(
        "send_message", {"text": "One real original message"}, env.context
    )
    assert original["status"] == "succeeded"
    replies = []

    def respond(request):
        if len(provider.requests) == 1:
            return _tool("recall_own_message", {"event_id": original["event_id"]}, "recall")
        replies.append(json.loads(request.messages[-1].content))
        return _tool("task_control", {"action": "complete", "result": "checked"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Recall only the original permitted message")
            repeated = await host.agent.result(result.data["work_id"])
        assert result.ok and repeated.data["text"] == "checked"
        if send_approved:
            assert replies[0]["ok"] and replies[0]["data"]["status"] == "succeeded", replies
        else:
            assert replies[0]["error_code"] == "permission_denied", replies
        assert [name for name, _ in env.bot.calls].count("delete_msg") == int(send_approved)
        assert len(provider.requests) == 2
    finally:
        await host.close_host_resources()


async def test_domain_revocation_during_effect_wait_stops_actual_dispatch(
    database, tmp_path, monkeypatch
):
    provider = FakeLLMProvider()
    _, harness, chat, source, host = await source_host(
        database, tmp_path, provider, (PluginPermission.WEB_READ,)
    )
    harness.settings.web_mode = WebMode.TAVILY
    page = WebSearchSource(
        "original", "Original", "https://example.com/old", "example.com", "", "body"
    )
    web = SimpleNamespace(name="reader", extract=AsyncMock(return_value=page))
    chat._tools._web_provider = web
    replies = []

    def respond(request):
        if len(provider.requests) == 1:
            return _tool("read_webpage", {"url": page.url}, "original-read")
        replies.append(json.loads(request.messages[-1].content))
        return _tool("task_control", {"action": "complete", "result": "checked"}, "finish")

    provider._responder = respond
    waiting, release = asyncio.Event(), asyncio.Event()
    run_effect = chat.run_effect

    async def paused(snapshot, effect):
        if not waiting.is_set():
            waiting.set()
            await release.wait()
        return await run_effect(snapshot, effect)

    monkeypatch.setattr(chat, "run_effect", paused)
    try:
        with host.invocation_scope(host.plugin_id, source):
            task = asyncio.create_task(host.agent.run("Read only with the live original approval"))
            await asyncio.wait_for(waiting.wait(), 3)
            host._approved_permissions = frozenset({PluginPermission.AGENT_RUN})
            release.set()
            result = await task
        assert result.ok
        assert replies[0]["error_code"] == "permission_denied", replies
        web.extract.assert_not_awaited()
    finally:
        release.set()
        await host.close_host_resources()
