"""Deletion boundaries preserve business effects, ownership and cleanup."""

import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env
from tests.unit.test_automation_mutation_boundary import service_for
from tests.unit.test_automation_runtime import _inbound, _script

from qq_ai_bot.adapters.onebot.message_id import parse_message_id
from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.control_context import resolve_execution_identity
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.social.models import SocialError, SocialTarget
from qq_ai_bot.web.bridge_state import BridgeState


@pytest.mark.parametrize("value", ["-123", "0", "123"])
def test_signed_onebot_message_reference(value):
    assert parse_message_id(value) == int(value)


@pytest.mark.parametrize("value", [True, False, "", "+1", " 1", "1 ", "１", "1.0", None])
def test_non_protocol_message_reference_rejected(value):
    with pytest.raises(ValueError):
        parse_message_id(value)


async def test_negative_reply_reference_still_requires_visible_canonical_event(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions.begin() as session:
        event = await session.scalar(select(ChatEventModel))
        event.platform_message_id = "-123"
        event_id = event.id
    target = SocialTarget(kind="space", id=env.space)
    route = await env.router.resolve_send_for_space(env.space)
    context = replace(env.context, visible_event_ids=frozenset({event_id}))
    assert await env.service.reply_reference(event_id, target, route, context) == "-123"
    with pytest.raises(SocialError, match="reply_event_not_visible"):
        await env.service.reply_reference(event_id, target, route, env.context)
    with pytest.raises(SocialError, match="reply_event_unavailable"):
        await env.service.reply_reference(
            event_id, target, route, replace(context, conversation_id="unrelated")
        )


@pytest.mark.parametrize("after_dispatch", [False, True])
async def test_social_failure_classifies_at_actual_gateway_boundary(
    database, tmp_path, monkeypatch, after_dispatch
):
    env = await social_env(database, tmp_path)
    validate = env.router.validate_prepared_connection
    validations = 0

    def check(route):
        nonlocal validations
        validations += 1
        if not after_dispatch and validations == 3:
            raise RuntimeError("connection revoked after claim")
        return validate(route)

    call = AsyncMock(side_effect=RuntimeError("unknown gateway outcome"))
    monkeypatch.setattr(env.router, "validate_prepared_connection", check)
    monkeypatch.setattr(env.service, "_call", call)
    args = {"text": "one effect"}
    result = await env.service.execute("send_message", args, env.context)
    assert result["status"] == ("uncertain" if after_dispatch else "failed")
    assert call.await_count == int(after_dispatch)
    assert await env.service.execute("send_message", args, env.context) == result
    assert call.await_count == int(after_dispatch)


@pytest.mark.parametrize(
    "error", [sqlite3.OperationalError("locked"), OSError("disk"), ValueError("corrupt")]
)
def test_search_cache_fault_is_a_miss(tmp_path, monkeypatch, error):
    cache = BridgeState(tmp_path / "cache.db")

    def fail(*_args):
        raise error

    monkeypatch.setattr(cache, "_access", fail)
    assert cache.access("query") is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_pool_closes_every_unique_owner_after_error(cancel):
    error = asyncio.CancelledError() if cancel else RuntimeError("first close")
    first = SimpleNamespace(close=AsyncMock(side_effect=error))
    last = SimpleNamespace(close=AsyncMock())
    pool = ModelClientPool(injected_profiles={"first": first, "last": last})
    pool._clients[("same-first", 1)] = first
    with pytest.raises(asyncio.CancelledError if cancel else ExceptionGroup):
        await pool.close()
    first.close.assert_awaited_once()
    last.close.assert_awaited_once()


async def test_optional_direct_mode_keeps_complete_frozen_authorized_catalog(tmp_path):
    from tests.support.full_contract_fixture import full_contract

    app = await full_contract(tmp_path, code_enabled=False)
    try:
        from qq_ai_bot.services.main_agent_contract import MainAgentContract

        configured = app.runtime.runner.main_contract
        # An omitted mode must also select direct; production passes Settings explicitly.
        contract = MainAgentContract(app.chat, configured.state)
        full = await contract.definitions()
        assert await configured.model_definitions() == full
        assert await contract.model_definitions() == full
        names = {tool.name for tool in full}
        assert {"terminal_exec", "terminal_write", "terminal_control", "admin_set_config"} <= names
        assert not {"execute_code", "lookup_tools", "run_python"} & names
        assert contract.script_api is None and contract.health()["mode"] == "direct"
        revision = contract.revision
        assert await contract.definitions() == full and contract.revision == revision
    finally:
        await app.close()


@pytest.mark.parametrize("settled", [False, True])
async def test_code_disabled_settles_original_composition_without_new_execution(
    database, tmp_path, settled
):
    import json

    from tests.support.codemode_cases import effect_rows, environment, outer_call

    from qq_ai_bot.codemode.driver import CodeModeDriver

    env = await environment(database, tmp_path)
    outer = outer_call(env, "await yuki_lookup({'original': True})")
    repo, control = env.control.repository, env.control
    parent = outer.identity.operation_id
    await repo.prepare_effect(
        control.lease, control.current["id"], parent, "code_composition", composition={"version": 1}
    )
    arguments = await env.owner.journal.objects.put("{}")
    child = parent + ":original-child"
    await repo.prepare_effect(
        control.lease,
        control.current["id"],
        child,
        "tool",
        invocation={
            "version": 1,
            "revision": 1,
            "parent_effect_key": parent,
            "owner_execution_id": control.current["id"],
            "child_ordinal": 0,
            "feed_index": 0,
            "engine_call_id": "0",
            "tool_id": "lookup",
            "arguments_ref": arguments,
            "dispatch_started": True,
            "budget_admitted": True,
        },
    )
    if settled:
        await env.owner.journal.record_effect(
            child, "accepted", {"result": '{"ok":true,"data":"original"}'}
        )
    before, tools_before, budget_before = await effect_rows(database, control.current["id"])
    env.host.api = None
    env.host.worker = None
    env.host.execute_business = AsyncMock(side_effect=AssertionError("must not execute"))
    result = json.loads(await CodeModeDriver(env.host, outer).resume())
    assert result["replay_forbidden"] and result["status"] == "partial"
    assert result["operations"][0]["operation_id"] == child
    after, tools_after, budget_after = await effect_rows(database, control.current["id"])
    assert after[child] == before[child]
    assert set(after) == set(before)
    assert (tools_after, budget_after) == (tools_before, budget_before)
    env.host.execute_business.assert_not_awaited()


async def test_automation_owner_rebind_resolves_new_actor_without_rewriting_template(database):
    service = service_for(database)
    row = await service.create(
        _script(), actor=ToolActor.from_inbound(_inbound()), conversation_key="private:10001"
    )
    owner = row.canonical_creator_person_id
    async with database.sessions.begin() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == owner)
        )
        binding.external_account_id = "20002"
        await session.flush()
        other = await ensure_person(session, "10001")
        assert other != owner
    async with database.sessions() as session:
        account, permission = await resolve_execution_identity(
            session, service._settings, owner_id=owner
        )
    assert account == "20002" and permission is PermissionLevel.USER
    saved = await service._repository.get(row.id)
    assert saved.creator_user_id == "10001"
    assert saved.canonical_creator_person_id == owner
    assert saved.script == row.script
    with pytest.raises(PermissionError):
        await service.require_manageable(row.id, ToolActor.from_inbound(_inbound("10001")))
    async with database.sessions.begin() as session:
        binding = await session.get(IdentityBindingModel, binding.id)
        binding.status = "disabled"
    async with database.sessions() as session:
        with pytest.raises(PermissionError, match="ambiguous"):
            await resolve_execution_identity(session, service._settings, owner_id=owner)


