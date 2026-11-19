"""Real App/Main/Work: delegation never rewrites the provider request prefix (§5.2).

Every actual Main request of the parent Work is captured at the model boundary and
encoded by the production Claude Messages and Chat Completions providers (the bytes
they send, minus the moving cache markers, which are not prompt content).

Scenarios (taskbook §5.2): P1 delegating with start_subtask keeps system and tools
byte-identical and free of device/instance/session/operation/run IDs; P2 native
progress produces no parent request and never appears in any request; P4 a native
permission wakes the parent and its card appears only in the appended tail; P3 the
completion wake likewise. Failure cases written first: dynamic facts in tools or
system; tools/system bytes changing between requests; progress text in a request
or an extra request caused by progress; a wake rewriting earlier history instead of
appending. Within one activation each request extends the previous byte for byte.
Artifact: ``test-results/external-agent-prompt-prefix/<protocol>.json``.
"""

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.integration.test_desktop_native_work_fixture import eventually
from tests.integration.test_desktop_native_work_fixture import native_app as native_app
from tests.support.desktop_native_work_fixture import DesktopNativeWorkScenario

from yuki.llm.anthropic_messages import AnthropicMessagesProvider
from yuki.llm.openai_compatible import OpenAICompatibleProvider

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "external-agent-prompt-prefix"
MARKERS = {"cache_control", "prompt_cache_breakpoint"}


def content(value: Any) -> Any:
    """The prompt content of a wire value: cache markers move by design."""
    if isinstance(value, dict):
        return {key: content(item) for key, item in value.items() if key not in MARKERS}
    if isinstance(value, list):
        return [content(item) for item in value]
    return value


