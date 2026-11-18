"""Real App/Main/Work: dispatch and settlement never rewrite the provider request prefix.

Every actual Main request of one Work is captured at the model boundary and encoded
by the production Claude Messages and Chat Completions providers (the bytes they send,
minus the moving cache markers, which are not prompt content). Failure cases written
before checking: dynamic external-agent facts (operation, device, instance, receipt
status, step kind/detail) appearing in tools or system; tools or system bytes
changing between requests; dispatching or a later settlement rewriting an earlier
message instead of appending.

Across the wait that releases the activation, the resumed activation lays out its own
tail (the established Work-resume layout, unchanged by external-agent dispatch): the
system, tools and earlier public history stay byte-identical, and the settlement
arrives only after them.
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
    await f.peer.event(prompt["id"], "completed", {"status": "completed", "text": marker})
    await eventually(facts, lambda v: v["work"]["state"] == "completed")

    make, system_key, items_key = PROTOCOLS[protocol]
    provider = make()
    try:
        payloads = [provider._build_payload(request) for request, _ in captured]
    finally:
        await provider.close()
    names = [name for _, name in captured]
    dispatch = names.index("use_coding_agent")
    wake = names.index("get_coding_agents")
    assert names[dispatch + 1] == "wait" and wake == dispatch + 2

    # Dynamic external-agent facts never enter the stable prefix.
    dynamic = [prompt["id"], f.device["device_id"], instance, prompt["run_id"]]
    for payload in payloads:
        stable = encoded(payload.get(system_key) if system_key else payload[items_key][0])
        stable += encoded(payload.get("tools"))
        assert not [value for value in dynamic if value.encode() in stable]

    def system(payload: dict[str, Any]) -> Any:
        # Chat Completions carries the system prompt as its first message.
        return payload.get(system_key) if system_key else payload[items_key][0]

    assert len({digest(system(payload)) for payload in payloads}) == 1
    assert len({digest(payload.get("tools")) for payload in payloads}) == 1

    def items(index: int) -> list[bytes]:
        return [encoded(item) for item in payloads[index][items_key]]

    # Within one activation every request extends the previous one byte for byte;
    # dispatching appends its call and receipt at the end.
    for earlier in range(len(payloads) - 1):
        if earlier + 1 == wake:
            continue
        before, after = items(earlier), items(earlier + 1)
        assert after[: len(before)] == before, (names[earlier], names[earlier + 1])
    receipt = items(dispatch + 1)[len(items(dispatch)) :]
    assert any(prompt["id"].encode() in item for item in receipt)
    assert not any(prompt["id"].encode() in item for item in items(dispatch))

    # Across the released activation: earlier public history stays identical, and the
    # settlement and its wake-up arrive only after it.
    before, after = items(wake - 1), items(wake)
    common = 0
    while common < min(len(before), len(after)) and before[common] == after[common]:
        common += 1
    # The earlier activation's own runtime_context block (system rules also name the tag).
    history = next(index for index, item in enumerate(before) if b'\\"current_time\\"' in item)
    assert history > 0
    assert common >= history
    assert not any(prompt["id"].encode() in item for item in after[:common])
    assert any(prompt["run_id"].encode() in item for item in after[common:])

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
                "wake_request": wake,
                "wake_common_prefix_items": common,
                "public_history_items": history,
            },
            indent=2,
        )
        + "\n"
    )