@pytest.mark.parametrize("revoke", [False, True])
async def test_shared_responses_transport_keeps_physical_attempt_guards_and_hooks(revoke):
    import httpx
    from tests.unit.test_deepseek_responses import _fixture, _request

    from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
    from qq_ai_bot.model_runtime.dispatch_guard import model_dispatch_guard
    from qq_ai_bot.model_runtime.request_accounting import (
        ProviderAttemptCounter,
        after_provider_request,
        before_provider_request,
        current_provider_attempts,
    )

    requests = []

    def respond(request):
        requests.append(request)
        return (
            httpx.Response(503, json={"error": "retry"})
            if len(requests) == 1
            else httpx.Response(200, json=_fixture("text_completed.json"))
        )

    validations = 0

    async def guard():
        nonlocal validations
        validations += 1
        if revoke and validations == 2:
            raise PermissionError("revoked before physical retry")

    before, after = AsyncMock(), AsyncMock()
    counter = ProviderAttemptCounter()
    tokens = [
        (current_provider_attempts, current_provider_attempts.set(counter)),
        (before_provider_request, before_provider_request.set(before)),
        (after_provider_request, after_provider_request.set(after)),
    ]
    try:
        async with httpx.AsyncClient(
            base_url="https://test.invalid/v1/", transport=httpx.MockTransport(respond)
        ) as client:
            provider = DeepSeekResponsesProvider(
                base_url="https://test.invalid/v1",
                api_key="test",
                timeout_seconds=1,
                max_retries=1,
                client=client,
            )
            with model_dispatch_guard(guard):
                if revoke:
                    with pytest.raises(PermissionError):
                        await provider.complete(_request())
                else:
                    await provider.complete(_request())
            assert not client.is_closed
        assert validations == 2
        assert counter.requests == len(requests) == (1 if revoke else 2)
        assert before.await_count == after.await_count == 2
        assert all(request.url.path == "/v1/responses" for request in requests)
        assert counter.unknown_usage_requests == 1
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


