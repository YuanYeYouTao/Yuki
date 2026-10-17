"""Each actual plugin Memory call uses live domain scopes, including evidence."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.integration.test_plugin_agent_domain_grants import source_host
from tests.unit.test_history_dispatch_ownership import _tool

from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.enums import MemoryEvidenceRelation, MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.persistence.models import ChatEventModel
from yuki_plugin_sdk.permissions import PluginPermission


def grants(kind):
    return tuple(
        permission
        for key, permission in (
            ("person", PluginPermission.MEMORY_PERSON_READ),
            ("group", PluginPermission.MEMORY_GROUP_READ),
        )
        if kind in {key, "both"}
    )


async def memory_scene(
    database, tmp_path, provider, initial, *, personal_content="Original personal fact"
):
    env, _, chat, source, host = await source_host(database, tmp_path, provider, grants(initial))
    await chat._people.observe(
        user_id="10001",
        nickname="member",
        group_id="20001",
        space_id=env.space,
        expected_person_id=env.person,
    )
    async with database.sessions() as session:
        event_id = await session.scalar(select(ChatEventModel.id))
    person = await chat._memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="10001",
            canonical_subject_person_id=env.person,
            memory_key="plugin-live:person",
            category="test",
            content=personal_content,
            source_type=MemorySourceType.EXPLICIT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event_id,
            source_speaker_user_id="10001",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            excerpt="original evidence",
        ),
    )
    group = await chat._memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.GROUP,
            group_id="20001",
            canonical_subject_space_id=env.space,
            memory_key="plugin-live:group",
            category="test",
            content="Original group fact",
            source_type=MemorySourceType.EXPLICIT,
        ),
    )
    return chat, source, host, person, group


@pytest.mark.parametrize(
    ("initial", "live", "view", "allowed"),
    [
        ("none", "none", "evidence", False),
        ("person", "none", "fact", False),
        ("person", "group", "fact", False),
        ("person", "group", "evidence", False),
        ("group", "group", "evidence", False),
        ("person", "person", "evidence", True),
        ("none", "person", "fact", True),
        ("none", "person", "evidence", True),
    ],
)
async def test_person_fact_and_evidence_follow_live_plugin_read_scope(
    database, tmp_path, initial, live, view, allowed
):
    provider = FakeLLMProvider()
    _, source, host, person, _ = await memory_scene(database, tmp_path, provider, initial)
    replies = []

    def respond(request):
        if len(provider.requests) == 1:
            host._approved_permissions = frozenset({PluginPermission.AGENT_RUN, *grants(live)})
            return _tool("read_memory", {"view": view, "fact_id": person.id}, "original-memory")
        replies.append(json.loads(request.messages[-1].content))
        return _tool("task_control", {"action": "complete", "result": "checked"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Use only the current approved Memory scope")
        assert result.ok and result.data["state"] == "completed"
        assert replies[0]["ok"] is allowed, replies
        if not allowed:
            assert replies[0]["error_code"] in {"permission_denied", "memory_not_found"}
            assert "Original personal fact" not in json.dumps(replies)
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize(
    ("initial", "live"), [("person", "group"), ("group", "person"), ("person", "none")]
)
async def test_memory_search_uses_only_current_plugin_scope(database, tmp_path, initial, live):
    provider = FakeLLMProvider()
    _, source, host, person, group = await memory_scene(database, tmp_path, provider, initial)
    replies = []

    def respond(request):
        if len(provider.requests) == 1:
            host._approved_permissions = frozenset({PluginPermission.AGENT_RUN, *grants(live)})
            return _tool("search_memory", {"query": "Original"}, "search-current-memory")
        replies.append(json.loads(request.messages[-1].content))
        return _tool("task_control", {"action": "complete", "result": "checked"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Search only the current approved Memory scope")
        assert result.ok
        if live == "none":
            assert replies[0]["error_code"] == "permission_denied", replies
        else:
            assert replies[0]["ok"], replies
            assert {row["fact_id"] for row in replies[0]["data"]["memories"]} == {
                group.id if live == "group" else person.id
            }
    finally:
        await host.close_host_resources()


async def test_parallel_memory_reads_keep_local_runtime_scopes(database, tmp_path, monkeypatch):
    from qq_ai_bot.plugin_host import main_turn

    provider = FakeLLMProvider()
    _, source, host, person, group = await memory_scene(database, tmp_path, provider, "both")
    backends, replies = [], {}
    backend = main_turn.MainAgentBackend

    def capture(*args, **kwargs):
        actual = backend(*args, **kwargs)
        backends.append(actual)
        return actual

    monkeypatch.setattr(main_turn, "MainAgentBackend", capture)

    def respond(request):
        if len(provider.requests) == 1:
            host._approved_permissions = frozenset({PluginPermission.AGENT_RUN, *grants("group")})
            return ChatResponse(
                "",
                0,
                tool_calls=tuple(
                    ToolCall(
                        key,
                        ToolFunction("read_memory", json.dumps({"view": view, "fact_id": fact_id})),
                    )
                    for key, view, fact_id in (
                        ("group", "fact", group.id),
                        ("person", "fact", person.id),
                        ("evidence", "evidence", person.id),
                    )
                ),
            )
        replies.update(
            {m.tool_call_id: json.loads(m.content) for m in request.messages if m.role == "tool"}
        )
        return _tool("task_control", {"action": "complete", "result": "checked"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Read actual Memory scopes in parallel")
        assert result.ok
        assert replies["group"]["ok"], replies
        assert not replies["person"]["ok"] and not replies["evidence"]["ok"], replies
        assert backends[0]._runtime.memory_allowed_scopes == (
            MemoryScopeType.PERSON,
            MemoryScopeType.PERSON_GROUP,
            MemoryScopeType.GROUP,
        )
    finally:
        await host.close_host_resources()


async def test_memory_live_scope_does_not_change_original_result_artifact_proof(database, tmp_path):
    from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository

    provider = FakeLLMProvider()
    chat, source, host, person, _ = await memory_scene(
        database,
        tmp_path,
        provider,
        "none",
        personal_content="Original personal fact " * 2000,
    )
    chat._settings.tooling_result_token_budget = 256
    chat._settings.agent_tool_result_max_characters = 100000
    source = replace(source, runtime_config=await chat._runtime_config.snapshot())
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "results", retention_seconds=60
    )
    replies = []

    def respond(request):
        count = len(provider.requests)
        if count == 1:
            host._approved_permissions = frozenset({PluginPermission.AGENT_RUN, *grants("person")})
            return _tool(
                "read_memory", {"view": "fact", "fact_id": person.id}, "original-full-fact"
            )
        replies.append(json.loads(request.messages[-1].content))
        if count == 2:
            return _tool(
                "read_tool_artifact",
                {
                    "handle": replies[-1]["artifact_handle"],
                    "operation": "get",
                    "path": ["memory", "content"],
                    "offset": 0,
                    "limit": 100,
                },
                "read-original-artifact",
            )
        return _tool("task_control", {"action": "complete", "result": "checked"}, "finish")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run(
                "Read the current approved fact through its original artifact"
            )
        assert result.ok
        assert "artifact_handle" in replies[0], replies
        assert replies[1]["ok"], replies
        assert "Original personal fact" in json.dumps(replies[1])
    finally:
        await host.close_host_resources()
