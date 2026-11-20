"""Real App/Main/Work: delegation never rewrites the provider request prefix (§5.2).

The App, router, Work scheduler and connector transport are real. Scripted Main
responses drive delegation, progress, permission and completion (P1–P4), an
observed terminal permission (P5), and two children settling in reverse order
(P6). Compare production Claude Messages, Chat Completions and OpenAI Responses
payloads after removing moving cache markers. The Responses model returns native
function calls through the production parser so the runner supplies continuation
items and tool outputs on later requests. Per-protocol hashes and wake boundaries
are written under test-results/external-agent-prompt-prefix/.
"""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.integration import test_current_material_main_contract as model_fixture
from tests.integration.test_desktop_native_work_fixture import eventually
from tests.integration.test_desktop_native_work_fixture import native_app as native_app
from tests.support.desktop_native_work_fixture import DesktopNativeWorkScenario, call

from yuki.domain.messages import ChatResponse
from yuki.external_agents import repository as agent_repository
from yuki.external_agents.cards import external_agent_card
from yuki.external_agents.db_models import AgentOperationModel
from yuki.external_agents.delegation import waiting_on_parent
from yuki.llm.anthropic_messages import AnthropicMessagesProvider
from yuki.llm.fake import FakeLLMProvider
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.llm.openai_responses import OpenAIResponsesProvider
from yuki.model_runtime.models import ModelProtocol
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_schema_v1 import work

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
    # Production OpenAI profiles declare explicit breakpoints, which move the
    # leading instructions into the first developer input item.
    "openai_responses": (
        lambda: OpenAIResponsesProvider(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            prompt_cache_breakpoints=True,
        ),
        None,
        "input",
    ),
    "chat_completions": (
        lambda: OpenAICompatibleProvider(
            base_url="https://wire.invalid", api_key="synthetic", timeout_seconds=1, max_retries=0
        ),
        None,
        "messages",
    ),
}


@pytest.fixture(params=sorted(PROTOCOLS))
def protocol(request, monkeypatch):
    protocol = request.param
    if protocol == "openai_responses":
        profile = model_fixture._profile().model_copy(
            update={"provider": "openai", "protocol": ModelProtocol.RESPONSES}
        )
        monkeypatch.setattr(model_fixture, "_profile", lambda: profile)
    return protocol


@pytest.fixture
async def wire(protocol, native_app, monkeypatch):
    make, system_key, items_key = PROTOCOLS[protocol]
    provider = make()
    if protocol == "openai_responses":
        complete = FakeLLMProvider.complete

        async def responses(model, request):
            scripted = await complete(model, request)
            output = [
                {
                    "type": "function_call",
                    "call_id": tool.id,
                    "name": tool.function.name,
                    "arguments": tool.function.arguments,
                }
                for tool in scripted.tool_calls
            ]
            if scripted.content:
                output.append({"type": "message", "role": "assistant", "content": scripted.content})
            return provider._parse_response(
                {"output": output}, provider._request_continuation(request), latency=0
            )

        monkeypatch.setattr(FakeLLMProvider, "complete", responses)
    try:
        yield protocol, provider, system_key, items_key
    finally:
        await provider.close()


async def test_dispatch_and_settlement_only_append_after_the_stable_prefix(
    wire, native_app, monkeypatch
):
    f = native_app
    captured: list[tuple[Any, str | None]] = []
    original = DesktopNativeWorkScenario.respond

    async def record(scenario, request):
        response = await original(scenario, request)
        if response is not None and [item.function.name for item in response.tool_calls] == [
            "wait"
        ]:
            # A parent answers what its delegation waits on before waiting on it; that
            # wait is refused (run_waiting_for_you, 2026-11-20 delegation deadlock fix).
            async with scenario.container.database.read_session() as reader:
                waiting = await waiting_on_parent(reader, scenario.delegation_id)
            if waiting:
                response = call(
                    "message_task",
                    {
                        "id": scenario.delegation_id,
                        "reply_to": waiting[0]["permission_id"],
                        "answer": "allow",
                    },
                )
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
    completed = await eventually(facts, lambda v: v["work"]["state"] == "completed")

    protocol, provider, system_key, items_key = wire
    payloads = [provider._build_payload(request) for request, _ in captured]
    if protocol == "openai_responses":
        assert any(req.continuation and req.continuation_items for req, _ in captured)
        assert any(
            item.get("type") == "function_call_output"
            for payload in payloads
            for item in payload["input"]
        )
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

    # P3 / P4: across each released activation the earlier public history stays
    # identical, and the card arrives only after it.
    commons = {}
    wake_inputs = [
        json.loads(row["payload_json"])["text"]
        for row in completed["inputs"]
        if row["kind"] == "subagent"
    ]
    permission_input, completion_input = wake_inputs
    for wake, needle, delivery in (
        (permission_wake, b"permission_id", permission_input),
        (completion_wake, record.encode(), completion_input),
    ):
        before = items(wake - 1)
        common = common_prefix(before, items(wake))
        history = next(index for index, item in enumerate(before) if b'\\"current_time\\"' in item)
        assert history > 0 and common >= history
        after = items(wake)
        assert b"external_agent_card" in delivery.encode()
        assert occurrences(payloads[wake - 1][items_key], delivery) == 0
        assert occurrences(payloads[wake][items_key][common:], delivery) == 1
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


