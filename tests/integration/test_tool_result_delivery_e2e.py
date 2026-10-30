"""Main/Plugin -> real result delivery -> public reader -> next model input.

Inputs and failure expectations are fixed before the implementation:
* small web search/page bodies stay inline, with an unchanged complete archive;
* an explicit token budget overrides the agent budget in either direction;
* public get defaults to the logical root, honors Unicode offsets and requested
  pages above the former 32000 cap, and never recursively archives its pages;
* Memory keeps every selected fact and its tail before presentation; an oversized
  first fact remains a successful original result, not an unavailable read;
* runtime configuration accepts positive budgets below/above the old caps and
  rejects zero/negative values without replacing the last valid override.

The fixed external web/model replies are protocol boundaries. The real Main
binding, domain services, SQLite, result budget and authorized Artifact reader
remain in the path. Evidence includes actual next-request tool messages and
independently reconstructed values, rather than a direct Repository read.
"""

import base64
import hashlib
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_plugin_memory_live_grants import memory_scene
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.unit.test_history_dispatch_ownership import _tool

from yuki.automation.models import RunStatus
from yuki.domain.messages import ChatImage, ChatResponse, ToolCall, ToolFunction
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.enums import MemoryScopeType, MemorySourceType
from yuki.memory.models import MemoryFactCreate
from yuki.persistence.models import ToolArtifactModel
from yuki.tool_results.access import ArtifactAccess
from yuki.tool_results.artifacts import ToolArtifactRepository
from yuki.web.models import WebMode, WebSearchResponse, WebSearchSource


async def _configure(chat, *, characters, tokens=None):
    """Use the real configuration owner, including its persisted overrides."""
    changes = []
    for key, value in (
        ("agent.tool_result_max_characters", characters),
        ("tooling.result_token_budget", tokens),
    ):
        if value is None:
            continue
        change = await chat._runtime_config.set_override(
            key,
            value,
            scope_type="global",
            scope_id="",
            actor_user_id="10001",
            trigger_message_id="tool-delivery-config",
        )
        assert change.success, change
        changes.append({"key": key, "value": value, "change_id": change.change_id})
    return changes


def _last_result(request, call_id):
    message = next(
        message
        for message in reversed(request.messages)
        if message.role == "tool" and message.tool_call_id == call_id
    )
    return json.loads(message.content)


async def _originals(database, root):
    async with database.read_session() as session:
        rows = (await session.scalars(select(ToolArtifactModel))).all()
        material = []
        for row in rows:
            path = root / row.relative_path
            material.append(
                {
                    "id": row.handle_id,
                    "relative_path": row.relative_path,
                    "work_id": row.work_id,
                    "effect_key": row.effect_key,
                    "provider_id": row.provider_id,
                    "tool_name": row.tool_name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "length_bytes": path.stat().st_size,
                    "access": json.loads(row.access_json),
                }
            )
        return material