@pytest.mark.parametrize("bridge_name", ["DeepSeekSearchBridge", "GeminiSearchBridge"])
async def test_search_close_releases_other_resources_even_if_first_fails(bridge_name):
    from qq_ai_bot.web.deepseek_bridge import DeepSeekSearchBridge
    from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge

    bridge = object.__new__(
        {"DeepSeekSearchBridge": DeepSeekSearchBridge, "GeminiSearchBridge": GeminiSearchBridge}[
            bridge_name
        ]
    )
    bridge.client = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close")))
    bridge.provider = bridge.client
    bridge.media = SimpleNamespace(close=AsyncMock())
    bridge.fallback = SimpleNamespace(close=AsyncMock())
    with pytest.raises(ExceptionGroup):
        await bridge.close()
    if bridge_name == "DeepSeekSearchBridge":
        bridge.media.close.assert_awaited_once()
    bridge.fallback.close.assert_awaited_once()


async def test_file_prepare_race_replays_concurrently_completed_original_receipt(
    database, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from tests.support.workspace_snapshots import snapshot_bytes

    env = await social_env(database, tmp_path)
    artifact = snapshot_bytes(env.store, "race.txt", b"original immutable attachment")
    args = {"artifact_id": artifact["artifact_id"], "attachment_kind": "file"}
    prepare = env.service.transfer.prepare
    completed = None

    @asynccontextmanager
    async def raced_prepare(artifact_id):
        nonlocal completed
        # The competing request completes after the first replay read and before
        # this request obtains the file. Its durable receipt is now authoritative.
        monkeypatch.setattr(env.service.transfer, "prepare", prepare)
        completed = await env.service.execute("send_message", args, env.context)
        raise FileNotFoundError("attachment disappeared after competing completion")
        yield  # pragma: no cover

    monkeypatch.setattr(env.service.transfer, "prepare", raced_prepare)
    result = await env.service.execute("send_message", args, env.context)
    assert result == completed and result["status"] == "succeeded"
    assert sum(action == "upload_group_file" for action, _ in env.bot.calls) == 1
