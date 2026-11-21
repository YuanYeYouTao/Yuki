"""Acceptance check for resident memory visibility (layered memory P1 task book §11.2).

Independent of the renderer on purpose: it re-checks every fact ID reported by
``yuki memory resident check`` against the task book §5.3 conditions with its own
read-only SQL (plain sqlite3, ``mode=ro``), and prints the violation count and the
offending fact IDs. It never writes and is not product code.

P1 scope follows the owner's decisions (Q16 b): a private conversation may show
the counterpart's person facts and SELF principles visible there; a space
conversation may show that space's group facts and SELF principles, all with
every evidence row from an event in that space.

Usage:
    yuki memory resident check > check.json
    python scripts/verify_resident_check.py --database data/yuki.db --check check.json
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
        """(event_id, conversation kind, conversation person_id, conversation space_id)."""
        return self._db.execute(
            "SELECT e.event_id, c.kind, c.person_id, c.space_id FROM memory_evidence e "
            "LEFT JOIN chat_events ev ON ev.id = e.event_id "
            "LEFT JOIN canonical_conversations c ON c.id = ev.canonical_conversation_id "
            "WHERE e.fact_id = ?",
            (fact_id,),
        ).fetchall()

    def violations(self, conversation: dict[str, Any], fact_id: int) -> list[str]:
        fact = self._db.execute(
            "SELECT scope_type, kind, status, conflict_state, review_state, valid_until, "
            "authority, source_type, canonical_subject_person_id, canonical_subject_space_id, "
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
            _source_type,
            subject_person,
            subject_space,
            visibility,
            visible_person,
            visible_space,
        ) = fact
        reasons: list[str] = []
        if status != "active":
            reasons.append("not_active")
        if conflict != "clear":
            reasons.append("contested")
        if review == "quarantined":
            reasons.append("quarantined")
        until = _parse_time(valid_until)
        if until is not None and until <= self._now:
            reasons.append("expired")
        if kind not in ("fact", "preference"):
            reasons.append("kind")
        evidence = self.evidence(fact_id)
        if conversation["kind"] == "private":
            counterpart = self.root(conversation["person_id"])
            if scope in ("person", "person_group"):
                if self.root(subject_person) != counterpart:
                    reasons.append("other_person_fact")
                elif not self.enabled(subject_person):
                    reasons.append("person_disabled")
                if authority not in ("explicit", "self_report"):
                    reasons.append("authority")
                if any(
                    kind_ == "private" and self.root(person) != counterpart
                    for _, kind_, person, _space in evidence
                ):
                    reasons.append("other_private_evidence")
            elif scope == "self":
                if not (
                    visibility == "global"
                    or (visibility == "private" and self.root(visible_person) == counterpart)
                ):
                    reasons.append("self_visibility")
            else:
                reasons.append("group_fact_in_private")
        else:
            space = conversation["space_id"]
            if scope == "group":
                if subject_space != space:
                    reasons.append("other_space_fact")
            elif scope == "self":
                visible = visibility == "global" or (
                    visibility == "group" and visible_space == space
                )
                if not visible:
                    reasons.append("self_visibility")
            else:
                reasons.append("person_fact_in_space")
            if not evidence or any(
                event_id is None or space_ != space for event_id, _kind, _person, space_ in evidence
            ):
                reasons.append("evidence_outside_space")
        return reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True, help="SQLite file path")
    parser.add_argument("--check", type=Path, required=True, help="resident check JSON")
    args = parser.parse_args()
    report = json.loads(args.check.read_text(encoding="utf-8"))
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    try:
        verifier = Verifier(connection)
        found: list[dict[str, Any]] = []
        for conversation in report["rendered"]:
            for fact_id in conversation["fact_ids"]:
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
                "checked_entries": sum(len(item["fact_ids"]) for item in report["rendered"]),
                "unstable": report["unstable"],
                "violations": len(found),
                "violation_fact_ids": sorted({item["fact_id"] for item in found}),
                "details": found,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(bool(found or report["unstable"]))


if __name__ == "__main__":
    sys.exit(main())
