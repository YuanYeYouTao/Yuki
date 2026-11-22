"""Offline replay of the one-activity-one-episode self-reflection rule (P2.1 §14.1).

Takes the most recent N completed chat self-reflection batches, rebuilds each batch's
model input exactly as ``SelfReflectionService`` does, and asks the production
``memory_self_reflection`` model route twice: once with the instruction and output schema
before P2.1 §8.1 ("old") and once with the current ones ("new"). Nothing is written to
memory: the source database is opened read-only and copied with SQLite's backup API into
a temporary file; the model runtime records its invocations in that copy, never in the
source.

The report lists, per batch, both outputs' episode titles, passages and evidence aliases,
and every old episode that shares no evidence alias with any new episode
(``old_without_new_counterpart``): judge in the deployment environment whether each of
those should have been dropped. The report holds chat-derived text and stays in the
deployment environment. Tool receipts past their retention are no longer in the input
(``tool_receipts`` shows how many were). This script calls a paid model and is not
product code.

Usage (inside the deployment environment, with the bot's model configuration):
    python scripts/replay_self_reflection_one_episode.py --source data/yuki.db --runs 20 \\
        --output test-results/self-reflection-one-episode/replay.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from pydantic import Field
from sqlalchemy import select

from yuki.cli import _cli_models
from yuki.config import Settings
from yuki.memory.claim_processor import MemoryClaimProcessor
from yuki.memory.mutation.service import MemoryMutationService
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.self_reflection.models import (
    SelfEpisodeProposal,
    SelfReflectionBatch,
    SelfReflectionInput,
    SelfReflectionOutput,
)
from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.memory.self_reflection.service import _INSTRUCTION, SelfReflectionService
from yuki.memory.service import MemoryFactService
from yuki.model_runtime.executor import ModelExecutor
from yuki.model_runtime.models import ModelExecutionPriority, ModelTask
from yuki.model_runtime.structured import StructuredTaskError, StructuredTaskRunner
from yuki.persistence.database import Database
from yuki.persistence.models import (
    ChatEventModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
)
from yuki.persistence.repositories import EventLedgerRepository
from yuki.persistence.repository_helpers import _event_record

CURRENT_EPISODE_RULE = """\
episodes 是创建 Episode 的唯一输出位置，记录你在当前群聊或私聊中真实参与过、值得长期记住的经历。
同一段活动只写一条：一局游戏、一次讨论、一次排查、一次闲聊各算一条，中间的回合、插话和阶段不拆开；
本批里互不相干的几段活动才分成几条。本批只是 context_events 里已经开始的同一段活动的继续、
又没有新的结果或转折时，不再另写。没有值得长期记住的事时 episodes 为空。
同一天、同一群或都是自己参与，不足以合并为一条。
"""
LEGACY_EPISODE_RULE = """\
episodes 是创建 Episode 的唯一输出位置，记录你在当前群聊或私聊中真实参与过、值得长期记住的
经历。一次可以输出多条 Episode；每条只写一个核心经历及其直接进展，彼此无因果关联的话题分成
不同 Episode 或舍弃不值得保留的部分。同一天、同一群或都是自己参与，不足以合并为一条。
"""
TEMPERATURE = 0.1  # SelfReflectionService.reflect
CONTEXT_EVENTS = 4  # SelfReflectionRepository.claim_due default


class LegacySelfReflectionOutput(SelfReflectionOutput):
    """The output schema before P2.1 §8.1: no description on ``episodes``."""

    episodes: tuple[SelfEpisodeProposal, ...] = Field(default=())


def arms() -> dict[str, tuple[str, type[SelfReflectionOutput]]]:
    """Instruction template and output schema of each arm, old first."""

    if CURRENT_EPISODE_RULE not in _INSTRUCTION:
        raise RuntimeError("current self-reflection episode rule changed; update this script")
    return {
        "old": (
            _INSTRUCTION.replace(CURRENT_EPISODE_RULE, LEGACY_EPISODE_RULE),
            LegacySelfReflectionOutput,
        ),
        "new": (_INSTRUCTION, SelfReflectionOutput),
    }


def _copy_read_only(source: Path, target: Path) -> None:
    origin = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
    finally:
        copy.close()
        origin.close()


async def _batches(
    database: Database, repository: SelfReflectionRepository, settings: Settings, runs: int
) -> list[SelfReflectionBatch]:
    """Rebuild the most recent completed chat batches from their recorded event ranges."""

    run_model, state_model = MemorySelfReflectionRunModel, MemorySelfReflectionStateModel
    batches: list[SelfReflectionBatch] = []
    async with database.read_session() as reader:
        recent = list(
            await reader.scalars(
                select(run_model)
                .where(run_model.status == "completed", run_model.first_event_id > 0)
                .order_by(run_model.id.desc())
                .limit(runs)
            )
        )
        for run in reversed(recent):
            owner = (
                state_model.canonical_person_id == run.canonical_person_id
                if run.canonical_person_id is not None
                else state_model.canonical_space_id == run.canonical_space_id
            )
            state_row = await reader.scalar(select(state_model).where(owner))
            if state_row is None:
                raise SystemExit(f"run {run.id}: its reflection owner has no state row")
            state = await SelfReflectionRepository._state(reader, state_row)
            events = await reader.scalars(
                repository._apply_event_scope(
                    select(ChatEventModel).where(
                        ChatEventModel.id >= run.first_event_id,
                        ChatEventModel.id <= run.last_event_id,
                        ChatEventModel.event_kind == "message",
                    ),
                    state_row,
                ).order_by(ChatEventModel.id)
            )
            context = await reader.scalars(
                repository._apply_event_scope(
                    select(ChatEventModel).where(
                        ChatEventModel.id < run.first_event_id,
                        ChatEventModel.event_kind == "message",
                    ),
                    state_row,
                )
                .order_by(ChatEventModel.id.desc())
                .limit(CONTEXT_EVENTS)
            )
            batches.append(
                SelfReflectionBatch(
                    state=state,
                    events=tuple(_event_record(row) for row in events),
                    context_events=tuple(_event_record(row) for row in reversed(list(context))),
                    trigger_reason=run.trigger_reason,
                    scheduled_slot=run.scheduled_slot,
                    run_id=run.id,
                    max_input_characters=settings.memory_self_reflection_max_characters,
                )
            )
    return batches


async def _generate(
    runner: StructuredTaskRunner,
    settings: Settings,
    payload: SelfReflectionInput,
    allowed: set[str],
    template: str,
    output_model: type[SelfReflectionOutput],
) -> dict[str, Any]:
    """Mirror the request of ``SelfReflectionService.reflect`` with one arm's contract."""

    def validate(output: SelfReflectionOutput) -> None:
        for kind, items in (("proposals", output.proposals), ("episodes", output.episodes)):
            for index, item in enumerate(items):
                if any(ref not in allowed for ref in item.evidence_refs):
                    raise StructuredTaskError(
                        "self-reflection referenced unavailable evidence",
                        reason_code="unknown_reference",
                        detail=f"{kind}.{index}",
                    )

    try:
        generated = await runner.run(
            task=ModelTask.MEMORY_SELF_REFLECTION,
            instruction=(
                template.format(timezone=settings.memory_self_reflection_timezone)
                + "\n【角色身份与人格参考】\n"
                + settings.character_prompt
            ),
            structured_input=payload,
            output_model=output_model,
            temperature=TEMPERATURE,
            max_output_tokens=settings.memory_self_reflection_max_output_tokens,
            # Same request; the reflection's best-effort priority would first wait out
            # the foreground quiet period of this freshly started process.
            priority=ModelExecutionPriority.MAINTENANCE,
            validation_retries=1,
            validate_output=validate,
            validation_repair_hint=(
                "Use only supplied event/tool aliases as evidence and correct the reported fields."
            ),
        )
    except StructuredTaskError as exc:
        return {"error": exc.reason_code}
    return {
        "count": len(generated.episodes),
        "proposals": len(generated.proposals),
        "episodes": [
            {
                "title": episode.title,
                "evidence_refs": list(episode.evidence_refs),
                "passages": [passage.content for passage in episode.passages],
            }
            for episode in generated.episodes
        ],
    }


