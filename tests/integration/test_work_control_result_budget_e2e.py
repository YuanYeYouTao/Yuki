"""E2E: lifecycle control results use the ordinary result budget; get_tasks steps do not nest.

Replays the 2026-11-18 production incident (Work ``e80d820c``): the Main polled its
own Work with ``get_tasks(id=...)``. Each call embedded the previous ``get_tasks``
receipt verbatim, so every receipt doubled its JSON escaping (121,923 →
2,769,473 bytes in eight calls). Control results bypassed ``ToolResultBudgeter``,
the last one alone was estimated at ~975k input tokens, the request exceeded the
1,041,808 input budget and compaction could not shrink the uncompactable current
round, so the Work was suspended with ``model_request_capacity``; a later
ordinary private turn failed the same way after a single ``get_tasks``.

Failure cases written before the implementation:

1. Repeated ``get_tasks(id=<own Work>)`` grows without bound: each result embeds the
   previous ``get_tasks`` result (a read-only control) as a nested JSON string.
2. A Work that already holds a huge legacy receipt (written before control results
   were budgeted) makes ``get_tasks(id)`` return it unbounded, whatever ``limit``.
3. An ordinary private turn without an owning Work that calls ``get_tasks(id)``
   receives the same unbounded result and dies on input capacity.
4. The receipt persisted for a control call differs from, or is larger than, what
   the model saw, so a recovery replay of the original call key would hand back
   an unbounded text (replay itself must not re-execute; it returns the receipt).
6. Control payload outside ``steps`` (for example a long goal) is not budgeted;
   the bound must hold for the whole control result, not only for nested steps.
7. ``get_tasks`` without ``id`` (directory listing) starts carrying steps.

Real results are kept (main-agent-runtime.md: single reads give the original
effects' real results, no progress summary); an oversized result becomes an
artifact with a bounded preview, ``artifact_handle`` and ``read_more`` exactly
like business tools (tool-results.md). Evidence is written to
``test-results/work-control-result-budget-e2e.json``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select
from tests.conftest import MemorySender
from tests.integration.test_execution_continuity_e2e import (
    ScriptedProvider,
    _call,
    _private,
    _private_scene,
)

from yuki.runtime.work_schema_v1 import effects, work
from yuki.tool_results.artifacts import ToolArtifactRepository

BUDGET = 6000
ARTIFACT = (
    Path(__file__).resolve().parents[2] / "test-results" / "work-control-result-budget-e2e.json"
)
RESULTS: dict[str, Any] = {}


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_work_control_result_budget_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "budget_characters": BUDGET,
                "scenarios": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )


async def _scene(database, tmp_path, provider):
    env, harness, chat, _ = await _private_scene(
        database, tmp_path, provider, agent_tool_result_max_characters=BUDGET
    )
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "results", retention_seconds=600, max_artifact_bytes=10_000_000
    )
    return env, harness, chat


def _texts(request) -> dict[str, str]:
    return {
        message.tool_call_id: message.content or ""
        for message in request.messages
        if message.role == "tool" and message.tool_call_id
    }


async def _receipts(database, work_id: str) -> dict[str, dict[str, Any]]:
    async with database.read_session() as reader:
        rows = (
            (await reader.execute(select(effects).where(effects.c.work_id == work_id)))
            .mappings()
            .all()
        )
    return {row["effect_key"].rsplit(":", 1)[-1]: json.loads(row["receipt_json"]) for row in rows}


def _nested_readonly_results(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in view.get("steps", ())
        if isinstance(step, dict) and step.get("tool") == "get_tasks" and "result" in step
    ]


async def test_polling_own_work_with_get_tasks_stays_bounded(database, tmp_path):
    """Cases 1, 4 and 7 on one admitted private Work."""
    polls = 6
    phase: dict[str, Any] = {}

    def respond(request):
        number = len(provider.requests)
        if number == 1:
            return _call("set_short_note", {"slot": 1, "text": "copied"}, "copy")
        if number == 2:
            phase["work_id"] = provider.ownership[-1]
        if 2 <= number < 2 + polls:
            return _call("get_tasks", {"id": phase["work_id"]}, f"poll-{number - 1}")
        if number == 2 + polls:
            return _call("get_tasks", {}, "listing")
        if number == 3 + polls:
            return _call("send", {"text": "好了"}, "deliver")
        return "done"

    provider = ScriptedProvider(respond)
    env, harness, _ = await _scene(database, tmp_path, provider)
    result = await harness.processor.handle(_private(env, "复制并核对", "poll"), MemorySender())
    assert result.reason == "chat"
    work_id = phase["work_id"]
    assert work_id is not None
    final = provider.requests[-1]
    texts = _texts(final)
    views = {identity: json.loads(text) for identity, text in texts.items()}
    sizes = [len(texts[f"poll-{index}"]) for index in range(1, polls + 1)]
    # Case 1: no result nests a read-only control result, every poll fits the budget
    # and keeps the real set_short_note result (no summary replaces it).
    for index in range(1, polls + 1):
        view = views[f"poll-{index}"]
        assert len(texts[f"poll-{index}"]) <= BUDGET
        assert "truncated" not in view
        assert _nested_readonly_results(view) == []
        tools = [step.get("tool") for step in view["steps"]]
        assert "set_short_note" in tools
        note = next(step for step in view["steps"] if step.get("tool") == "set_short_note")
        assert "result" in note
        # Each earlier poll and the running call itself are listed by name and status.
        assert tools.count("get_tasks") == index
    # Growth is per listed step, never a doubling of the previous result.
    for before, after in pairwise(sizes):
        assert after - before < 1000
    # Case 4: the persisted receipt is the same bounded text the model saw.
    receipts = await _receipts(database, work_id)
    for index in range(1, polls + 1):
        assert receipts[f"poll-{index}"]["result"] == texts[f"poll-{index}"]
    # Case 7: the directory listing carries no steps.
    listing = views["listing"]
    assert listing["tasks"] and all("steps" not in task for task in listing["tasks"])
    RESULTS["polling_own_work"] = {
        "work_id": work_id,
        "poll_result_characters": sizes,
        "nested_readonly_results": sum(
            len(_nested_readonly_results(views[f"poll-{index}"])) for index in range(1, polls + 1)
        ),
        "receipt_equals_model_text": True,
        "listing_has_steps": any("steps" in task for task in listing["tasks"]),
    }


async def test_huge_existing_receipt_and_goal_are_bounded_in_an_ordinary_turn(
    database, tmp_path, monkeypatch
):
    """Cases 2, 3 and 6: a previous Work holds a huge legacy receipt and goal."""
    phase: dict[str, Any] = {"name": "first"}
    long_goal = "整理并核对外部 Agent 事件化改造的全部回执。" * 600

    def respond(request):
        number = len(provider.requests)
        if phase["name"] == "first":
            if number == 1:
                return _call("set_short_note", {"slot": 1, "text": "copied"}, "copy")
            if number == 2:
                phase["work_id"] = provider.ownership[-1]
                return _call("manage_task", {"action": "update", "goal": long_goal}, "goal")
            if number == 3:
                return _call("get_tasks", {"id": phase["work_id"]}, "own")
            if number == 4:
                return _call("send", {"text": "好了"}, "deliver")
            return "done"
        if number == phase["start"] + 1:
            phase["ownership"] = provider.ownership[-1]
            return _call("get_tasks", {"id": phase["work_id"], "limit": 1}, "lookup")
        if number == phase["start"] + 2:
            return _call("send", {"text": "看过了"}, "answer")
        return "done"

    provider = ScriptedProvider(respond)
    env, harness, _ = await _scene(database, tmp_path, provider)
    first = await harness.processor.handle(_private(env, "复制一下", "first"), MemorySender())
    assert first.reason == "chat"
    work_id = phase["work_id"]
    own = _texts(provider.requests[3])["own"]
    # Case 6: a long goal outside steps is still bounded and archived.
    assert len(own) <= BUDGET
    own_view = json.loads(own)
    assert own_view["truncated"] is True and own_view["artifact_handle"]
    # Seed a receipt written before control results were budgeted (production
    # shape: a 2.7 MB raw control result on the newest effect).
    legacy = json.dumps(
        {"id": work_id, "steps": [{"tool": "manage_task", "result": "回执" * 200_000}]},
        ensure_ascii=False,
    )
    now = time.time() + 60
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(effects).values(
                effect_key=f"legacy-chain:99:legacy-{work_id}",
                work_id=work_id,
                kind="tool",
                state="accepted",
                receipt_json=json.dumps(
                    {
                        "result": legacy,
                        "outcome": {
                            "tool": "manage_task",
                            "ok": True,
                            "side_effecting": True,
                            "executed": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                created=now,
                updated=now,
            )
        )
    phase.update(name="second", start=len(provider.requests))
    second = await harness.processor.handle(_private(env, "刚才做了什么", "second"), MemorySender())
    # Case 3: the ordinary turn has no owning Work and completes.
    assert second.reason == "chat"
    assert phase["ownership"] is None
    lookup_text = _texts(provider.requests[phase["start"] + 1])["lookup"]
    lookup = json.loads(lookup_text)
    # Case 2: the huge legacy receipt is delivered as a bounded preview + artifact.
    assert len(lookup_text) <= BUDGET
    assert lookup["truncated"] is True and lookup["artifact_handle"]
    assert lookup["original_characters"] > 400_000
    async with database.read_session() as reader:
        state = await reader.scalar(select(work.c.state).where(work.c.id == work_id))
    RESULTS["huge_existing_receipt_ordinary_turn"] = {
        "work_state": state,
        "own_result_characters": len(own),
        "own_original_characters": own_view["original_characters"],
        "lookup_result_characters": len(lookup_text),
        "lookup_original_characters": lookup["original_characters"],
        "ordinary_turn_owner": phase["ownership"],
        "ordinary_turn_reason": second.reason,
    }
