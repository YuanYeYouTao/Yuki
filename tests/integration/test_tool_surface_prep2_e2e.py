"""Internal decisions read typed outcomes; model-visible results stay byte-identical.

Failure cases were fixed before the implementation (see
docs/operations/tool-surface-prep2-20261107.md):
* web search sources come from the live typed outcome, malformed items are
  skipped, and the same sources from two searches are recorded once;
* a failed search records nothing;
* media over the model's image capability keeps a committed result and only
  replaces an uncommitted one, decided from typed evidence;
* a model ``task_control(accept)`` keeps its visible text, original receipt
  and byte-identical replay;
* Host admission of a plugin invocation creates the same Work without a model
  tool name, and the plugin member directory returns the same items;
* a SELF quote is verified against the bounded result text the model actually
  saw (redacted), so old rows keep their original excerpt and new rows follow
  the projection the model reads.

Every scenario writes normalized evidence under
``test-results/tool-surface-prep2/<label>/`` (label from ``PREP2_LABEL``), so a
run on the base revision and on this change can be compared byte for byte.
"""

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.support.agent_backend import StubAgentBackend
from tests.support.work_runner_helpers import case, run, tool
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from yuki.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from yuki.domain.messages import ChatImage, ChatResponse, ChatTool
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.models import ModelCapability
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects
from yuki.services.agent_tools import ToolRuntime
from yuki.services.execution_sources import recover_source
from yuki.web.models import WebMode, WebSearchResponse, WebSearchSource
from yuki_plugin_sdk.permissions import PluginPermission

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?")
_HEX = re.compile(r"\b[0-9a-f]{64}\b")
_EPOCH = re.compile(r"\b1[0-9]{9}\.[0-9]+\b")
PIXEL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZKsAAAAASUVORK5CYII="
)