def _totals(batches: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    done = [batch[arm] for batch in batches if "error" not in batch[arm]]
    processed = sum(batch["events"] for batch in batches if "error" not in batch[arm])
    episodes = sum(item["count"] for item in done)
    return {
        "episodes": episodes,
        "episodes_per_100_events": round(100 * episodes / processed, 2) if processed else None,
        "batches_with_3_plus_episodes": sum(item["count"] >= 3 for item in done),
        "batches_without_episode": sum(item["count"] == 0 for item in done),
        "errors": len(batches) - len(done),
    }


async def replay(
    database: Database, settings: Settings, models: ModelExecutor, *, runs: int
) -> dict[str, Any]:
    """Replay against ``database``, which must already be a disposable copy."""

    repository = SelfReflectionRepository(database)
    facts = MemoryFactService(MemoryFactRepository(database))
    service = SelfReflectionService(
        settings=settings,
        repository=repository,
        # Required by the constructor; the replay never commits a mutation.
        mutations=MemoryMutationService(
            settings=settings,
            facts=facts,
            processor=MemoryClaimProcessor(settings=settings, facts=facts),
            ledger=EventLedgerRepository(database),
        ),
        models=models,
    )
    contracts = arms()
    runner = StructuredTaskRunner(models)
    rows: list[dict[str, Any]] = []
    for batch in await _batches(database, repository, settings, runs):
        payload, event_map, tool_map = await service._input(batch)
        allowed = set(event_map) | set(tool_map)
        row: dict[str, Any] = {
            "run_id": batch.run_id,
            "scheduled_slot": batch.scheduled_slot,
            "scope_type": payload.scope_type.value,
            "first_event_id": batch.events[0].id,
            "last_event_id": batch.events[-1].id,
            "events": len(payload.events),
            "tool_receipts": len(payload.tool_receipts),
        }
        for arm, (template, output_model) in contracts.items():
            row[arm] = await _generate(runner, settings, payload, allowed, template, output_model)
        if "error" not in row["old"] and "error" not in row["new"]:
            covered = {ref for item in row["new"]["episodes"] for ref in item["evidence_refs"]}
            row["old_without_new_counterpart"] = [
                {"index": index, "title": item["title"], "evidence_refs": item["evidence_refs"]}
                for index, item in enumerate(row["old"]["episodes"], start=1)
                if not covered.intersection(item["evidence_refs"])
            ]
        rows.append(row)
    return {
        "model": models.model_name(ModelTask.MEMORY_SELF_REFLECTION),
        "temperature": TEMPERATURE,
        "runs_requested": runs,
        "instructions_sha256": {
            arm: hashlib.sha256(template.encode()).hexdigest()
            for arm, (template, _) in contracts.items()
        },
        "batches": rows,
        "totals": {
            "batches": len(rows),
            "processed_events": sum(row["events"] for row in rows),
            **{arm: _totals(rows, arm) for arm in contracts},
        },
    }


async def _main(source: Path, runs: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "replay.db"
        _copy_read_only(source, copy)
        url = f"sqlite+aiosqlite:///{copy.as_posix()}"
        database = Database(url)
        try:
            async with _cli_models(Settings(database_url=url), database) as (active, models):
                return await replay(database, active, models, runs=runs)
        finally:
            await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True, help="production SQLite file")
    parser.add_argument("--runs", type=int, default=20, help="most recent completed batches")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("test-results/self-reflection-one-episode/replay.json"),
    )
    args = parser.parse_args()
    report = asyncio.run(_main(args.source, args.runs))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