async def test_small_web_results_reach_the_next_main_request(
    database, tmp_path, tool_delivery_evidence
):
    evidence_dir, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=False)
    case.settings.web_mode = WebMode.TAVILY
    case.settings.context_compact_trigger_tokens = 500000
    root = tmp_path / "artifacts"
    case.chat._tool_artifacts = ToolArtifactRepository(database, root, retention_seconds=600)
    original = "完整研究正文 " * 500 + "WEB_BODY_TAIL"
    source = WebSearchSource(
        "source-original",
        "Original page",
        "https://example.com/original",
        "example.com",
        "",
        original,
    )
    web = SimpleNamespace(
        search=AsyncMock(return_value=WebSearchResponse("research", (source,), None, 0)),
        extract=AsyncMock(return_value=source),
    )
    case.chat._tools._web_provider = web
    seen = []

    def respond(request):
        if len(case.provider.requests) == 1:
            return _tool("web_search", {"query": "research"}, "search-original")
        call_id = "search-original" if len(case.provider.requests) == 2 else "page-original"
        seen.append(_last_result(request, call_id))
        if len(case.provider.requests) == 2:
            return _tool("read_webpage", {"url": source.url}, "page-original")
        return "research delivered"

    case.provider._responder = respond
    evidence.update({"scenario": "small-web-inline", "original": original, "model_results": seen})
    try:
        case.clock.advance(2)
        result = await case.dispatch()
        assert result.status is RunStatus.SUCCEEDED, result
        assert len(seen) == 2
        for payload in seen:
            assert payload["ok"] and not payload.get("truncated", False), payload
            assert payload["data"]["sources"][0]["relevant_content"] == original
            assert payload["evidence_state"]
        async with database.read_session() as session:
            rows = (await session.scalars(select(ToolArtifactModel))).all()
            assert len(rows) == 2
            by_tool = {payload["tool_name"]: payload for payload in seen}
            for row in rows:
                archived = json.loads((root / row.relative_path).read_text(encoding="utf8"))
                assert archived["data"]["sources"][0]["relevant_content"] == original
                assert archived["evidence_state"] == by_tool[row.tool_name]["evidence_state"]
                access = json.loads(row.access_json)
                assert access["conversation_id"] == case.env.context.conversation_id
                assert access["actor_person_id"] == case.env.person
                assert access["generation"] == 1
        evidence["originals"] = await _originals(database, root)
    finally:
        evidence["evidence_dir"] = str(evidence_dir)


