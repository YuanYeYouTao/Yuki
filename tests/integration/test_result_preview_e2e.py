"""Main keeps real history items visible and resumes at the first unseen item.

The scenario starts with a page larger than its display budget. Its first
preview must contain complete events, and the public reader must continue at
the original events offset, not restart the query or invent another source.
Empty/oversized-item and source revocation cases retain their existing reader
coverage; this checks the actual Main -> archive -> reader -> model path.
"""

import json
import shutil
from dataclasses import asdict

import pytest
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.event_seed import seed_scoped_event

from yuki.automation.models import RunStatus
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.tool_results.artifacts import ToolArtifactRepository


@pytest.mark.parametrize("work_enabled", [False, True])
async def test_main_history_preview_resumes_at_first_unseen_event(
    database, tmp_path, tool_delivery_evidence, work_enabled
):
    directory, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=work_enabled)
    case.settings.agent_tool_result_max_characters = 7000
    case.settings.tooling_result_token_budget = None
    case.settings.context_compact_trigger_tokens = 500000
    store = ToolArtifactRepository(database, tmp_path / "originals", retention_seconds=60)
    case.chat._tool_artifacts = store
    originals = []
    for index in range(8):
        content = f"preview-needle-{index} " + "原文\\\n" * 180 + f" tail-{index}"
        event = await seed_scoped_event(
            case.env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            origin_key=f"preview-original-{index}",
            sender_user_id="10001",
            direction="inbound",
            content=content,
        )
        originals.append((event.event.id, content))
    evidence.update(original_events=originals, work_enabled=work_enabled, budget=7000)
    observed = []
    rows = []
    continuation = None

    def call(name, arguments, identity):
        return ChatResponse(
            "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(arguments))),)
        )

    def respond(request):
        nonlocal continuation
        evidence["requests"] = [asdict(item) for item in case.provider.requests]
        if len(case.provider.requests) == 1:
            return call(
                "query_chat_history",
                {"view": "search", "keyword": "preview-needle", "order": "oldest", "limit": 8},
                "original-history-query",
            )
        reply = next(message for message in reversed(request.messages) if message.role == "tool")
        payload = json.loads(reply.content)
        observed.append(payload)
        evidence["model_tool_results"] = observed
        assert len(reply.content) <= 7000
        if len(observed) == 1:
            assert payload["ok"] and payload["truncated"]
            rows.extend(payload["data"]["events"])
            assert 0 < len(rows) < len(originals)
            continuation = next(item for item in payload["read_more"] if item["path"] == ["events"])
            assert continuation["offset"] == len(rows)
        else:
            assert payload["ok"], payload
            page = payload["data"]
            assert page["path"] == ["events"]
            rows.extend(page["value"])
            if page["next_offset"] is None:
                evidence["reconstructed_events"] = rows
                return "All original history items were read."
            continuation = {**continuation, "offset": page["next_offset"]}
        return call("read_tool_artifact", continuation, f"history-page-{len(rows)}")

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    evidence["run"] = result.model_dump(mode="json")
    for original in (tmp_path / "originals").rglob("*"):
        if original.is_file():
            target = directory / "originals" / original.relative_to(tmp_path / "originals")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    assert result.status is RunStatus.SUCCEEDED, result
    assert [(row["event_id"], row["content"]) for row in rows] == originals
    assert not [action for action, _ in case.env.bot.calls if action.startswith("send_")]
