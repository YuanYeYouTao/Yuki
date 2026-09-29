"""Evaluate authorized search_memory retrieval against a private labeled corpus.

The corpus, SQLite snapshot, and per-query results stay outside the repository.
This script reads an isolated database copy; it never starts a Bot or sends QQ.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.enums import MemoryRetrievalMode, MemoryScopeType
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryQuery
from qq_ai_bot.memory.query import normalize_query_text
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.persistence.database import Database


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _labels_by_key(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    rows = _read_jsonl(path)
    labels = {str(row["key"]): row for row in rows}
    if len(labels) != len(rows):
        raise ValueError("duplicate label key")
    return labels


async def evaluate(args: argparse.Namespace) -> None:
    if not args.snapshot.is_file():
        raise FileNotFoundError(args.snapshot)
    questions = _read_jsonl(args.questions)
    if len({row["key"] for row in questions}) != len(questions):
        raise ValueError("duplicate question key")
    labels = _labels_by_key(args.labels)
    if labels and set(labels) != {str(row["key"]) for row in questions}:
        raise ValueError("labels must cover exactly the supplied questions")

    database = Database("sqlite+aiosqlite:///" + args.snapshot.as_posix())
    repository = MemoryFactRepository(database)
    retriever = MemoryRetriever(
        repository=repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
    )
    results: list[dict[str, Any]] = []
    try:
        for row in questions:
            query_text = str(row["query"])
            scope = AuthorizedMemoryScope(
                requester_person_id=row.get("actor_person_id"),
                current_private_person_id=row.get("actor_person_id"),
                current_space_id=row.get("current_space_id"),
                allowed_scopes=(
                    MemoryScopeType.PERSON,
                    MemoryScopeType.PERSON_GROUP,
                    MemoryScopeType.GROUP,
                    MemoryScopeType.SELF,
                ),
            )
            query = MemoryQuery(
                text=query_text,
                normalized_text=normalize_query_text(query_text),
                mode=MemoryRetrievalMode.RELEVANT,
                targets=(),
                candidate_limit=args.candidate_limit,
                limit_per_target=args.limit,
                always_on_explicit_preference_limit=0,
                query_term_limit=12,
                semantic_enabled=False,
            )
            started = time.perf_counter()
            result = await retriever.retrieve_authorized(query, scope, limit=args.limit)
            elapsed_ms = (time.perf_counter() - started) * 1000
            hit_ids = [hit.fact.id for hit in result.hits]
            record: dict[str, Any] = {
                "key": row["key"],
                "hit_ids": hit_ids,
                "candidate_count": result.candidate_count,
                "ranked_count": result.ranked_count,
                "returned_count": len(hit_ids),
                "truncated": result.truncated,
                "exhaustive": result.exhaustive,
                "partial_reason": result.partial_reason,
                "semantic_status": result.semantic_status,
                "latency_ms": round(elapsed_ms, 2),
            }
            if labels:
                label = labels[str(row["key"])]
                gold = set(int(value) for value in label["direct_support_fact_ids"])
                answerable = bool(label["answerable"])
                if answerable != bool(gold):
                    raise ValueError(f"inconsistent label: {row['key']}")
                authorized_gold = await repository.get_active_authorized(scope, tuple(gold))
                if {fact.id for fact in authorized_gold} != gold:
                    raise ValueError(f"gold contains inactive or unauthorized fact: {row['key']}")
                record["answerable"] = answerable
                record["gold_count"] = len(gold)
                record["p_at_3"] = len(gold.intersection(hit_ids[:3])) / 3 if answerable else None
                record["recall_at_10"] = (
                    len(gold.intersection(hit_ids[:10])) / len(gold) if answerable else None
                )
                record["candidate_false_positive"] = not answerable and bool(hit_ids)
            results.append(record)
    finally:
        await database.close()

    args.output.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
        encoding="utf-8",
    )
    counters = Counter("answerable" if row.get("answerable") else "no_answer" for row in results)
    answerable = [row for row in results if row.get("answerable")]
    no_answer = [row for row in results if row.get("answerable") is False]
    summary = {
        "questions": len(results),
        "labels": dict(counters) if labels else None,
        "mean_p_at_3": statistics.mean(row["p_at_3"] for row in answerable) if answerable else None,
        "mean_recall_at_10": (
            statistics.mean(row["recall_at_10"] for row in answerable) if answerable else None
        ),
        "no_answer_candidate_rate": (
            sum(row["candidate_false_positive"] for row in no_answer) / len(no_answer)
            if no_answer
            else None
        ),
        "truncated": sum(row["truncated"] for row in results),
        "exhaustive": sum(row["exhaustive"] for row in results),
        "median_latency_ms": statistics.median(row["latency_ms"] for row in results),
        "p95_latency_ms": sorted(row["latency_ms"] for row in results)[
            max(0, int(len(results) * 0.95) - 1)
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-limit", type=int, default=50)
    parser.add_argument("--limit", type=int, default=10)
    asyncio.run(evaluate(parser.parse_args()))


if __name__ == "__main__":
    main()