@pytest.mark.parametrize(
    ("agent_budget", "token_budget", "body_length", "requested_limit"),
    [
        (1024, 2048, 15000, None),
        (20000, 1024, 10000, None),
        (64000, None, 70000, 50000),
        (64000, None, 70000, None),
    ],
)
async def test_public_reader_uses_effective_budget_and_exact_unicode_continuation(
    database,
    tmp_path,
    tool_delivery_evidence,
    agent_budget,
    token_budget,
    body_length,
    requested_limit,
):
    _, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=False)
    case.settings.web_mode = WebMode.TAVILY
    changes = await _configure(case.chat, characters=agent_budget, tokens=token_budget)
    case.settings.context_compact_trigger_tokens = 500000
    budget = token_budget * 4 if token_budget is not None else agent_budget
    root = tmp_path / "artifacts"
    case.chat._tool_artifacts = ToolArtifactRepository(database, root, retention_seconds=600)
    original = ('原文ß😀"\\\n' * (body_length // 7 + 1))[:body_length] + "UNICODE_TAIL"
    source = WebSearchSource(
        "S1", "Original", "https://example.com/full", "example.com", "", original
    )
    case.chat._tools._web_provider = SimpleNamespace(
        search=AsyncMock(return_value=WebSearchResponse("full", (source,), None, 0))
    )
    seen, pieces = [], []
    call_id, handle = "search-full", None

    def respond(request):
        nonlocal call_id, handle
        if len(case.provider.requests) == 1:
            return _tool("web_search", {"query": "full"}, call_id)
        payload = _last_result(request, call_id)
        seen.append(payload)
        assert len(json.dumps(payload, ensure_ascii=False)) <= budget
        if handle is None:
            assert payload["ok"] and payload["truncated"], payload
            handle = payload["artifact_handle"]
            offset = 0
        else:
            assert payload["ok"], payload
            page = payload["data"]
            assert page["mode"] == "json" and page["offset_unit"] == "characters"
            assert page["path"] == ["sources", 0, "relevant_content"]
            assert page["offset"] == sum(len(piece) for piece in pieces)
            pieces.append(page["value"])
            offset = page["next_offset"]
            if offset is None:
                return "original reconstructed"
            assert offset == sum(len(piece) for piece in pieces)
        call_id = f"read-{len(pieces)}"
        args = {"handle": handle, "path": ["sources", 0, "relevant_content"], "offset": offset}
        if requested_limit is not None:
            args["limit"] = requested_limit
        return _tool("read_tool_artifact", args, call_id)

    case.provider._responder = respond
    evidence.update(
        {
            "scenario": "public-reader-continuation",
            "budget": budget,
            "config_changes": changes,
            "model_results": seen,
            "pieces": pieces,
            "input": original,
            "requested_limit": requested_limit,
        }
    )
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, result
    assert "".join(pieces) == original
    if budget == 64000:
        assert len(pieces[0]) > 32000
    async with database.read_session() as session:
        assert len((await session.scalars(select(ToolArtifactModel))).all()) == 1
    evidence["originals"] = await _originals(database, root)
    evidence["reconstructed_sha256"] = hashlib.sha256("".join(pieces).encode()).hexdigest()


async def test_oversized_first_web_source_exposes_body_and_exact_continuation(
    database, tmp_path, tool_delivery_evidence
):
    """A source too large for a whole array item still exposes its real body, separately."""
    _, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=False)
    case.settings.web_mode = WebMode.TAVILY
    case.settings.context_compact_trigger_tokens = 500000
    changes = await _configure(case.chat, characters=4096)
    root = tmp_path / "first-source-artifacts"
    case.chat._tool_artifacts = ToolArtifactRepository(database, root, retention_seconds=600)
    original = "正文开头 " + "网页资料ß😀" * 1500 + " WEB_SOURCE_TAIL"
    source = WebSearchSource(
        "first-original",
        "Original page",
        "https://example.com/large",
        "example.com",
        "",
        original,
    )
    case.chat._tools._web_provider = SimpleNamespace(
        search=AsyncMock(return_value=WebSearchResponse("large source", (source,), None, 0))
    )
    seen, pieces = [], []
    call_id, handle = "first-large-source", None

    def respond(request):
        nonlocal call_id, handle
        if len(case.provider.requests) == 1:
            return _tool("web_search", {"query": "large source"}, call_id)
        payload = _last_result(request, call_id)
        seen.append(payload)
        assert payload["ok"], payload
        assert len(json.dumps(payload, ensure_ascii=False)) <= 4096
        if handle is None:
            assert payload["truncated"]
            handle = payload["artifact_handle"]
            assert payload["data"]["sources"] == []
            assert payload["preview"]["path"] == ["sources", 0]
            pieces.append(payload["preview"]["value"]["relevant_content"])
            assert pieces[0] and original.startswith(pieces[0])
            continuation = next(
                item
                for item in payload["read_more"]
                if item["path"] == ["sources", 0, "relevant_content"]
            )
            assert continuation["offset"] == len(pieces[0])
        else:
            page = payload["data"]
            assert page["path"] == ["sources", 0, "relevant_content"]
            assert page["offset"] == sum(map(len, pieces))
            pieces.append(page["value"])
            if page["next_offset"] is None:
                return "read the complete source tail"
            continuation = {"handle": handle, "path": page["path"], "offset": page["next_offset"]}
        call_id = f"source-page-{len(pieces)}"
        return _tool("read_tool_artifact", continuation, call_id)

    case.provider._responder = respond
    evidence.update(
        {
            "scenario": "oversized-first-web-source",
            "config_changes": changes,
            "original": original,
            "model_results": seen,
            "pieces": pieces,
        }
    )
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, result
    assert "".join(pieces) == original
    evidence["originals"] = await _originals(database, root)


@pytest.mark.parametrize(("agent_budget", "token_budget"), [(1024, 2048), (20000, 1024)])
async def test_plugin_memory_keeps_complete_selected_facts_before_delivery(
    database, tmp_path, tool_delivery_evidence, agent_budget, token_budget, caplog
):
    _, evidence = tool_delivery_evidence
    caplog.set_level("INFO", logger="yuki.services.agent_tools")
    provider = FakeLLMProvider()
    chat, source, host, first, _ = await memory_scene(
        database, tmp_path, provider, "person", personal_content="observatory " + "记忆正文" * 300
    )
    facts = [first]
    for index in range(2):
        facts.append(
            await chat._memories.remember(
                MemoryFactCreate(
                    scope_type=MemoryScopeType.PERSON,
                    subject_user_id="10001",
                    canonical_subject_person_id=source.inbound.person_id,
                    memory_key=f"observatory:{index}",
                    category="test",
                    content=f"observatory {index} " + "完整尾项" * 300 + f" MEMORY_TAIL_{index}",
                    source_type=MemorySourceType.EXPLICIT,
                )
            )
        )
    changes = await _configure(chat, characters=agent_budget, tokens=token_budget)
    source = replace(source, runtime_config=await chat._runtime_config.snapshot())
    root = tmp_path / "memory-artifacts"
    chat._tool_artifacts = ToolArtifactRepository(database, root, retention_seconds=600)
    seen, values = [], []
    call_id, handle = "memory-search", None

    def respond(request):
        nonlocal call_id, handle
        if len(provider.requests) == 1:
            return _tool("search_memory", {"query": "observatory", "limit": 100}, call_id)
        payload = _last_result(request, call_id)
        seen.append(payload)
        assert payload["ok"], payload
        if call_id == "memory-search":
            if not payload.get("truncated"):
                values.extend(payload["data"]["memories"])
                return _tool("task_control", {"action": "complete", "result": "read all"}, "done")
            handle = payload["artifact_handle"]
            values.extend(payload["data"]["memories"])
            continuation = next(
                item for item in payload["read_more"] if item["path"] == ["memories"]
            )
            assert continuation["handle"] == handle
            assert continuation["offset"] == len(values)
            call_id = f"memory-page-{len(values)}"
            return _tool("read_tool_artifact", continuation, call_id)
        else:
            page = payload["data"]
            values.extend(page["value"])
            offset = page.get("next_offset")
            if offset is None:
                return _tool("task_control", {"action": "complete", "result": "read all"}, "done")
        call_id = f"memory-page-{len(values)}"
        return _tool(
            "read_tool_artifact",
            {"handle": handle, "path": ["memories"], "offset": offset},
            call_id,
        )

    provider._responder = respond
    evidence.update(
        {
            "scenario": "plugin-memory-selected-facts",
            "config_changes": changes,
            "model_results": seen,
            "read_facts": values,
            "input_facts": [{"fact_id": fact.id, "content": fact.content} for fact in facts],
        }
    )
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Read every matching original observatory fact")
        assert result.ok, result
        assert {row["fact_id"]: row["content"] for row in values} == {
            fact.id: fact.content for fact in facts
        }
        assert any("outcome=success result_count=3" in record.message for record in caplog.records)
        if handle is not None:
            async with database.read_session() as session:
                rows = (await session.scalars(select(ToolArtifactModel))).all()
                original = json.loads((root / rows[0].relative_path).read_text(encoding="utf8"))
                assert len(original["data"]["memories"]) == 3
                assert original["data"]["returned_count"] == 3
                assert original["evidence_state"]
            evidence["originals"] = await _originals(database, root)
        else:
            assert seen[0]["evidence_state"]
    finally:
        await host.close_host_resources()


async def test_budget_configuration_owner_keeps_valid_overrides(
    database, tmp_path, tool_delivery_evidence
):
    _, evidence = tool_delivery_evidence
    assert make_settings(database.url).agent_tool_result_max_characters == 64000
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=False)
    changes = []
    for value in (32000, 1, 64001):
        changes.extend(await _configure(case.chat, characters=value))
        assert (
            await case.chat._runtime_config.snapshot()
        ).agent.tool_result_max_characters == value
    for key in ("agent.tool_result_max_characters", "tooling.result_token_budget"):
        for value in (0, -1):
            invalid = await case.chat._runtime_config.set_override(
                key,
                value,
                scope_type="global",
                scope_id="",
                actor_user_id="10001",
                trigger_message_id="invalid-budget",
            )
            assert not invalid.success, invalid
    snapshot = await case.chat._runtime_config.snapshot()
    assert snapshot.agent.tool_result_max_characters == 64001
    assert snapshot.tooling.result_token_budget is None
    evidence.update(
        {
            "scenario": "configuration-owner",
            "changes": changes,
            "effective_characters": snapshot.agent.tool_result_max_characters,
            "effective_tokens": snapshot.tooling.result_token_budget,
        }
    )