async def send_prompt(f, text):
    sent = await f.client.post(
        "client/v1/messages", headers=f.headers, json={"request_id": str(uuid4()), "text": text}
    )
    assert sent.status_code == 200, sent.text


def common_prefix(before: list[bytes], after: list[bytes]) -> int:
    for index, (left, right) in enumerate(zip(before, after, strict=False)):
        if left != right:
            return index
    return min(len(before), len(after))


def request_evidence(payload, system_key, items_key):
    return {
        "system_sha256": digest(payload[system_key] if system_key else payload[items_key][0]),
        "tools_sha256": digest(payload["tools"]),
        "item_sha256": [digest(item) for item in payload[items_key]],
    }


def write_scenario(protocol, scenario, evidence):
    directory = ARTIFACT / scenario
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{protocol}.json").write_text(json.dumps(evidence, indent=2) + "\n")


async def test_observation_notice_keeps_router_prefix(wire, native_app, monkeypatch):
    protocol, provider, system_key, items_key = wire
    f = native_app
    devices = (await f.client.get("agents/v1/devices", headers=f.headers)).json()
    instance = devices["instances"][0]["id"]
    sessions = await f.client.get(f"agents/v1/instances/{instance}/sessions", headers=f.headers)
    session = sessions.json()["sessions"][0]["id"]
    baseline, notice = [], []
    observed = asyncio.Event()
    notified = asyncio.Event()

    async def watch(scenario, request):
        control = current_work_control.get()
        if control is None:
            return None
        payload = provider._build_payload(request)
        if control.source.get("observation_id"):
            if not notice:
                notice.append(payload)
                return call("send", {"text": "The terminal is waiting for approval of P5Tool."})
            notified.set()
            return call("manage_task", {"action": "finish", "result": "notice processed"})
        if not baseline:
            baseline.append(payload)
            return call(
                "start_subtask",
                {
                    "executor": {"agent": instance},
                    "session": session,
                    "relationship": "observe",
                    "goal": "Watch the terminal",
                },
            )
        observed.set()
        return ChatResponse(content="Observation registered.", latency_seconds=0)

    monkeypatch.setattr(DesktopNativeWorkScenario, "respond", watch)
    await send_prompt(f, "Observe the terminal session.")
    await asyncio.wait_for(observed.wait(), 15)
    [command] = [cmd for cmd in f.peer.commands if cmd["action"] == "watch"]
    await f.peer.event(command["id"], "permission", {"native_request_id": "p5", "tool": "P5Tool"})
    await asyncio.wait_for(notified.wait(), 15)
    assert b"P5Tool" in encoded(notice[0][items_key])
    before, after = [request_evidence(p, system_key, items_key) for p in (baseline[0], notice[0])]
    for key in ("system_sha256", "tools_sha256"):
        assert before[key] == after[key]
    write_scenario(protocol, "observation", {"router": before, "notice": after})


