"""Real Main/Work receipts keep search and explicit historical page reads separate."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.integration.test_codemode_lifecycle_recovery import FakeEngine
from tests.support.automation_unified_delivery_helpers import sent, setup_run
from tests.unit.test_app_messages import admin as admin
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.codemode.driver import CodeHost
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    ToolArtifactModel,
    WebSearchRunModel,
    WebSearchSourceModel,
)
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.models import WebMode, WebSearchResponse, WebSearchSource
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission


def _web_fixture(page_fails):
    source = WebSearchSource(
        "original-source",
        "Original source",
        "https://source.example/search",
        "source.example",
        "original snippet",
        "original upstream evidence",
    )
    web = SimpleNamespace(
        name="fixture",
        search=AsyncMock(
            return_value=WebSearchResponse(
                "research",
                (source,),
                "original-search",
                0,
                provider="fixture",
                provider_summary="upstream synthesis",
            )
        ),
        extract=AsyncMock(side_effect=WebSearchError("extract_failed", "original reader failure"))
        if page_fails
        else AsyncMock(return_value=replace(source, relevant_content="historical page")),
    )
    return web


def _enable_fake_code(chat, settings, calls, monkeypatch):
    """Real Runner/CodeHost/Core invocations; only the Python VM is emulated."""
    settings.code_mode_enabled = True
    runner = chat.runtime.runner
    runner.main_contract = MainAgentContract(chat, runner.main_contract.state, code_enabled=True)
    runner.code_mode_settings = settings
    original = runner._code_host

    def create(*args, **kwargs):
        host = original(*args, **kwargs)
        assert isinstance(host, CodeHost)
        host.worker = SimpleNamespace(execution_digest=lambda: "search-separation-fake-engine")
        host.engine_factory = lambda *_args: FakeEngine(calls=calls)
        return host

    monkeypatch.setattr(runner, "_code_host", create)


@pytest.mark.parametrize("page_fails", [False, True])
async def test_code_composition_calls_real_search_reader_and_social_once(
    database, tmp_path, monkeypatch, page_fails
):
    case = await setup_run(database, tmp_path, mode="silent")
    case.settings.web_mode = WebMode.TAVILY
    web = _web_fixture(page_fails)
    case.chat._tools._web_provider = web
    old_url = "https://archive.example/code-selected-history"
    calls = [
        ("yuki_web_search", {"query": "research"}),
        ("yuki_read_webpage", {"url": old_url}),
        ("yuki_send_message", {"text": "保留搜索结果并完成交付。"}),
    ]
    _enable_fake_code(case.chat, case.settings, calls, monkeypatch)
    script = "\n".join(f"await {name}({args!r})" for name, args in calls)

    def respond(_request):
        if len(case.provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "accept-original-work",
                        ToolFunction("task_control", '{"action":"accept","goal":"完成研究并交付"}'),
                    ),
                ),
            )
        if len(case.provider.requests) == 2:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "original-composition",
                        ToolFunction("execute_code", json.dumps({"code": script})),
                    ),
                ),
            )
        return "完成。"

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, result
    assert len(case.provider.requests) == 3
    assert web.search.await_count == 1, [
        message.content
        for request in case.provider.requests
        for message in request.messages
        if message.role == "tool"
    ]
    web.extract.assert_awaited_once_with(old_url, "读取用户指定的网页")
    assert len(sent(case.env)) == 1
    assert {tool.name for tool in case.provider.requests[0].tools}.isdisjoint({"yuki_web_search"})
    from qq_ai_bot.runtime.work_schema_v1 import effects

    async with database.sessions() as session:
        rows = (await session.execute(select(effects))).mappings().all()
    children = [
        row
        for row in rows
        if json.loads(row["receipt_json"]).get("outcome", {}).get("tool")
        in {"web_search", "read_webpage", "send_message"}
    ]
    assert len(children) == 3 and all(row["state"] in {"accepted", "failed"} for row in children)
    assert sum(row["kind"] == "code_composition" for row in rows) == 1


@pytest.mark.parametrize("page_fails", [False, True])
async def test_app_http_search_historical_read_and_replay_never_repeat_send(
    app_env, database, tmp_path, page_fails
):
    env = app_env
    chat = env.harness.processor._chat
    env.harness.settings.web_mode = WebMode.TAVILY
    web = _web_fixture(page_fails)
    chat._tools._web_provider = web
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "app-results", retention_seconds=60
    )
    old_url = "https://archive.example/app-selected-history"

    def respond(_request):
        index = len(env.provider.requests)
        if index == 1:
            call = ToolCall("original-search", ToolFunction("web_search", '{"query":"research"}'))
        elif index == 2:
            call = ToolCall(
                "original-read", ToolFunction("read_webpage", json.dumps({"url": old_url}))
            )
        elif index == 3:
            call = ToolCall(
                "original-send", ToolFunction("send_message", '{"text":"App research delivered"}')
            )
        else:
            return "完成。"
        return ChatResponse("", 0, tool_calls=(call,))

    env.provider._responder = respond
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    body = {"request_id": str(uuid4()), "text": "请完成研究", "attachments": []}
    headers = bearer(device["token"])
    response = await env.client.post("/client/v1/messages", json=body, headers=headers)
    assert response.status_code == 200, response.text
    await drain(env)
    assert len(env.provider.requests) == 4
    web.search.assert_awaited_once()
    web.extract.assert_awaited_once_with(old_url, "读取用户指定的网页")
    replay = await env.client.post("/client/v1/messages", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    await drain(env)
    assert len(env.provider.requests) == 4
    web.search.assert_awaited_once()
    web.extract.assert_awaited_once()
    async with database.sessions() as session:
        assert list(
            await session.scalars(
                select(ChatEventModel.content).where(
                    ChatEventModel.canonical_conversation_id
                    == response.json()["data"]["conversation_id"],
                    ChatEventModel.direction == "outbound",
                )
            )
        ) == ["App research delivered"]
        assert await session.scalar(select(func.count()).select_from(WebSearchRunModel)) == 0


@pytest.mark.parametrize("work_enabled", [False, True])
@pytest.mark.parametrize("page_fails", [False, True])
async def test_main_search_then_historical_read_then_social_send(
    database, tmp_path, work_enabled, page_fails
):
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=work_enabled)
    case.settings.web_mode = WebMode.TAVILY
    old_url = "https://archive.example/older-conversation-link"
    source = WebSearchSource(
        "original-source",
        "Original source",
        "https://source.example/search",
        "source.example",
        "already returned snippet",
        "already returned evidence",
    )
    web = SimpleNamespace(
        name="fixture",
        search=AsyncMock(
            return_value=WebSearchResponse(
                "research",
                (source,),
                "original-search",
                0,
                provider="fixture",
                provider_summary="upstream synthesis",
            )
        ),
        extract=AsyncMock(side_effect=WebSearchError("extract_failed", "original reader failure"))
        if page_fails
        else AsyncMock(
            return_value=replace(source, url=old_url, relevant_content="historical page")
        ),
    )
    case.chat._tools._web_provider = web
    case.chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "results", retention_seconds=60
    )

    def respond(_request):
        index = len(case.provider.requests)
        if index == 1:
            call = ToolCall("original-search", ToolFunction("web_search", '{"query":"research"}'))
        elif index == 2:
            call = ToolCall(
                "explicit-read", ToolFunction("read_webpage", json.dumps({"url": old_url}))
            )
        elif index == 3:
            call = ToolCall(
                "original-send", ToolFunction("send_message", '{"text":"已保留搜索结果。"}')
            )
        else:
            return "完成。"
        return ChatResponse("", 0, tool_calls=(call,))

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, result
    assert len(case.provider.requests) == 4
    web.search.assert_awaited_once()
    web.extract.assert_awaited_once_with(old_url, "读取用户指定的网页")
    assert len(sent(case.env)) == 1
    results = [
        json.loads(message.content)
        for request in case.provider.requests
        for message in request.messages
        if message.role == "tool" and message.tool_call_id == "explicit-read"
    ]
    assert results and results[-1]["ok"] is not page_fails
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebSearchRunModel)) == 0
        assert await session.scalar(select(func.count()).select_from(WebSearchSourceModel)) == 0
        rows = list(await session.scalars(select(ToolArtifactModel)))
        assert any(row.tool_name == "web_search" for row in rows)
        assert any(row.tool_name == "read_webpage" for row in rows) is not page_fails


async def test_plugin_search_grant_does_not_grant_page_read_and_revocation_still_applies(
    database, tmp_path
):
    case = await setup_run(database, tmp_path, mode="silent")
    source = WebSearchSource(
        "source", "Source", "https://source.example/page", "source.example", "", ""
    )
    web = SimpleNamespace(
        search=AsyncMock(return_value=WebSearchResponse("query", (source,), None, 0)),
        extract=AsyncMock(return_value=source),
    )
    host = HostPluginContext(
        plugin_id="test.web-separation",
        approved_permissions=(PluginPermission.WEB_SEARCH,),
        services=PluginFacadeServices(web_provider=web),
    )
    invocation = PluginInvocation(
        plugin_id=host.plugin_id,
        origin=case.actor.origin,
        actor_user_id=case.actor.user_id,
        actor_person_id=case.actor.person_id,
        bot_user_id=case.actor.bot_user_id,
        actor=case.actor,
        source_event_id=case.actor.event_id,
        conversation_id=case.actor.conversation_id,
        runtime_config=await case.chat._runtime_config.snapshot(),
    )
    with host.bind(invocation):
        assert (await host.web.search("query")).ok
        with pytest.raises(PluginPermissionError, match=r"web\.read"):
            await host.web.read(source.url)
    web.search.assert_awaited_once()
    web.extract.assert_not_awaited()
    reader = HostPluginContext(
        plugin_id=host.plugin_id,
        approved_permissions=(PluginPermission.WEB_READ,),
        services=PluginFacadeServices(web_provider=web),
    )
    with reader.bind(invocation):
        assert (await reader.web.read(source.url)).ok
        await reader.close_host_resources()
        with pytest.raises(PluginPermissionError, match="closed"):
            await reader.web.read(source.url)
    web.extract.assert_awaited_once()


async def test_retired_source_rows_still_expire_with_cascading_sources(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        for age in (20, 1):
            row = WebSearchRunModel(
                conversation_key=case.actor.conversation_id,
                trigger_message_id="original",
                query="original query",
                trigger_event_id=case.actor.event_id,
                provider="historical",
                partial_failure=False,
                created_at=now - timedelta(days=age),
                canonical_conversation_id=case.actor.conversation_id,
            )
            session.add(row)
            await session.flush()
            session.add(
                WebSearchSourceModel(
                    run_id=row.id,
                    ordinal=1,
                    title="Original source",
                    url=f"https://example.com/{age}",
                    domain="example.com",
                    snippet="original",
                    created_at=row.created_at,
                )
            )
    assert await WebSearchSourceRepository(database).cleanup_expired(retention_days=7, now=now) == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebSearchRunModel)) == 1
        assert await session.scalar(select(func.count()).select_from(WebSearchSourceModel)) == 1
    assert await PeopleRepository(database).delete_person_id(case.actor.person_id)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebSearchRunModel)) == 0
        assert await session.scalar(select(func.count()).select_from(WebSearchSourceModel)) == 0


async def test_retired_search_settings_follow_existing_unknown_key_behavior(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    for key in ("web.extract_max_results", "web.source_max_runs_per_conversation"):
        result = await case.chat._runtime_config.set_override(
            key,
            3,
            scope_type="global",
            scope_id="",
            actor_user_id="9000",
            trigger_message_id="original-admin-request",
        )
        assert not result.success and result.error_category == "unknown_key"
    config = await case.chat._runtime_config.snapshot()
    assert config.web.search_max_results > 0 and config.web.source_retention_days > 0