async def test_oversized_memory_fact_continues_from_its_actual_preview(
    database, tmp_path, tool_delivery_evidence, caplog
):
    """A real fact above both budgets is successful and its exact tail remains readable."""
    _, evidence = tool_delivery_evidence
    caplog.set_level("INFO", logger="yuki.services.agent_tools")
    provider = FakeLLMProvider()
    original = "original observatory " + "完整记忆ß😀" * 1000 + " SINGLE_MEMORY_TAIL"
    chat, source, host, fact, _ = await memory_scene(
        database, tmp_path, provider, "person", personal_content=original
    )
    changes = await _configure(chat, characters=1024, tokens=1024)
    source = replace(source, runtime_config=await chat._runtime_config.snapshot())
    root = tmp_path / "single-memory-artifacts"
    chat._tool_artifacts = ToolArtifactRepository(database, root, retention_seconds=600)
    seen, pieces = [], []
    call_id, handle = "single-fact", None

    def respond(request):
        nonlocal call_id, handle
        if len(provider.requests) == 1:
            return _tool("read_memory", {"view": "fact", "fact_id": fact.id}, call_id)
        payload = _last_result(request, call_id)
        seen.append(payload)
        assert payload["ok"], payload
        if handle is None:
            assert payload["truncated"]
            handle = payload["artifact_handle"]
            pieces.append(payload["data"]["memory"]["content"])
            continuation = next(
                item for item in payload["read_more"] if item["path"] == ["memory", "content"]
            )
            assert continuation["offset"] == len(pieces[0])
        else:
            page = payload["data"]
            assert page["offset"] == sum(map(len, pieces))
            pieces.append(page["value"])
            if page["next_offset"] is None:
                return _tool("task_control", {"action": "complete", "result": "read tail"}, "done")
            continuation = {
                "handle": handle,
                "path": ["memory", "content"],
                "offset": page["next_offset"],
            }
        call_id = f"single-page-{len(pieces)}"
        return _tool("read_tool_artifact", continuation, call_id)

    provider._responder = respond
    evidence.update(
        {
            "scenario": "single-memory-preview-continuation",
            "config_changes": changes,
            "original": original,
            "model_results": seen,
            "pieces": pieces,
        }
    )
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Read the complete original fact including its tail")
        assert result.ok, result
        assert "".join(pieces) == original
        assert any("outcome=success result_count=1" in record.message for record in caplog.records)
        evidence["originals"] = await _originals(database, root)
        async with database.read_session() as session:
            rows = (await session.scalars(select(ToolArtifactModel))).all()
            assert len(rows) == 1
            archived = json.loads((root / rows[0].relative_path).read_text(encoding="utf8"))
            assert archived["data"]["memory"]["content"] == original
            assert archived["evidence_state"]
    finally:
        await host.close_host_resources()


