"""Progress reads preserve native targets, ordering, contents and actual gaps."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from yuki_agent_connector import claude_inspect
from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers.acp import ACPDriver, Session
from yuki_agent_connector.drivers.claude import ClaudeDriver, ManagedSession
from yuki_agent_connector.drivers.codex import CodexDriver


async def discard(*_):
    pass


class NativeRPC:
    def __init__(self):
        self.calls = []

    async def call(self, method, args):
        self.calls.append((method, args))
        if method == "thread/read":
            return {"thread": {"id": "s", "status": {"type": "idle"}}}
        if method == "thread/turns/list":
            old = args.get("cursor") == "older"
            return {
                "data": [
                    {
                        "id": "old" if old else "new",
                        "status": "completed",
                        "itemsView": args["itemsView"],
                        "startedAt": 1 if old else 3,
                        "completedAt": 2 if old else 4,
                        "items": [{"id": "action", "type": "agentMessage", "text": "latest fact"}],
                    }
                ],
                "nextCursor": None if old else "older",
            }
        if method == "thread/items/list":
            return {
                "data": [
                    {
                        "item": {
                            "id": "call",
                            "type": "mcpToolCall",
                            "tool": "read",
                            "result": {"ok": True},
                        }
                    }
                ],
                "nextCursor": "item-older",
            }
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_codex_one_native_page_keeps_current_turn_separate_from_old_page():
    driver = CodexDriver(Instance("i", "codex", "i", "unused"), discard)
    rpc = NativeRPC()
    driver.rpc = rpc
    result = await driver.execute({"id": "q", "action": "get", "session_id": "s", "arguments": {}})
    assert result["status"] == "idle" and result["status_scope"] == "session"
    assert result["latest_turn"]["id"] == "new"
    assert result["latest_turn"]["items"][0]["text"] == "latest fact"
    assert len(rpc.calls) == 2
    rpc.calls.clear()
    result = await driver.execute(
        {
            "id": "q",
            "action": "get",
            "session_id": "s",
            "arguments": {"include_history": True, "cursor": "older"},
        }
    )
    assert result["native"]["turns"][0]["id"] == "old"
    assert result["latest_turn"]["id"] == "new"
    assert result["history_order"] == "desc"
    assert result["next_cursor"] is None
    assert [args.get("cursor") for method, args in rpc.calls if method == "thread/turns/list"] == [
        "older",
        None,
    ]


@pytest.mark.asyncio
async def test_codex_explicit_old_turn_reads_original_items_not_current_turn():
    driver = CodexDriver(Instance("i", "codex", "i", "unused"), discard)
    rpc = NativeRPC()
    driver.rpc = rpc
    result = await driver.execute(
        {
            "id": "q",
            "action": "get",
            "session_id": "s",
            "execution_id": "old",
            "arguments": {"cursor": "items-page"},
        }
    )
    assert result["execution_id"] == "old"
    assert result["selected_turn"]["items"][0]["result"] == {"ok": True}
    assert result["next_cursor"] == "item-older"
    method, args = rpc.calls[-1]
    assert method == "thread/items/list" and args["turnId"] == "old"
    assert args["cursor"] == "items-page"
    assert not any(method in {"thread/resume", "turn/start"} for method, _ in rpc.calls)


@dataclass
class SessionInfo:
    session_id: str = "s"


def message(identity, role="assistant", content=None):
    return SimpleNamespace(
        uuid=identity,
        type=role,
        message={"content": content or [{"type": "text", "text": identity}]},
    )


def test_claude_tail_page_and_tool_details_do_not_become_oldest_thirty(monkeypatch):
    messages = [message(str(i)) for i in range(45)]
    messages[-1] = message(
        "44",
        content=[
            {
                "type": "tool_result",
                "tool_use_id": "call",
                "content": "real output",
                "is_error": True,
            },
            {"type": "thinking", "thinking": "private"},
        ],
    )
    monkeypatch.setattr(claude_inspect, "get_session_info", lambda *_, **__: SessionInfo())
    monkeypatch.setattr(claude_inspect, "get_session_messages", lambda *_, **__: messages)
    request = {"action": "get", "session_id": "s", "include_history": True, "limit": 30}
    first = claude_inspect.inspect_profile(request)
    assert [item["id"] for item in first["messages"]] == [str(i) for i in range(15, 45)]
    assert first["next_cursor"] == "30" and first["history_order"] == "asc"
    assert first["messages"][-1]["content"] == [messages[-1].message["content"][0]]
    assert first["messages"][-1]["content_omitted"] is True
    second = claude_inspect.inspect_profile({**request, "cursor": first["next_cursor"]})
    assert [item["id"] for item in second["messages"]] == [str(i) for i in range(15)]
    assert second["latest_message"]["id"] == "44" and second["next_cursor"] is None
    latest = claude_inspect.inspect_profile({"action": "get", "session_id": "s"})
    assert [item["id"] for item in latest["messages"]] == ["44"]


def test_claude_explicit_execution_selects_its_user_and_tool_result_group(monkeypatch):
    messages = [
        message("A", "user"),
        message("tool", "user", [{"type": "tool_result", "content": "A result"}]),
        message("answer"),
        message("B", "user"),
        message("new"),
    ]
    monkeypatch.setattr(claude_inspect, "get_session_info", lambda *_, **__: SessionInfo())
    monkeypatch.setattr(claude_inspect, "get_session_messages", lambda *_, **__: messages)
    request = {"action": "get", "session_id": "s", "execution_id": "A", "include_history": True}
    result = claude_inspect.inspect_profile(request)
    assert [item["id"] for item in result["messages"]] == ["A", "tool", "answer"]
    missing = claude_inspect.inspect_profile({**request, "execution_id": "absent"})
    assert (
        missing["history_available"] is False and missing["history_reason"] == "execution_not_found"
    )


@pytest.mark.asyncio
async def test_attached_claude_and_acp_do_not_substitute_current_execution(monkeypatch):
    claude = ClaudeDriver(Instance("c", "claude", "c", "unused"), discard)

    async def inspect(*_):
        return {"status": "unknown", "history_available": True}

    monkeypatch.setattr(claude, "_inspect", inspect)
    claude.sessions["s"] = ManagedSession(
        "s", SimpleNamespace(), asyncio.get_running_loop().create_future(), execution="B"
    )
    acp = ACPDriver(Instance("a", "acp", "a", "unused"), discard)
    acp.connection = SimpleNamespace()
    acp.sessions["s"] = Session("s", ".", execution="B")
    for driver in (claude, acp):
        result = await driver.execute(
            {
                "id": "q",
                "action": "get",
                "session_id": "s",
                "execution_id": "A",
                "arguments": {"include_history": True},
            }
        )
        assert result["execution_id"] == "A" and result["status"] == "unknown"
        assert result["reason"] == "execution_not_attached"
    assert result["history_available"] is False
    assert result["history_reason"] == "native_history_not_supported"