def encoded(value: Any) -> bytes:
    return json.dumps(content(value), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


PROTOCOLS = {
    "claude_messages": (
        lambda: AnthropicMessagesProvider(
            base_url="https://wire.invalid", api_key="synthetic", timeout_seconds=1, max_retries=0
        ),
        "system",
        "messages",
    ),
    "chat_completions": (
        lambda: OpenAICompatibleProvider(
            base_url="https://wire.invalid", api_key="synthetic", timeout_seconds=1, max_retries=0
        ),
        None,
        "messages",
    ),
}


@pytest.mark.parametrize("protocol", sorted(PROTOCOLS))
async def test_dispatch_and_settlement_only_append_after_the_stable_prefix(
    native_app, monkeypatch, protocol
):
    f = native_app
    captured: list[tuple[Any, str | None]] = []
    original = DesktopNativeWorkScenario.respond

    async def record(scenario, request):
        response = await original(scenario, request)
        if response is not None:
            name = response.tool_calls[0].function.name if response.tool_calls else None
            captured.append((request, name))
        return response

    monkeypatch.setattr(DesktopNativeWorkScenario, "respond", record)
    marker = "native-fixture-" + uuid4().hex
    assert (
        await f.client.post("__test/native-scenario", json={"marker": marker})
    ).status_code == 200
    catalog = (await f.client.get("agents/v1/devices", headers=f.headers)).json()
    instance = catalog["instances"][0]["id"]
    body = {
        "request_id": str(uuid4()),
        "text": marker,
        "agent_target": {
            "intent": "prompt",
            "device_id": f.device["device_id"],
            "instance_id": instance,
            "cwd": "/isolated/native-project",
        },
    }
    sent = await f.client.post("client/v1/messages", headers=f.headers, json=body)
    assert sent.status_code == 200, sent.text

    async def facts():
        return (await f.client.get("__test/native-facts")).json()

    waiting = await eventually(
        facts, lambda v: v["work"] and v["work"]["state"] == "waiting_external"
    )
    [prompt] = [row for row in waiting["operations"] if row["action"] == "prompt"]
    record = waiting["delegation"]["id"]
    before_progress = len(captured)
    progress = ["native-progress-" + uuid4().hex for _ in range(3)]
    for text in progress:
        await f.peer.event(prompt["id"], "progress", {"text": text})
    # P4: a permission is a wake fact; it is what produces the next parent request.
    await f.peer.event(
        prompt["id"],
        "permission",
        {"native_request_id": "native-permission", "tool": "Write", "options": ["allow"]},
    )
    await eventually(
        facts,
        lambda v: v["work"]["state"] == "waiting_external" and len(captured) > before_progress,
    )
    await f.peer.event(prompt["id"], "completed", {"status": "completed", "text": marker})
    await eventually(facts, lambda v: v["work"]["state"] == "completed")

    make, system_key, items_key = PROTOCOLS[protocol]
    provider = make()
    try:
        payloads = [provider._build_payload(request) for request, _ in captured]
    finally:
        await provider.close()
    names = [name for _, name in captured]
    dispatch = names.index("start_subtask")
    wakes = [index + 1 for index, name in enumerate(names[:-1]) if name == "wait"]
    # P2: the three progress events produced no request; the first request after the
    # dispatch's wait is the permission wake.
    assert wakes and wakes[0] == before_progress
    permission_wake, completion_wake = wakes[0], wakes[-1]
    assert permission_wake < completion_wake

    # P1: dynamic external-agent facts never enter the stable prefix.
    dynamic = [prompt["id"], f.device["device_id"], instance, prompt["run_id"], record]
    for payload in payloads:
        stable = encoded(payload.get(system_key) if system_key else payload[items_key][0])
        stable += encoded(payload.get("tools"))
        assert not [value for value in dynamic if value.encode() in stable]

    # P2: progress never reaches a request by itself; only an explicit get_tasks(id)
    # timeline read (a tool result the model asked for) may show it afterwards.
    for payload in payloads[: completion_wake + 1]:
        assert not [text for text in progress if text.encode() in encoded(payload)]
    assert names[completion_wake] == "get_tasks"

    def system(payload: dict[str, Any]) -> Any:
        # Chat Completions carries the system prompt as its first message.
        return payload.get(system_key) if system_key else payload[items_key][0]

    assert len({digest(system(payload)) for payload in payloads}) == 1
    assert len({digest(payload.get("tools")) for payload in payloads}) == 1

    def items(index: int) -> list[bytes]:
        return [encoded(item) for item in payloads[index][items_key]]

    # Within one activation every request extends the previous one byte for byte.
    for earlier in range(len(payloads) - 1):
        if earlier + 1 in wakes:
            continue
        before, after = items(earlier), items(earlier + 1)
        assert after[: len(before)] == before, (names[earlier], names[earlier + 1])
    receipt = items(dispatch + 1)[len(items(dispatch)) :]
    assert any(record.encode() in item for item in receipt)
    assert not any(record.encode() in item for item in items(dispatch))

    def common_prefix(earlier: int, later: int) -> int:
        before, after = items(earlier), items(later)
        common = 0
        while common < min(len(before), len(after)) and before[common] == after[common]:
            common += 1
        return common

    # P3 / P4: across each released activation the earlier public history stays
    # identical, and the card arrives only after it.
    commons = {}
    for wake, needle in (
        (permission_wake, b"permission_id"),
        (completion_wake, record.encode()),
    ):
        before = items(wake - 1)
        common = common_prefix(wake - 1, wake)
        history = next(index for index, item in enumerate(before) if b'\\"current_time\\"' in item)
        assert history > 0 and common >= history
        after = items(wake)
        assert not any(b"external_agent_card" in item for item in after[:common])
        assert any(b"external_agent_card" in item for item in after[common:])
        assert any(needle in item for item in after[common:])
        commons[wake] = (common, history)
    permission_tail = b"".join(items(permission_wake)[commons[permission_wake][0] :])
    assert b"Write" in permission_tail

    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / f"{protocol}.json").write_text(
        json.dumps(
            {
                "protocol": protocol,
                "requests": [
                    {
                        "response_tool": name,
                        "system_sha256": digest(system(payload)),
                        "tools_sha256": digest(payload.get("tools")),
                        "items": len(payload[items_key]),
                        "item_sha256": [digest(item) for item in payload[items_key]],
                    }
                    for name, payload in zip(names, payloads, strict=True)
                ],
                "dispatch_request": dispatch,
                "progress_events_without_request": len(progress),
                "permission_wake_request": permission_wake,
                "completion_wake_request": completion_wake,
                "wake_common_prefix_items": {str(k): v[0] for k, v in commons.items()},
                "public_history_items": {str(k): v[1] for k, v in commons.items()},
            },
            indent=2,
        )
        + "\n"
    )
