"""E2E: execution compact only runs when it can bring the request below the trigger (#646).

Production shape (2026-11-22 data on #646): a Responses Main carries encrypted
reasoning, so a round in progress can only use execution compact. The soft
trigger asked only whether the tail held an assistant/tool entry, never whether
compaction could bring the request below the trigger. When the retained part
(initial material plus the round the normal Main has not observed yet) alone
stays above the trigger, every tool round bought another summary that saved
1–2k tokens: one round paid 134 compactions for 135 Main requests.
``conversation-compact.md`` says: if the request still sits above the trigger
after compaction, keep working while it fits; do not buy summaries repeatedly.

Failure cases written before the change:

R1 every tool round crosses the trigger, and the retained part alone is above
   it: no execution compact is bought (the old code buys one per request), the
   round keeps its original chain and finishes with every original tool result;
   the next turn starts with a public compact.
R2 one large round crosses the trigger, later rounds are small: compaction is
   skipped while the large round must be retained, runs exactly once when it
   can reach below the trigger, and the same round never compacts again. The
   paid compact keeps its receipts (``dispatched → response → paired`` for an
   ordinary turn, the Work journal checkpoint for a Work) and its
   ``model_invocations`` row names the pre-compact chain.
R3 a known hard capacity the retained round cannot fit: the existing capacity
   failure stays (a Work is suspended with ``model_request_capacity``), no
   compact request is bought.

Main, Work, Social, compaction, the Responses adapter, the executor, the
diagnostic writer and SQLite are real; only the model replies are scripted at
the HTTP boundary. Evidence is written to
``test-results/execution-compact-retrigger-e2e.json``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from tests.conftest import MemorySender, build_harness, make_settings
from tests.integration.test_model_invocation_request_kind_e2e import Wire, chained, oracle, tool
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env

from yuki.domain.messages import ChatResponse
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import journal, work
from yuki.runtime.work_session import WorkSession

TRIGGER = 30000
# About 33k estimated tokens: one such round alone exceeds the trigger.
LARGE = "long part; " * 9000
CHECKPOINT = "EXECUTION_CHECKPOINT: the parts delivered so far are recorded."
ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "execution-compact-retrigger-e2e.json"
RESULTS: dict[str, Any] = {}


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    patch = subprocess.check_output(["git", "diff", "HEAD", "--", "src", "tests"], cwd=ROOT)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_execution_compact_retrigger_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "source_revision": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "source_patch_sha256": hashlib.sha256(patch).hexdigest(),
                "trigger_tokens": TRIGGER,
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


async def _compact_receipts(database):
    async with database.read_session() as reader:
        rows = (
            await reader.execute(
                text(
                    "SELECT source_key, payload_json FROM model_context_observations "
                    "WHERE source_key LIKE 'compact:%' ORDER BY created_at"
                )
            )
        ).mappings()
        return [
            {"source_key": row["source_key"], "phase": json.loads(row["payload_json"])["phase"]}
            for row in rows
        ]


async def _run(database, tmp_path, monkeypatch, entry, sends, *, capacity=None):
    """Two turns in one group: the long round, then a following user message."""
    journal_saves = []
    save = WorkSession.save

    async def record_save(self, phase, *args, **kwargs):
        # The Work's compaction journal: dispatched/response carry the pending
        # compaction, paired publishes it with a new compact chain link.
        await save(self, phase, *args, **kwargs)
        journal_saves.append(
            {
                "phase": phase,
                "compaction": "context_compaction" in self.progress,
                "compact_links": sum(
                    link["reason"] == "compact" for link in self.progress.get("chain_links", ())
                ),
            }
        )

    monkeypatch.setattr(WorkSession, "save", record_save)
    goal = "WORK_GOAL: deliver every part, then wait for review."
    parts = 0

    def respond(request):
        nonlocal parts
        kind = oracle(request)
        if kind == "execution_compact":
            return ChatResponse(CHECKPOINT, 0)
        if kind == "public_compact":
            return ChatResponse("PUBLIC_CHECKPOINT: earlier parts delivered.", 0)
        if entry == "work":
            control = current_work_control.get()
            if control is None or control.current is None:
                return tool("manage_task", {"action": "start", "goal": goal}, "accept-work")
        if parts < len(sends):
            parts += 1
            return tool("send", {"text": sends[parts - 1]}, f"part-{parts}")
        if entry == "work":
            return tool("wait", {"for": [{"seconds": 3600}]}, f"wait-{len(provider.requests)}")
        return ChatResponse("NO_REPLY", 0)

    env = await social_env(database, tmp_path)
    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=entry == "work",
        enabled_groups_csv="20001",
        context_compact_trigger_tokens=TRIGGER,
    )
    harness = build_harness(database, settings, provider)
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    results = []
    async with Wire(database, provider, "responses") as wire:
        chat.runtime.runner._models = wire.executor
        if capacity is not None:
            wire.executor.capacity = lambda _: ModelCapacity(input_tokens=capacity)
        try:
            results.append(
                await harness.processor.handle(chained(env, goal, "long-round"), MemorySender())
            )
            first_turn = len(wire.sent)
            first_receipts = await _compact_receipts(database)
            first_saves = len(journal_saves)
            async with database.read_session() as reader:
                delivered = list(
                    await reader.scalars(
                        text(
                            "SELECT content FROM chat_events "
                            "WHERE direction = 'outbound' ORDER BY id"
                        )
                    )
                )
            results.append(
                await harness.processor.handle(
                    chained(env, "Anything else?", "next-turn"), MemorySender()
                )
            )
        finally:
            await wire.writer.close()
            rows = await wire.rows()
    async with database.read_session() as reader:
        works = [
            dict(row)
            for row in (await reader.execute(select(work).order_by(work.c.created))).mappings()
        ]
        journals = [dict(row) for row in (await reader.execute(select(journal))).mappings()]
    objects = WorkJournal(WorkRepository(database)).objects
    progress = [
        (await objects.hydrate(json.loads(row["payload_json"])))
        .get("metadata", {})
        .get("progress", {})
        for row in journals
    ]
    facts = [
        {
            "kind": oracle(item["request"]),
            "estimated_tokens": estimate_request_tokens(item["request"]),
            "request_chain_id": item["request"].request_chain_id,
            "continuation": item["request"].continuation is not None,
        }
        for item in wire.sent
    ]
    return {
        "first_turn": facts[:first_turn],
        "next_turn": facts[first_turn:],
        "results": [result.reason for result in results],
        "delivered": delivered,
        "rows": [
            {key: row.get(key) for key in ("request_kind", "request_chain_id", "success")}
            for row in rows
        ],
        "works": [
            {key: row[key] for key in ("state", "reason", "model_requests", "tool_calls")}
            for row in works
        ],
        "compact_receipts": first_receipts,
        "journal_saves": journal_saves[:first_saves],
        "chain_links": [link for value in progress for link in value.get("chain_links", [])],
        "execution_checkpoints": [
            value["task_material"]["execution_checkpoint"]
            for value in progress
            if value.get("task_material", {}).get("execution_checkpoint")
        ],
    }


def _parts(texts):
    """Part markers in delivery order; a long part is split into several messages."""
    return [text_.split()[0] for text_ in texts if text_.startswith("PART_")]


def _kinds(facts):
    return [fact["kind"] for fact in facts]


@pytest.mark.parametrize("entry", ["ordinary", "work"])
async def test_round_that_cannot_compact_below_trigger_continues_without_compaction(
    database, tmp_path, monkeypatch, entry
):
    """R1: the counting case. The old code buys one compact per Main request."""
    sends = [f"PART_{index} " + LARGE for index in range(1, 7)]
    facts = await _run(database, tmp_path, monkeypatch, entry, sends)
    RESULTS[f"R1-{entry}"] = facts
    first = facts["first_turn"]
    mains = [fact for fact in first if fact["kind"] == "main"]
    after_first_send = mains[2 if entry == "work" else 1 :]
    # Precondition: every request after the first large round crosses the trigger.
    assert after_first_send and all(f["estimated_tokens"] >= TRIGGER for f in after_first_send)
    assert _kinds(first).count("execution_compact") == 0, _kinds(first)
    assert len(first) == len(sends) + (2 if entry == "work" else 1)
    # The round stays on its original provider chain and delivers every part.
    assert len({fact["request_chain_id"] for fact in first[1:]}) == 1
    assert all(fact["continuation"] for fact in first[1:])
    assert _parts(facts["delivered"]) == _parts(sends)
    assert facts["compact_receipts"] == []
    assert not [save for save in facts["journal_saves"] if save["compaction"]]
    if entry == "work":
        assert facts["works"][0]["model_requests"] == len(first)
    assert not [link for link in facts["chain_links"] if link["reason"] == "compact"]
    # The following turn begins with the public compact instead.
    assert _kinds(facts["next_turn"])[:2] == ["public_compact", "main"]
    assert facts["next_turn"][1]["estimated_tokens"] < TRIGGER
    assert [row["request_kind"] for row in facts["rows"]] == _kinds(
        facts["first_turn"] + facts["next_turn"]
    )


@pytest.mark.parametrize("entry", ["ordinary", "work"])
async def test_compaction_that_reaches_below_trigger_runs_once_in_the_round(
    database, tmp_path, monkeypatch, entry
):
    """R2: skipped while the large round is retained, then exactly one compact."""
    sends = ["PART_1 " + LARGE, "PART_2 short", "PART_3 short", "PART_4 short"]
    facts = await _run(database, tmp_path, monkeypatch, entry, sends)
    RESULTS[f"R2-{entry}"] = facts
    first = facts["first_turn"]
    kinds = _kinds(first)
    assert kinds.count("execution_compact") == 1, kinds
    compact = kinds.index("execution_compact")
    # The request that retained the unobserved large round crossed the trigger
    # without buying a summary that could not reach below it.
    skipped = first[compact - 1]
    assert skipped["kind"] == "main" and skipped["estimated_tokens"] >= TRIGGER
    assert skipped["request_chain_id"] == first[compact]["request_chain_id"]
    assert first[compact + 1]["request_chain_id"] != first[compact]["request_chain_id"]
    assert all(fact["estimated_tokens"] < TRIGGER for fact in first[compact + 1 :])
    assert _parts(facts["delivered"]) == _parts(sends)
    assert [row["request_kind"] for row in facts["rows"][: len(first)]] == kinds
    assert facts["rows"][compact]["request_chain_id"] == first[compact - 1]["request_chain_id"]
    if entry == "ordinary":
        assert [receipt["phase"] for receipt in facts["compact_receipts"]] == [
            "dispatched",
            "response",
            "paired",
        ]
        assert len({r["source_key"].rsplit(":", 1)[0] for r in facts["compact_receipts"]}) == 1
    else:
        assert facts["compact_receipts"] == []
        compacting = [
            index for index, save in enumerate(facts["journal_saves"]) if save["compaction"]
        ]
        phases = [facts["journal_saves"][index]["phase"] for index in compacting]
        assert phases == ["dispatched", "response"], facts["journal_saves"]
        published = facts["journal_saves"][compacting[-1] + 1]
        assert published == {"phase": "paired", "compaction": False, "compact_links": 1}
        assert facts["journal_saves"][-1]["compact_links"] == 1
        assert CHECKPOINT in facts["execution_checkpoints"]
        assert {
            "from": first[compact]["request_chain_id"],
            "to": first[compact + 1]["request_chain_id"],
            "reason": "compact",
        } in facts["chain_links"]
        # The paid compact counts in the original Work budget, once.
        assert facts["works"][0]["model_requests"] == len(first)


@pytest.mark.parametrize("entry", ["ordinary", "work"])
async def test_hard_capacity_the_retained_round_cannot_fit_keeps_capacity_failure(
    database, tmp_path, monkeypatch, entry
):
    """R3: the soft rule never replaces the known hard capacity failure."""
    sends = ["PART_1 " + LARGE, "PART_2 never reached"]
    facts = await _run(database, tmp_path, monkeypatch, entry, sends, capacity=50000)
    RESULTS[f"R3-{entry}"] = facts
    first = facts["first_turn"]
    assert _kinds(first) == ["main"] * (2 if entry == "work" else 1)
    assert _parts(facts["delivered"]) == ["PART_1"]
    assert facts["compact_receipts"] == []
    assert not [save for save in facts["journal_saves"] if save["compaction"]]
    if entry == "ordinary":
        assert facts["results"][0] == "capacity_failure"
    else:
        (saved,) = facts["works"]
        assert saved["state"] == "suspended"
        assert saved["reason"] == "model_request_capacity"
        assert saved["model_requests"] == len(first)
        assert saved["tool_calls"] == 1