def _stable(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    seen: dict[str, str] = {}

    def uuid(match):
        return seen.setdefault(match.group(0), f"<uuid:{len(seen) + 1}>")

    text = _UUID.sub(uuid, text)
    text = _HEX.sub("<sha256>", text)
    text = _TIME.sub("<time>", text)
    return _EPOCH.sub("<epoch>", text) + "\n"


def _write(name, value):
    root = Path(__file__).resolve().parents[2]
    label = os.environ.get("PREP2_LABEL", "current")
    directory = root / "test-results" / "tool-surface-prep2" / label
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(_stable(value), encoding="utf-8")


def _tool_messages(provider):
    seen = {}
    for request in provider.requests:
        for message in request.messages:
            if message.role == "tool":
                seen.setdefault(message.tool_call_id, message.content)
    return seen


async def _receipts(probe):
    async with probe.repository.database.read_session() as session:
        rows = (
            (
                await session.execute(
                    select(effects.c.effect_key, effects.c.state, effects.c.receipt_json).order_by(
                        effects.c.effect_key
                    )
                )
            )
            .mappings()
            .all()
        )
    return {
        row["effect_key"].rsplit(":", 1)[-1]: {
            "state": row["state"],
            "result": json.loads(row["receipt_json"]).get("result"),
            "outcome": json.loads(row["receipt_json"]).get("outcome"),
        }
        for row in rows
    }


class _TypedBackend(StubAgentBackend):
    """Fixture tools publish typed outcomes through the real result projection."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.executed = []

    def parallel_safe(self, name, runtime):
        return False

    def is_side_effecting(self, name, arguments, runtime):
        return name != "web_search"

    async def execute_call(self, invocation):
        self.executed.append(invocation.call.id)
        outcome = self.outcomes[invocation.call.id]
        return (await ToolResultBudgeter(max_characters=None).render(outcome)).text

    def finalize(self, text, runtime):
        return text


def _search(ok, sources):
    return ToolExecutionResult(
        ok=ok,
        data={"query": "q", "sources": sources} if ok else None,
        error_code=None if ok else "search_failed",
        provider_id="core",
        tool_name="web_search",
    )


async def test_web_sources_and_media_budget_follow_typed_outcomes(database, tmp_path, monkeypatch):
    sources = [
        {"url": "https://example.com/a", "title": "A", "snippet": "alpha"},
        {"url": "https://example.com/b", "title": "B"},
        {"url": 7, "title": "bad url"},
        "not-a-source",
    ]
    image = ChatImage(PIXEL, source="tool", tool_handle="fixture")
    outcomes = {
        "search-live": _search(True, sources),
        "search-reused": _search(True, sources),
        "search-failed": _search(False, []),
        "image-committed": ToolExecutionResult(
            ok=True,
            data={"status": "succeeded"},
            mutation_committed=True,
            provider_id="core",
            tool_name="write_fixture",
            images=(image,),
        ),
        "image-uncommitted": ToolExecutionResult(
            ok=True,
            data={"status": "succeeded"},
            mutation_committed=False,
            provider_id="core",
            tool_name="write_fixture",
            images=(image,),
        ),
    }
    responses = [
        ChatResponse("", 0, tool_calls=(tool("web_search", {"query": "q"}, "search-live"),)),
        ChatResponse("", 0, tool_calls=(tool("web_search", {"query": "q"}, "search-reused"),)),
        ChatResponse("", 0, tool_calls=(tool("web_search", {"query": "x"}, "search-failed"),)),
        ChatResponse("", 0, tool_calls=(tool("write_fixture", {}, "image-committed"),)),
        ChatResponse("", 0, tool_calls=(tool("write_fixture", {"n": 2}, "image-uncommitted"),)),
        "done",
    ]
    probe = await case(database, tmp_path, responses)
    probe.backend = _TypedBackend(outcomes)
    probe.runtime = replace(
        probe.runtime,
        fixed_tools=(*probe.runtime.fixed_tools, ChatTool("web_search", "search", {})),
    )
    capabilities = probe.runner._models.capabilities
    monkeypatch.setattr(
        probe.runner._models,
        "capabilities",
        lambda task: capabilities(task) - {ModelCapability.IMAGE_INPUT},
    )
    result = await run(probe)
    visible = _tool_messages(probe.provider)
    evidence = {
        "final_text": result.text,
        "executed": probe.backend.executed,
        "model_visible": visible,
        "portable_search": probe.control.session.progress.get("portable_search"),
        "portable_search_truncated": probe.control.session.progress.get(
            "portable_search_truncated", False
        ),
        "receipts": await _receipts(probe),
    }
    _write("runner-web-and-media", evidence)
    assert evidence["portable_search"] == [
        {"url": "https://example.com/a", "title": "A", "snippet": "alpha"},
        {"url": "https://example.com/b", "title": "B", "snippet": ""},
    ]
    committed = json.loads(visible["image-committed"])
    assert "error" not in committed and committed["media_error"] == "image_capability_unavailable"
    assert json.loads(visible["image-uncommitted"]) == {
        "ok": False,
        "error": "image_capability_unavailable",
        "read": False,
    }


async def test_same_sources_from_two_searches_are_recorded_once(database, tmp_path):
    sources = [{"url": "https://example.com/r", "title": "R", "snippet": "reused"}]
    outcomes = {"search-a": _search(True, sources), "search-b": _search(True, sources)}
    responses = [
        ChatResponse(
            "",
            0,
            tool_calls=(
                tool("web_search", {"query": "same"}, "search-a"),
                tool("web_search", {"query": "same"}, "search-b"),
            ),
        ),
        "done",
    ]
    probe = await case(database, tmp_path, responses)
    probe.backend = _TypedBackend(outcomes)
    probe.runtime = replace(
        probe.runtime,
        fixed_tools=(*probe.runtime.fixed_tools, ChatTool("web_search", "search", {})),
    )
    await run(probe)
    evidence = {
        "executed": probe.backend.executed,
        "model_visible": _tool_messages(probe.provider),
        "portable_search": probe.control.session.progress.get("portable_search"),
    }
    _write("runner-search-dedupe", evidence)
    assert evidence["portable_search"] == [
        {"url": "https://example.com/r", "title": "R", "snippet": "reused"}
    ]


async def test_model_accept_keeps_visible_result_receipt_and_replay(database, tmp_path):
    calls = (tool("manage_task", {"action": "start", "goal": "original goal"}, "accept"),)
    probe = await case(database, tmp_path, [ChatResponse("", 0, tool_calls=calls), "done"])
    probe.control.current = None
    probe.control.source_key = "prep2-accept-source"
    await run(probe)
    async with probe.repository.database.read_session() as session:
        key = await session.scalar(
            select(effects.c.effect_key).where(effects.c.effect_key.like("%:accept"))
        )
    replay = await probe.control.session.journal.effect_result(key, original_tool="task_control")
    receipts = await _receipts(probe)
    evidence = {
        "model_visible": _tool_messages(probe.provider)["accept"],
        "receipt": receipts["accept"],
        "replay": replay,
    }
    _write("model-accept", evidence)
    assert evidence["model_visible"] == evidence["receipt"]["result"] == replay


async def _plugin_host(database, tmp_path, provider, permissions):
    env, harness, chat, _, _ = await _scene(database, tmp_path, provider)
    chat._tools.social_service = env.service
    async with database.read_session() as session:
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
        request_id="prep2-plugin-source",
    )
    source = ToolRuntime(
        inbound=recovered.inbound(event, legacy_conversation_key="bot:80001:group:20001"),
        runtime_config=await chat._runtime_config.snapshot(),
    )
    host = HostPluginContext(
        plugin_id="test.prep2",
        approved_permissions=(PluginPermission.AGENT_RUN, *permissions),
        services=PluginFacadeServices(
            approval_revision="prep2-approved-manifest",
            ledger=harness.ledger,
            social=env.service,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    return env, harness, chat, source, host


async def test_plugin_host_admission_search_and_member_directory(database, tmp_path):
    provider = FakeLLMProvider()
    env, harness, chat, source, host = await _plugin_host(
        database,
        tmp_path,
        provider,
        (PluginPermission.WEB_SEARCH, PluginPermission.GROUP_MEMBERS_READ),
    )
    harness.settings.web_mode = WebMode.TAVILY
    page = WebSearchSource(
        "original", "Original", "https://example.com/plugin", "example.com", "", "body"
    )
    chat._tools._web_provider = SimpleNamespace(
        name="original-provider",
        search=AsyncMock(return_value=WebSearchResponse("query", (page,), "request", 0)),
        extract=AsyncMock(return_value=page),
    )

    def respond(request):
        if len(provider.requests) == 1:
            return _tool("web_search", {"query": "query"}, "search")
        return _tool("manage_task", {"action": "finish", "result": "done"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Research with the approved domain")
            members = await host.groups.list_members(env.space, limit=10)
        row = await WorkRepository(database).get(result.data["work_id"])
        checkpoint = json.loads(row["checkpoint_json"])
        evidence = {
            "result": {"ok": result.ok, "state": result.data["state"]},
            "work": {
                "goal": row["goal"],
                "state": row["state"],
                "model_requests": row["model_requests"],
                "tool_calls": row["tool_calls"],
                "source_key_prefix": row["source_key"].split(":", 1)[0],
            },
            "portable_search": checkpoint.get("progress", {}).get("portable_search"),
            "model_visible": _tool_messages(provider),
            "members": list(members),
        }
        _write("plugin-admission-search-members", evidence)
        assert result.ok and result.data["state"] == "completed"
        assert row["goal"] == "Research with the approved domain"
        assert members
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize(
    ("label", "quote"),
    [
        ("data", "test-10001"),
        ("presentation-only", "get_members"),
        ("absent", "不存在的工具结果"),
    ],
)
async def test_main_self_quote_is_verified_against_the_real_result(
    database, tmp_path, label, quote
):
    from tests.conftest import build_harness, make_settings
    from tests.integration.test_self_automation_delivery import self_actor
    from tests.support.social_identity_cases import social_env
    from tests.unit.test_memory_mutation import _service

    from yuki.conversation.hydrate import primary_alias_for_conversation
    from yuki.conversation.scope import ConversationTurnSnapshot
    from yuki.domain.conversations import ConversationScope, ScopeType
    from yuki.persistence.models import MemoryMutationReceiptModel, MemoryToolReceiptModel
    from yuki.runtime.origin import TurnOrigin
    from yuki.runtime.work_activation import bind_work_activation
    from yuki.runtime.work_control import WorkControl
    from yuki.services.execution_sources import recover_self_source
    from yuki.services.main_agent_contract import MainAgentContract
    from yuki.tool_results.recorder import ToolReceiptRepository
    from yuki.workspace.short_state import ShortState

    env = await social_env(database, tmp_path)
    actor = await self_actor(database, env)
    mutations, _facts, _ledger, _ = _service(database, self_memory_enabled=True)
    write = {
        "target": {"self": True},
        "content": "我实际查询了当前群成员列表。",
        "kind": "episode",
        "quote": quote,
    }
    calls = [
        ("get_members", {"limit": 10, "target": {"space_id": env.space}}),
        ("save_memory", write),
    ]

    def respond(request):
        index = len(provider.requests) - 1
        if index >= len(calls):
            return "NO_REPLY"
        name, arguments = calls[index]
        return _tool(name, arguments, f"original-{index}")

    provider = FakeLLMProvider(responder=respond)
    harness = build_harness(
        database,
        make_settings(
            database.url,
            enabled_groups_csv="20001",
            runtime_work_enabled=True,
            self_memory_enabled=True,
        ),
        provider,
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._tools._memory_mutations = mutations
    chat._tool_receipts = ToolReceiptRepository(database)
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    owner = WorkRepository(database)
    source = {
        "origin": "self_initiative",
        "principal_kind": "self",
        "actor_user_id": "",
        "initiative_run_id": actor.initiative_run_id,
        "conversation_id": actor.conversation_id,
        "generation": 1,
        "space_id": env.space,
        "presence_id": env.presence,
        "bot_user_id": "80001",
        "group_id": "20001",
        "instruction": "记录实际工具经历，然后静默结束",
        "delivery_contract": "return_to_caller",
    }
    lease = await owner.acquire(actor.conversation_id, 1)
    item = await owner.accept(
        lease, source_key=actor.source_key, source=source, goal=source["instruction"]
    )
    recovered = await recover_self_source(
        database, actor.conversation_id, source, request_id=item["id"]
    )
    identity = ConversationScope.group(
        "80001",
        "20001",
        platform=recovered.platform,
        conversation_id=actor.conversation_id,
        presence_id=actor.presence_id,
    )
    async with database.read_session() as session:
        runtime_key = await primary_alias_for_conversation(session, actor.conversation_id)

    async def validate():
        assert await owner.valid(lease)

    control = WorkControl(owner, lease, item["source_key"], source, validate)
    control.current = item
    async with chat._turn_coordinator.background_turn(runtime_key) as token:
        snapshot = ConversationTurnSnapshot(
            actor.conversation_id,
            runtime_key,
            1,
            None,
            token.version,
            identity.key,
            initiative_run_id=actor.initiative_run_id,
        )
        async with bind_work_activation(control):
            await chat.generate_self_initiative(
                trigger=recovered.trigger(),
                runtime=await chat._runtime_config.snapshot(group_id=env.space),
                turn_token=token,
                turn_snapshot=snapshot,
                before_model_request=validate,
                source_runtime=ToolRuntime(
                    inbound=None,
                    actor_context=recovered.actor(item["id"]),
                    conversation_key=runtime_key,
                    execution_id=item["id"],
                    origin=TurnOrigin.SELF_INITIATIVE,
                    initiative_run_id=actor.initiative_run_id,
                    conversation_id=actor.conversation_id,
                    scope_type=ScopeType.GROUP,
                    external_target_id="20001",
                    space_id=env.space,
                    sandbox_source={**source, "work_id": item["id"]},
                ),
            )
    async with database.read_session() as session:
        excerpts = list(
            await session.scalars(
                select(MemoryToolReceiptModel.result_excerpt).order_by(MemoryToolReceiptModel.id)
            )
        )
        mutations_written = len(list(await session.scalars(select(MemoryMutationReceiptModel))))
    visible = _tool_messages(provider)
    written = json.loads(visible["original-1"])
    _write(
        f"self-quote-{label}",
        {
            "quote": quote,
            "members_model_visible": visible["original-0"],
            "save_memory_ok": written.get("ok", True),
            "save_memory_error": written.get("error"),
            "mutations_written": mutations_written,
            "stored_excerpts": excerpts,
        },
    )
    assert visible["original-0"]
    # The excerpt is the bounded text the model actually saw. The cutover removed
    # presentation fields (tool name, provider) from it, so a quote found only in
    # them no longer matches; a quote from the real result data still does.
    assert written.get("ok", True) is (label == "data"), written


async def test_self_reflection_reads_recorded_tool_names_through_the_map(database):
    """Stored rows keep their original names; the reflection model reads the current tool."""
    from tests.support.model_executor import InjectedModelExecutor
    from tests.unit.test_self_initiative_memory import claim, finish, seed

    from yuki.memory.self_reflection.repository import SelfReflectionRepository
    from yuki.memory.self_reflection.service import SelfReflectionService
    from yuki.persistence.models import MemoryToolReceiptModel
    from yuki.tool_results.recorder import ToolReceiptRepository

    mutations, _facts, event, run_id = await seed(database)
    for call, name in (
        ("call-1", "web_read"),
        ("call-2", "send_message"),
        ("call-3", "web_search"),
    ):
        await ToolReceiptRepository(database).record_receipt(
            provider_id="core",
            tool_name=name,
            success=True,
            error_category=None,
            initiative_run_id=run_id,
            execution_id="execution-1",
            tool_call_id=call,
            canonical_conversation_id=event.canonical_conversation_id,
            result_excerpt=f"{name} 原始结果",
        )
    await finish(database, run_id)
    (batch,) = await claim(database)
    reflection = SelfReflectionService(
        settings=make_settings(database.url, self_memory_enabled=True),
        repository=SelfReflectionRepository(database),
        mutations=mutations,
        models=InjectedModelExecutor(FakeLLMProvider()),
    )
    payload, _, _ = await reflection._input(batch)
    async with database.read_session() as session:
        stored = list(
            await session.scalars(
                select(MemoryToolReceiptModel.tool_name).order_by(MemoryToolReceiptModel.id)
            )
        )
    shown = [(item.tool_name, item.result_excerpt) for item in payload.tool_receipts]
    _write("self-reflection-tool-names", {"stored": stored, "shown": shown})
    assert stored == ["web_read", "send_message", "web_search"]


async def test_host_admission_writes_no_model_receipt_and_raises_real_failures(database, tmp_path):
    probe = await case(database, tmp_path, [])
    control = probe.control
    control.current = None
    control.source_key = "prep2-host-source"
    with pytest.raises(ValueError, match="work_goal_required"):
        await control.admit_goal("   ")
    assert control.current is None
    await control.admit_goal("Host goal")
    assert control.current["goal"] == "Host goal" and control.current["state"] == "running"
    assert await _receipts(probe) == {}