async def test_public_reader_defaults_to_logical_root_and_keeps_explicit_text_and_image(
    database, tmp_path, tool_delivery_evidence
):
    """Root get, text and image stay separate public operations, never manifests of pages."""
    _, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=False)
    root = tmp_path / "reader-artifacts"
    store = ToolArtifactRepository(database, root, retention_seconds=600)
    case.chat._tool_artifacts = store
    access = ArtifactAccess(
        case.env.context.conversation_id,
        1,
        case.env.person,
        read_scope=json.dumps(
            {"memory": [], "plugin_id": None, "delegation_id": None}, sort_keys=True
        ),
    )
    value = {"source": "original", "nested": ["first", "last"]}
    content = json.dumps({"ok": True, "data": value}, ensure_ascii=False)
    handle = await store.write_artifact(
        provider_id="fixture",
        tool_name="original",
        content=content,
        media_type="application/json",
        access=access,
    )
    pixels = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(pixels, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(pixels.getvalue()).decode()
    image_handle = await store.write_media_artifact(
        provider_id="fixture",
        tool_name="image-original",
        images=(ChatImage(data_url),),
        access=access,
    )
    seen, images = {}, []

    def respond(request):
        if len(case.provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=tuple(
                    ToolCall(name, ToolFunction("read_tool_artifact", json.dumps(args)))
                    for name, args in (
                        ("root", {"handle": handle}),
                        ("text", {"handle": handle, "operation": "text"}),
                        ("image", {"handle": image_handle, "operation": "image"}),
                    )
                ),
            )
        for name in ("root", "text", "image"):
            seen[name] = _last_result(request, name)
        images.extend(image for message in request.messages for image in message.images)
        return "read original structured, text and image values"

    case.provider._responder = respond
    evidence.update(
        {
            "scenario": "public-reader-operations",
            "model_results": seen,
            "original_value": value,
            "original_text": content,
        }
    )
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, result
    assert all(payload["ok"] for payload in seen.values()), seen
    assert seen["root"]["data"]["logical_root"] == "data"
    assert seen["root"]["data"]["path"] == []
    assert seen["root"]["data"]["value"] == value
    assert seen["text"]["data"]["content"] == content
    assert seen["image"]["data"]["mode"] == "image"
    assert [image.data_url for image in images] == [data_url]
    evidence["image_sha256"] = hashlib.sha256(pixels.getvalue()).hexdigest()
    evidence["originals"] = await _originals(database, root)
    async with database.read_session() as session:
        assert len((await session.scalars(select(ToolArtifactModel))).all()) == 2


async def test_public_reader_errors_allow_an_explicit_text_read_without_archive_recursion(
    database, tmp_path, tool_delivery_evidence
):
    """Non-JSON get and a missing path are real read failures, not empty successful pages."""
    _, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=False)
    root = tmp_path / "reader-error-artifacts"
    store = ToolArtifactRepository(database, root, retention_seconds=600)
    case.chat._tool_artifacts = store
    access = ArtifactAccess(
        case.env.context.conversation_id,
        1,
        case.env.person,
        read_scope=json.dumps(
            {"memory": [], "plugin_id": None, "delegation_id": None}, sort_keys=True
        ),
    )
    original = "原始普通文本 ß 😀\n末尾完整保留"
    text_handle = await store.write_artifact(
        provider_id="fixture",
        tool_name="plain",
        content=original,
        media_type="text/plain",
        access=access,
    )
    json_handle = await store.write_artifact(
        provider_id="fixture",
        tool_name="json",
        content='{"ok":true,"data":{"found":1}}',
        media_type="application/json",
        access=access,
    )
    seen = {}

    def respond(request):
        if len(case.provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=tuple(
                    ToolCall(name, ToolFunction("read_tool_artifact", json.dumps(args)))
                    for name, args in (
                        ("non-json", {"handle": text_handle}),
                        ("missing-path", {"handle": json_handle, "path": ["absent"]}),
                    )
                ),
            )
        if len(case.provider.requests) == 2:
            for name in ("non-json", "missing-path"):
                seen[name] = _last_result(request, name)
            return _tool(
                "read_tool_artifact", {"handle": text_handle, "operation": "text"}, "explicit-text"
            )
        seen["explicit-text"] = _last_result(request, "explicit-text")
        return "read original plain text after the explicit correction"

    case.provider._responder = respond
    evidence.update(
        {"scenario": "public-reader-errors", "model_results": seen, "original_text": original}
    )
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, result
    assert not seen["non-json"]["ok"]
    assert seen["non-json"]["error_code"] == "artifact_not_json"
    assert "text" in seen["non-json"]["public_message"]
    assert not seen["missing-path"]["ok"]
    assert seen["missing-path"]["error_code"] == "artifact_path_not_found"
    assert seen["explicit-text"]["ok"]
    assert seen["explicit-text"]["data"]["content"] == original
    evidence["originals"] = await _originals(database, root)
    async with database.read_session() as session:
        assert len((await session.scalars(select(ToolArtifactModel))).all()) == 2
