"""Additional legacy, multi-parent and large uncertain-result boundaries; no external IO."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.support.codemode_cases import effect_rows, environment, outer_call
from tests.unit.test_history_preparation_reuse import context, snapshot

from qq_ai_bot.codemode.driver import CodeModeDriver
from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.work_session import PendingComposition
from qq_ai_bot.services.history_projection import prepare_history
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize("kind,renderer", [(None, None), ("emergency", None), ("emergency", 2)])
async def test_legacy_summary_requires_explicit_boundary(database, monkeypatch, kind, renderer):
    current = context((((1,), ChatMessage("user", "authorized current history")),))
    current = replace(
        current,
        rollup_text="current authorized summary",
        metrics=replace(current.metrics, rollup_mode="model"),
    )
    frozen = FrozenFragments.load([]).extend_history(
        current.history_fragments, current.history_event_fragments
    )
    previous = snapshot(
        frozen, summary="old body with unknown representation", kind=kind, renderer=renderer
    )
    monkeypatch.setattr(
        EventLedgerRepository,
        "read_scope_missing_history",
        AsyncMock(return_value=(current.read_version, ())),
    )
    prepared = await prepare_history(
        SimpleNamespace(database=database, read=AsyncMock(return_value=previous)),
        current,
        view_key="a" * 64,
        context_key="b" * 64,
        contract_revision="c" * 64,
        history_fits=lambda _: True,
        context_fits=lambda _: True,
        context_hard_fits=lambda _: True,
    )
    assert prepared.reason == "rollup"
    assert prepared.context.rollup_text == current.rollup_text
    assert prepared.context.metrics.rollup_mode == "model"
    assert prepared.context.read_version == current.read_version


@pytest.mark.parametrize("guard", ["pending", "provider_pause_replay", "compaction_staging"])
async def test_two_parent_results_rebase_only_after_all_protocol_guards_clear(
    database, tmp_path, guard
):
    env = await environment(database, tmp_path)
    owner = env.owner
    calls = tuple(ToolCall(f"parent-{i}", ToolFunction("execute_code", "{}")) for i in range(2))
    original = TurnTranscript(
        (
            ChatMessage("user", "old business history"),
            ChatMessage("assistant", tool_calls=calls),
            *(
                ChatMessage("tool", f"original result {i}", tool_call_id=call.id)
                for i, call in enumerate(calls)
            ),
        )
    )
    owner.transcript = original
    owner.uses_recovery_transcript = True
    old_chain = original.chain_id
    before = await effect_rows(database, env.control.current["id"])
    fresh = TurnTranscript((ChatMessage("user", "current authorized ordinary history"),))
    if guard == "pending":
        owner.pending_compositions = [
            PendingComposition(c.id, "original-operation-" + c.id, 1, "original-boundary")
            for c in calls
        ]
    else:
        owner.progress[guard] = {"original": True}
    assert await owner.rebase_business(fresh, append_material=False) is False
    assert owner.transcript is original and owner.uses_recovery_transcript
    if guard == "pending":
        owner.pending_compositions.pop()
        assert await owner.rebase_business(fresh, append_material=False) is False
        owner.pending_compositions.clear()
    else:
        owner.progress.pop(guard)
    assert await owner.rebase_business(fresh, append_material=False) is True
    assert owner.transcript.chain_id != old_chain
    evidence = json.loads(owner.transcript.request().messages[-1].content)
    assert [(c["call_id"], c["result"]) for c in evidence["calls"]] == [
        (c.id, f"original result {i}") for i, c in enumerate(calls)
    ]
    assert all(c["effect_key"].startswith(old_chain + ":") for c in evidence["calls"])
    assert "old business history" not in str(owner.transcript.request())
    assert "current authorized ordinary history" in str(owner.transcript.request())
    assert await owner.rebase_business(fresh, append_material=False) is False
    assert await effect_rows(database, env.control.current["id"]) == before


@pytest.mark.parametrize("limit", [2000, 24000])
async def test_large_unknown_parent_preserves_status_and_verifiable_full_archive(
    database, tmp_path, limit
):
    env = await environment(database, tmp_path)
    env.host.result_limit = limit
    archived = []

    async def archive(value):
        archived.append(value)
        return "synthetic-full-artifact"

    env.host.archive = archive
    body = {
        "ok": False,
        "status": "partial",
        "stop_reason": "unknown",
        "replay_forbidden": True,
        "stdout": "",
        "stdout_truncated": False,
        "uncertain_operation_id": "original-id-" + "x" * 30000,
        "operations": [
            {
                "operation_id": str(i) + "x" * 1000,
                "tool": "long-tool-" + "y" * 1000,
                "status": "unknown",
            }
            for i in range(20)
        ],
    }
    original = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    result = await CodeModeDriver(env.host, outer_call(env, "inert"))._bounded_result(
        body, "original-parent"
    )
    bounded = json.loads(result)
    assert len(result) <= limit
    assert (
        bounded["status"] == "partial"
        and bounded["stop_reason"] == "unknown"
        and bounded["replay_forbidden"] is True
    )
    assert bounded["stdout"] == "" and bounded["stdout_truncated"] is False
    assert bounded["operations_count"] == 20 and bounded["operations_truncated"] is True
    assert (
        bounded["operations_ref"]["parent_key_sha256"]
        == hashlib.sha256(b"original-parent").hexdigest()
    )
    assert bounded["summary_ref"] == "synthetic-full-artifact" and archived == [original]
    uncertain = json.dumps(json.loads(original)["uncertain_operation_id"], ensure_ascii=False)
    assert (
        bounded["uncertain_operation_id_ref_sha256"]
        == hashlib.sha256(uncertain.encode()).hexdigest()
    )