async def test_two_delegations_complete_in_reverse_order(wire, native_app, monkeypatch):
    protocol, provider, system_key, items_key = wire
    f = native_app
    devices = (await f.client.get("agents/v1/devices", headers=f.headers)).json()
    instance = devices["instances"][0]["id"]
    requests = []
    waits = []
    operations = []
    # Each new native prompt opens its own thread and turn on this device.
    reply = f.peer.reply

    async def reply_in_session(command, result, **kwargs):
        if command["action"] == "prompt":
            result.update(
                session_id="thread-" + command["id"], execution_id="turn-" + command["id"]
            )
        await reply(command, result, **kwargs)

    monkeypatch.setattr(f.peer, "reply", reply_in_session)
    card_inputs = {}

    def record_card(**facts):
        if facts["event"] == "completed":
            card_inputs[facts["operation_id"]] = facts
        return external_agent_card(**facts)

    monkeypatch.setattr(agent_repository, "external_agent_card", record_card)

    async def delegate(scenario, request):
        control = current_work_control.get()
        if control is None:
            return None
        requests.append(provider._build_payload(request))
        if control.current is None:
            return call("manage_task", {"action": "start", "goal": "Run both delegations"})
        scenario.work_id = control.current["id"]
        async with scenario.container.database.read_session() as reader:
            children = (
                await reader.execute(
                    select(AgentOperationModel.id, work.c.id, work.c.state)
                    .join(work, AgentOperationModel.work_id == work.c.id)
                    .where(
                        work.c.parent_work_id == scenario.work_id,
                        AgentOperationModel.action == "prompt",
                    )
                    .order_by(work.c.created)
                )
            ).all()
        if len(children) < 2:
            return call(
                "start_subtask",
                {
                    "executor": {"agent": instance},
                    "cwd": f"/isolated/project-{len(children)}",
                    "goal": f"Run project {len(children)}",
                },
            )
        operations[:] = [op for op, _, _ in children]
        pending = [{"run": child} for _, child, state in children if state != "completed"]
        if pending:
            waits.append(len(requests) - 1)
            return call("wait", {"for": pending})
        return call("manage_task", {"action": "finish", "result": "Both completed"})

    monkeypatch.setattr(DesktopNativeWorkScenario, "respond", delegate)
    await send_prompt(f, "Delegate both projects.")

    async def facts():
        return (await f.client.get("__test/native-facts")).json()

    await eventually(
        facts,
        lambda v: (
            v["work"] and v["work"]["state"] == "waiting_external" and len(f.peer.results) == 2
        ),
    )
    assert len(set(operations)) == 2
    evidence = []
    summaries = ["p6-completion-" + uuid4().hex for _ in operations]
    for index, operation in enumerate(reversed(operations)):
        prior = waits[index]
        f.peer.native_session = f.peer.results[operation]["session_id"]
        f.peer.native_execution = f.peer.results[operation]["execution_id"]
        await f.peer.event(
            operation, "completed", {"status": "completed", "text": summaries[index]}
        )
        state = "completed" if index else "waiting_external"
        settled = await eventually(
            facts,
            lambda v, prior=prior, state=state: (
                len(requests) > prior + 1 and v["work"]["state"] == state
            ),
        )
        before, after = (p[items_key] for p in requests[prior : prior + 2])
        common = common_prefix(
            [encoded(item) for item in before], [encoded(item) for item in after]
        )
        history = next(
            index for index, item in enumerate(before) if b'\\"current_time\\"' in encoded(item)
        )
        assert history > 0 and common >= history
        cards = [
            json.loads(row["payload_json"])["text"]
            for row in settled["inputs"]
            if row["kind"] == "subagent"
        ]
        [delivery] = [text for text in cards if summaries[index] in text]
        card = json.loads(delivery)["text"]
        decoded = json.loads(card)
        assert decoded["operation_id"] == operation
        assert decoded["kind"] == "external_agent_card"
        assert external_agent_card(**card_inputs[operation]).encode() == card.encode()
        # Compare the entire persisted card with the text actually delivered by the runner.
        assert occurrences(before, delivery) == occurrences(after[:common], delivery) == 0
        assert occurrences(after[common:], delivery) == 1
        assert occurrences(after[common:], summaries[1 - index]) == 0
        evidence.append(
            {
                "prior": prior,
                "wake": prior + 1,
                "operation_id": operation,
                "common_prefix_items": common,
                "public_history_items": history,
                "card_sha256": hashlib.sha256(card.encode()).hexdigest(),
            }
        )
    assert len(waits) == 2
    write_scenario(
        protocol,
        "concurrent",
        {
            "requests": [request_evidence(p, system_key, items_key) for p in requests],
            "completions": evidence,
        },
    )


def occurrences(items, needle):
    return sum(item.count(needle) for value in items for item in text_fields(value))


def text_fields(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_fields(item)
