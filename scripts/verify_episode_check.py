"""Acceptance check for episode index visibility (layered memory P2 §11.2, P2.1 §14.1).

``index_fact_ids`` of the check report holds every listed ID of the index (P2.1: the
long-term impressions and the recent window), ``recent_fact_ids`` today's new
episodes; all of them must satisfy the same rules.

Independent of the renderer on purpose: it re-checks every fact ID reported by
``yuki memory episodes check`` against the task book §5.3 conditions with its own
read-only SQL (plain sqlite3, ``mode=ro``), and prints the violation count and the
offending fact IDs. It never writes and is not product code.

Rules (P1 evidence rules, P2 owner decisions P2-Q2 / P2-Q3): a private conversation
with P may list P's person / person_group episodes and SELF episodes visible
privately to P, none evidenced in someone else's private chat or an untraceable
conversation; a space conversation G may list G's group episodes, person_group
episodes of G and SELF episodes visible in G, each with at least one evidence row
and every row a direct chat event in G. Always: kind episode, active, clear,
not quarantined, not expired, not third-party, person owners enabled.

Usage:
    yuki memory episodes check > check.json
    python scripts/verify_episode_check.py --database data/yuki.db --check check.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class Verifier:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        self._now = datetime.now(UTC)

    def root(self, person_id: str | None) -> str | None:
        if person_id is None:
            return None
        row = self._db.execute(
            "SELECT coalesce(merged_into_person_id, id) FROM persons WHERE id = ?", (person_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def enabled(self, person_id: str) -> bool:
        row = self._db.execute(
            "SELECT o.enabled AND r.enabled FROM persons o "
            "JOIN persons r ON r.id = coalesce(o.merged_into_person_id, o.id) WHERE o.id = ?",
            (person_id,),
        ).fetchone()
        return bool(row and row[0])

    def evidence(self, fact_id: int) -> list[tuple[Any, ...]]:
        """(direct event_id, conversation kind, conversation person_id, conversation space_id).

        A tool-receipt row resolves through its trigger event; its event_id stays NULL.
        """
        return self._db.execute(
            "SELECT e.event_id, c.kind, c.person_id, c.space_id FROM memory_evidence e "
            "LEFT JOIN memory_tool_receipts r ON r.id = e.tool_receipt_id "
            "LEFT JOIN chat_events ev ON ev.id = coalesce(e.event_id, r.trigger_event_id) "
            "LEFT JOIN canonical_conversations c ON c.id = ev.canonical_conversation_id "
            "WHERE e.fact_id = ?",
            (fact_id,),
        ).fetchall()

    def violations(self, conversation: dict[str, Any], fact_id: int) -> list[str]:
        fact = self._db.execute(
            "SELECT scope_type, kind, status, conflict_state, review_state, valid_until, "
            "authority, canonical_subject_person_id, canonical_subject_space_id, "
            "visibility_type, canonical_visibility_person_id, canonical_visibility_space_id "
            "FROM memory_facts WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if fact is None:
            return ["missing_fact"]
        (
            scope,
            kind,
            status,
            conflict,
            review,
            valid_until,
            authority,
            subject_person,
            subject_space,
            visibility,
            visible_person,
            visible_space,
        ) = fact
        reasons: list[str] = []
        if kind != "episode":
            reasons.append("kind")
        if status != "active":
            reasons.append("not_active")
        if conflict != "clear":
            reasons.append("contested")
        if review == "quarantined":
            reasons.append("quarantined")
        until = _parse_time(valid_until)
        if until is not None and until <= self._now:
            reasons.append("expired")
        if authority == "third_party":
            reasons.append("authority")
        if scope in ("person", "person_group") and not self.enabled(subject_person):
            reasons.append("person_disabled")
        evidence = self.evidence(fact_id)
        if conversation["kind"] == "private":
            counterpart = self.root(conversation["person_id"])
            if scope in ("person", "person_group"):
                if self.root(subject_person) != counterpart:
                    reasons.append("other_person_episode")
            elif scope == "self":
                if not (visibility == "private" and self.root(visible_person) == counterpart):
                    reasons.append("self_visibility")
            else:
                reasons.append("group_episode_in_private")
            if any(
                kind_ is None or (kind_ == "private" and self.root(person) != counterpart)
                for _, kind_, person, _space in evidence
            ):
                reasons.append("other_private_evidence")
        else:
            space = conversation["space_id"]
            if scope in ("group", "person_group"):
                if subject_space != space:
                    reasons.append("other_space_episode")
            elif scope == "self":
                if not (visibility == "group" and visible_space == space):
                    reasons.append("self_visibility")
            else:
                reasons.append("person_episode_in_space")
            if not evidence or any(
                event_id is None or space_ != space for event_id, _kind, _person, space_ in evidence
            ):
                reasons.append("evidence_outside_space")
        return reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True, help="SQLite file path")
    parser.add_argument("--check", type=Path, required=True, help="episodes check JSON")
    args = parser.parse_args()
    report = json.loads(args.check.read_text(encoding="utf-8"))
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    try:
        verifier = Verifier(connection)
        found: list[dict[str, Any]] = []
        checked = 0
        for conversation in report["rendered"]:
            listed = [*conversation["index_fact_ids"], *conversation["recent_fact_ids"]]
            checked += len(listed)
            if len(set(listed)) != len(listed):
                found.append(
                    {
                        "conversation_id": conversation["conversation_id"],
                        "fact_id": None,
                        "reasons": ["listed_twice"],
                    }
                )
            for fact_id in listed:
                reasons = verifier.violations(conversation, int(fact_id))
                if reasons:
                    found.append(
                        {
                            "conversation_id": conversation["conversation_id"],
                            "fact_id": fact_id,
                            "reasons": reasons,
                        }
                    )
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "conversations": report["conversations"],
                "checked_entries": checked,
                "unstable": report["unstable"],
                "violations": len(found),
                "violation_fact_ids": sorted(
                    {item["fact_id"] for item in found if item["fact_id"] is not None}
                ),
                "details": found,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(bool(found or report["unstable"]))


if __name__ == "__main__":
    sys.exit(main())
