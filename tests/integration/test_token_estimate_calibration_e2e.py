"""E2E: the local token estimate places the compact trigger near real usage.

Production replay (2026-11-22, 13 Main requests paired with their real
``prompt_tokens``; only length statistics, no production text): the estimate
counted every non-ASCII character as 2 tokens and every 3 ASCII characters as
1 token. Chinese text measures about 0.8 token per character, so the configured
96000 trigger fired at 54–64k real tokens, a 1.5–1.8x over-estimate. Owner
decision: non-ASCII (CJK) characters count 1 token each, ASCII keeps 3
characters per token, opaque reasoning stays counted; no usage feedback state.

Failure cases written before the change:

C1 Every paired sample is rebuilt as a request with the sample's serialized
   composition: system, Chinese chat, tool calls/outputs, encrypted reasoning
   and a fixed tool declaration of the production size (within 10% of the
   sample's serialized bytes). Estimate / real usage: median about 1.2
   (1.1–1.35), at most 1.5, and no real under-count (the largest ASCII-heavy
   group sample sits at 1.0; floor 0.98). Growing each sample's conversation
   part at its own rate, the default trigger lands at a median of about 80k
   real tokens. The old coefficient gives a ratio median of about 1.8.
C2 A real group chat grows under the default 96000 trigger, once with mostly
   Chinese lines and once with ID/number-heavy mixed lines. The provider
   replays real usage with per-class rates fitted on the same pairs (ASCII
   2.24 characters per token, CJK 0.80 token per character, 4 tokens per
   message, tool declarations 4.75 bytes per token; C1 checks the replay
   against every real pair within 12%). The first public compact happens at
   72k–90k (Chinese) / 85k–100k (mixed) replayed real tokens and no Main
   estimate falls below replayed usage; the old coefficient compacts at about
   47k / 57k.

Main, Social, the fixed production tool contract, compaction and SQLite are
real; only model replies are scripted. Evidence is written to
``test-results/token-estimate-calibration-e2e.json``.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.commands_and_chat_helpers import inbound
from tests.support.event_seed import seed_scoped_event
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env

from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import _request_input, estimate_request_tokens
from yuki.services.context_compaction import (
    EXECUTION_COMPACT_INSTRUCTION,
    PUBLIC_COMPACT_INSTRUCTION,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "token-estimate-calibration-e2e.json"
RESULTS: dict[str, Any] = {}
TRIGGER = 96000

# Real-usage replay rates fitted on the paired samples below.
ASCII_CHARS_PER_TOKEN = 2.24
CJK_TOKENS_PER_CHAR = 0.804
TOKENS_PER_MESSAGE = 4
TOOL_BYTES_PER_TOKEN = 4.75

_CJK = "今天的讨论记录已经整理好了大家看看还有什么需要补充的地方我们明天继续推进"
_ASCII = "id=20261122 ts=1763800000 v2.1 ok #4096 see example.com/a/7 "


@dataclass(frozen=True)
class Sample:
    """Serialized (ASCII, non-ASCII) character counts of one paired Main request."""

    kind: str
    messages: int
    tool_bytes: int
    system: tuple[int, int]
    user: tuple[int, int]
    assistant: tuple[int, int]
    calls: tuple[int, int]
    outputs: tuple[int, int]
    call_ids: int
    reasoning: tuple[int, int]
    bytes: int
    real: int


# 2026-11-22 production pairs: lengths and real prompt tokens only.
SAMPLES = (
    Sample("space", 518, 32363, (2020, 7739), (52194, 11881), (11433, 6476),
           (0, 0), (0, 0), 0, (0, 0), 193802, 64746),
    Sample("space", 7, 32363, (2020, 7739), (1781, 828), (69, 28),
           (0, 0), (0, 0), 0, (0, 0), 62982, 14960),
    Sample("space", 9, 32363, (2020, 7739), (2051, 867), (69, 28),
           (0, 0), (0, 0), 0, (0, 0), 63429, 15045),
    Sample("private", 219, 32363, (2020, 7739), (23390, 9408), (4266, 2235),
           (1597, 420), (10934, 0), 1238, (5416, 3), 153665, 39849),
    Sample("private", 202, 32363, (2020, 7739), (15687, 9517), (5236, 3472),
           (0, 0), (0, 0), 0, (0, 0), 124671, 35131),
    Sample("private", 258, 32363, (2020, 7739), (22437, 11038), (5132, 3411),
           (3843, 827), (18097, 288), 2413, (16804, 9), 187551, 48248),
    Sample("private", 368, 32363, (2020, 7739), (48725, 4144), (9280, 5040),
           (862, 0), (20209, 5350), 1460, (1784, 1), 202467, 61714),
    Sample("private", 338, 32363, (2020, 7739), (23885, 5108), (9280, 5040),
           (30, 0), (67, 0), 127, (0, 0), 134399, 40321),
    Sample("private", 233, 32363, (2020, 7739), (34757, 11660), (6255, 4452),
           (527, 218), (31278, 892), 762, (1740, 1), 198090, 56026),
    Sample("private", 230, 32363, (2020, 7739), (19451, 11387), (6255, 4452),
           (327, 255), (4810, 91), 635, (3652, 2), 150881, 41047),
    Sample("private", 249, 32363, (2020, 7739), (20888, 11569), (6255, 4452),
           (915, 411), (11276, 198), 1397, (9196, 5), 169334, 45325),
    Sample("private", 240, 32363, (2020, 7739), (21296, 11951), (6255, 4452),
           (1910, 352), (20130, 1610), 889, (5288, 3), 180457, 48448),
    Sample("space", 124, 32363, (2020, 7739), (12117, 3131), (1253, 883),
           (0, 0), (0, 0), 0, (0, 0), 87553, 24453),
)  # fmt: skip


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    patch = subprocess.check_output(["git", "diff", "HEAD", "--", "src", "tests"], cwd=ROOT)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_token_estimate_calibration_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "source_revision": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "source_patch_sha256": hashlib.sha256(patch).hexdigest(),
                "trigger_tokens": TRIGGER,
                "replay_rates": {
                    "ascii_chars_per_token": ASCII_CHARS_PER_TOKEN,
                    "cjk_tokens_per_char": CJK_TOKENS_PER_CHAR,
                    "tokens_per_message": TOKENS_PER_MESSAGE,
                    "tool_bytes_per_token": TOOL_BYTES_PER_TOKEN,
                },
                "scenarios": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _mixed(ascii_count: int, cjk_count: int) -> str:
    """Synthetic text with exact class counts; no JSON escapes."""
    ascii_text = (_ASCII * (ascii_count // len(_ASCII) + 1))[:ascii_count]
    cjk_text = (_CJK * (cjk_count // len(_CJK) + 1))[:cjk_count]
    return ascii_text + cjk_text


def _split(total: tuple[int, int], parts: int) -> list[str]:
    ascii_count, cjk_count = total
    return [
        _mixed(
            ascii_count * (index + 1) // parts - ascii_count * index // parts,
            cjk_count * (index + 1) // parts - cjk_count * index // parts,
        )
        for index in range(parts)
    ]


def _tools(total_bytes: int, count: int = 29) -> tuple[ChatTool, ...]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["text"],
    }
    names = [f"fixed_tool_{index:02d}" for index in range(count)]
    empty = len(
        json.dumps(
            [{"name": name, "description": "", "parameters": schema} for name in names],
            ensure_ascii=False,
        )
    )
    return tuple(
        ChatTool(name, piece, schema)
        for name, piece in zip(names, _split((total_bytes - empty, 0), count), strict=True)
    )


def _sample_request(sample: Sample) -> ChatRequest:
    rounds = max(1, sample.call_ids // 50) if sample.call_ids else 0
    ordinary = sample.messages - 1 - 2 * rounds
    assistants = max(1, ordinary // 4)
    users = _split(sample.user, ordinary - assistants)
    replies = _split(sample.assistant, assistants)
    messages = [ChatMessage("system", _mixed(*sample.system))]
    while users or replies:
        if users:
            messages.append(ChatMessage("user", users.pop(0)))
        if users:
            messages.append(ChatMessage("user", users.pop(0)))
        if replies:
            messages.append(ChatMessage("assistant", replies.pop(0)))
    id_length = sample.call_ids // (2 * rounds) if rounds else 0
    for index, arguments, output in zip(
        range(rounds), _split(sample.calls, rounds), _split(sample.outputs, rounds), strict=True
    ):
        identity = f"call_{index:04d}".ljust(id_length, "x")
        call = ToolCall(identity, ToolFunction("fixed_tool_00", arguments))
        messages.append(ChatMessage("assistant", None, tool_calls=(call,)))
        messages.append(ChatMessage("tool", output, tool_call_id=identity))
    blob_count = sample.reasoning[1]
    continuation = (
        ProviderContinuation(
            "openai",
            "responses",
            tuple(
                {"type": "reasoning", "id": f"rs_{index:04d}", "encrypted_content": blob}
                for index, blob in enumerate(_split((sample.reasoning[0], 0), blob_count))
            ),
        )
        if blob_count
        else None
    )
    return ChatRequest(
        messages=tuple(messages),
        model="calibration",
        tools=_tools(sample.tool_bytes),
        continuation=continuation,
    )


def _class_counts(text: str) -> tuple[int, int]:
    ascii_count = sum(ord(character) < 128 for character in text)
    return ascii_count, len(text) - ascii_count


def _chars(text: str) -> dict[str, int]:
    ascii_count, non_ascii = _class_counts(text)
    return {"ascii": ascii_count, "non_ascii": non_ascii}


def replay_usage(request: ChatRequest) -> int:
    """Real prompt tokens by the per-class rates measured on the paired samples.

    Text content, call arguments and identifiers are tokenized; JSON framing is
    a per-message constant; encrypted reasoning counts no visible text.
    """
    ascii_count = non_ascii = 0

    def add(text: str | None) -> None:
        nonlocal ascii_count, non_ascii
        counts = _class_counts(text or "")
        ascii_count += counts[0]
        non_ascii += counts[1]

    for message in request.messages:
        add(message.content)
        add(message.tool_call_id)
        for call in message.tool_calls:
            add(call.id)
            add(call.function.arguments)
    declarations = [
        {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
        for tool in request.tools
    ]
    tool_bytes = len(json.dumps(declarations, ensure_ascii=False).encode())
    return round(
        ascii_count / ASCII_CHARS_PER_TOKEN
        + non_ascii * CJK_TOKENS_PER_CHAR
        + TOKENS_PER_MESSAGE * len(request.messages)
        + tool_bytes / TOOL_BYTES_PER_TOKEN
    )


def test_replayed_pairs_estimate_close_to_real_usage_without_undercount():
    """C1: rebuilt production pairs, estimate against their real prompt tokens."""
    rows = []
    for sample in SAMPLES:
        request = _sample_request(sample)
        serialized = json.dumps(_request_input(request), ensure_ascii=False, default=str)
        estimated = estimate_request_tokens(request)
        replayed = replay_usage(request)
        # Grow this sample's conversation part (all but system and tools) at
        # its own estimate/real rate until the estimate reaches the trigger.
        fixed = replace(request, messages=request.messages[:1], continuation=None)
        fixed_estimate = estimate_request_tokens(fixed)
        fixed_real = replay_usage(fixed)
        trigger_real = fixed_real + (TRIGGER - fixed_estimate) * (sample.real - fixed_real) / (
            estimated - fixed_estimate
        )
        rows.append(
            {
                "kind": sample.kind,
                "messages": len(request.messages),
                "sample_bytes": sample.bytes,
                "rebuilt_bytes": len(serialized.encode()),
                "rebuilt_chars": _chars(serialized),
                "real": sample.real,
                "estimated": estimated,
                "replayed": replayed,
                "ratio": round(estimated / sample.real, 3),
                "replay_ratio": round(replayed / sample.real, 3),
                "projected_trigger_real": round(trigger_real),
            }
        )
    ratios = [row["ratio"] for row in rows]
    triggers = [row["projected_trigger_real"] for row in rows]
    RESULTS["C1-replayed-pairs"] = {
        "samples": rows,
        "ratio_median": statistics.median(ratios),
        "ratio_min": min(ratios),
        "ratio_max": max(ratios),
        "projected_trigger_real_median": statistics.median(triggers),
        "projected_trigger_real_min": min(triggers),
        "projected_trigger_real_max": max(triggers),
    }
    # The rebuilt requests carry the production composition.
    assert all(abs(row["rebuilt_bytes"] / row["sample_bytes"] - 1) <= 0.10 for row in rows), rows
    # The replay used by C2 tracks every real pair.
    assert all(abs(row["replay_ratio"] - 1) <= 0.12 for row in rows), rows
    assert 1.1 <= statistics.median(ratios) <= 1.35, ratios
    # The largest, ASCII-heaviest group sample sits at about 1.0 (1.008 from its
    # exact production counts): ASCII keeps 3 characters per token by decision.
    assert min(ratios) >= 0.98, ratios
    assert max(ratios) <= 1.5, ratios
    # The default trigger lands near 80k real tokens across these compositions;
    # the two 7–9 message samples extrapolate a tiny conversation part.
    assert 75000 <= statistics.median(triggers) <= 86000, triggers
    assert min(triggers) >= 60000 and max(triggers) <= 105000, triggers


def _kind(request: ChatRequest) -> str:
    content = request.messages[-1].content or ""
    if content.startswith(PUBLIC_COMPACT_INSTRUCTION):
        return "public_compact"
    if content.startswith(EXECUTION_COMPACT_INSTRUCTION):
        return "execution_compact"
    return "main"


@pytest.mark.parametrize(
    ("composition", "event_ascii", "event_cjk", "expected"),
    [
        # Mostly Chinese chat lines.
        ("chinese", 120, 300, (72000, 90000)),
        # Chat with IDs, numbers and links: ASCII:CJK 2:1 before the envelope.
        ("mixed", 500, 250, (85000, 100000)),
    ],
)
async def test_default_trigger_compacts_near_eighty_thousand_real_tokens(
    database, tmp_path, composition, event_ascii, event_cjk, expected
):
    """C2: growing group chat, real usage replayed by the fake provider."""
    trace: list[dict[str, Any]] = []

    def respond(request: ChatRequest) -> ChatResponse:
        kind = _kind(request)
        usage = replay_usage(request)
        trace.append(
            {
                "kind": kind,
                "messages": len(request.messages),
                "estimated": estimate_request_tokens(request),
                "replayed_usage": usage,
                "content_chars": _chars("".join(item.content or "" for item in request.messages)),
            }
        )
        if kind == "public_compact":
            return ChatResponse("PUBLIC_CHECKPOINT: 前面的讨论已记录。", 0, prompt_tokens=usage)
        return ChatResponse("NO_REPLY", 0, prompt_tokens=usage)

    env = await social_env(database, tmp_path)
    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=False,
        enabled_groups_csv="20001",
        context_compact_trigger_tokens=TRIGGER,
    )
    harness = build_harness(database, settings, provider)
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    scope = ConversationScope.group("80001", "20001")
    line = _mixed(event_ascii, event_cjk)
    seeded = 0

    async def seed(count: int) -> None:
        nonlocal seeded
        for _ in range(count):
            seeded += 1
            await seed_scoped_event(
                harness.scoped_events,
                scope=scope,
                origin_key=f"calibration-history-{seeded}",
                sender_user_id="10001",
                direction="inbound",
                content=f"{seeded:05d} {line}",
            )

    await seed(60)
    turn = 0
    while not any(item["kind"] == "public_compact" for item in trace):
        turn += 1
        assert turn <= 40, trace
        message = replace(
            inbound(
                f"第{turn}轮：继续 {line}",
                message_id=f"calibration-turn-{turn}",
                user_id="10001",
                group_id="20001",
                mentions_bot=True,
            ),
            bot_user_id="80001",
            conversation_id=env.context.conversation_id,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        )
        result = await harness.processor.handle(message, MemorySender())
        assert result.reason == "chat", result
        await seed(10)
    compact_index = next(
        index for index, item in enumerate(trace) if item["kind"] == "public_compact"
    )
    compact = trace[compact_index]
    before = trace[compact_index - 1]
    after = trace[compact_index + 1]
    RESULTS[f"C2-trigger-point-{composition}"] = {
        "seeded_events": seeded,
        "turns": turn,
        "trace": trace,
        "compact": compact,
        "last_main_before_compact": before,
        "first_main_after_compact": after,
    }
    assert before["kind"] == "main" and before["estimated"] < TRIGGER
    assert compact["estimated"] >= TRIGGER * 0.95
    assert after["kind"] == "main" and after["estimated"] < TRIGGER
    # The compact request carries the public prefix that crossed the trigger.
    assert expected[0] <= compact["replayed_usage"] <= expected[1], compact
    assert all(
        item["estimated"] >= item["replayed_usage"] for item in trace if item["kind"] == "main"
    ), trace
