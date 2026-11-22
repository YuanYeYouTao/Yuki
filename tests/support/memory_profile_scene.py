"""Shared real scene for the memory layering P4 (person profiles) E2E suite.

Task book: docs/architecture/Yuki-记忆分层P4人物画像任务书-2026-11-22.md §13. It
extends the P1 resident scene to the P4 fixture: Person A (the owner), B, C, D and
B2 (merged into B by the cases that need it); groups G and H; A, B and C are members
of G, B and D of H. Facts and evidence go through the real write paths with evidence
on real fixture chat events; the few direct rows (tool receipts, foreign evidence)
are the ones production holds from legacy or imported data.

All fixture text is invented. Profile imports stay inside functions so the scene
builds before the implementation lands.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from tests.support.account_observations import apply_observations, profile_message
from tests.support.canonical_identity import ensure_person
from tests.support.memory_resident_scene import (
    GROUPS,
    QQ,
    ResidentScene,
    normalize,
)

from yuki.domain.conversations import ConversationScope
from yuki.memory.enums import MemoryAuthority, MemoryEvidenceRelation
from yuki.persistence.models import MemoryEvidenceModel, MemoryToolReceiptModel

RESULTS = Path(__file__).resolve().parents[2] / "test-results" / "memory-profiles"
BOT = "80001"


class ProfileScene(ResidentScene):
    """The resident scene plus D, C in G and D in H."""

    async def build(self, *, work_enabled: bool = False, **settings: Any) -> ProfileScene:
        await super().build(work_enabled=work_enabled, **settings)
        async with self.database.immediate_session() as writer:
            self.persons["D"] = await ensure_person(
                writer, QQ["D"], display_name="person-D", now=datetime.now(UTC)
            )
        for person, group in (("C", "G"), ("B", "H"), ("D", "H")):
            await apply_observations(
                self.database,
                profile_message(
                    user_id=QQ[person], nickname=f"person-{person}", group_id=GROUPS[group]
                ),
            )
        await self.event(
            "private:D", "D", "opening private:D", scope=ConversationScope.private(BOT, QQ["D"])
        )
        return self

    @property
    def profiles(self):
        from yuki.memory.profiles import ProfileStore

        return ProfileStore(self.database)

    def entry(self, text_: str, *fact_ids: int, section: str | None = None):
        from yuki.memory.profiles import ProfileEntry

        return ProfileEntry(section=section, text=text_, fact_ids=tuple(fact_ids))

    async def receipt_evidence(
        self,
        fact_id: int,
        trigger_event_id: int,
        *,
        person: str | None = None,
        space: str | None = None,
        source_event_ids: tuple[int, ...] = (),
    ) -> int:
        """A tool-receipt evidence row (direct write, as SELF reflection stores them)."""
        now = datetime.now(UTC)
        async with self.database.immediate_session() as writer:
            receipt = MemoryToolReceiptModel(
                conversation_key_hash="p" * 64,
                trigger_event_id=trigger_event_id,
                bot_user_id=BOT,
                canonical_person_id=self.persons[person] if person else None,
                canonical_space_id=self.spaces[space] if space else None,
                source_event_ids_json=json.dumps(list(source_event_ids)),
                provider_id="profile-e2e",
                tool_name="read_chat_history",
                success=True,
                result_excerpt="一段虚构的聊天记录摘要",
                result_characters=11,
                created_at=now,
                expires_at=now + timedelta(days=30),
            )
            writer.add(receipt)
            await writer.flush()
            evidence = MemoryEvidenceModel(
                fact_id=fact_id,
                tool_receipt_id=receipt.id,
                source_speaker_user_id=BOT,
                relation=MemoryEvidenceRelation.AGENT_REFLECTION.value,
                authority=MemoryAuthority.AGENT_REFLECTION.value,
                confidence=0.9,
                excerpt="一段虚构的聊天记录摘要",
                created_at=now,
            )
            writer.add(evidence)
            await writer.flush()
            return evidence.id

    async def profile_tables(self) -> dict[str, list[dict[str, Any]]]:
        """The three P4 tables, read with plain SQL (independent of the ORM models)."""
        tables = {
            "memory_profiles": (
                "SELECT p.id, p.person_id, p.audience, p.current_version,"
                " p.considered_signature, p.locked FROM memory_profiles p"
                " ORDER BY p.person_id, p.audience"
            ),
            "memory_profile_versions": (
                "SELECT p.person_id, p.audience, v.version, v.entries_json, v.source,"
                " v.reason, v.dream_run_public_id FROM memory_profile_versions v"
                " JOIN memory_profiles p ON p.id = v.profile_id"
                " ORDER BY p.person_id, p.audience, v.version"
            ),
            "memory_space_members": (
                "SELECT space_id, position, person_id, source, dream_run_public_id"
                " FROM memory_space_members ORDER BY space_id, position"
            ),
        }
        result: dict[str, list[dict[str, Any]]] = {}
        async with self.database.read_session() as reader:
            for name, sql in tables.items():
                rows = (await reader.execute(text(sql))).mappings().all()
                result[name] = [
                    {
                        key: json.loads(value) if key == "entries_json" else value
                        for key, value in row.items()
                        if key != "id"
                    }
                    for row in rows
                ]
        # Person and space IDs are random: order by fixture label for stable artifacts.
        labels = self.labels()

        def order(row: dict[str, Any]) -> tuple[Any, ...]:
            owner = row.get("person_id") if "audience" in row else row["space_id"]
            return (
                labels.get(owner, owner),
                row.get("audience", ""),
                row.get("version", row.get("position", 0)),
            )

        return {name: sorted(rows, key=order) for name, rows in result.items()}

    async def evidence_count(self, fact_id: int) -> int:
        async with self.database.read_session() as reader:
            return len(
                (
                    await reader.scalars(
                        select(MemoryEvidenceModel.id).where(MemoryEvidenceModel.fact_id == fact_id)
                    )
                ).all()
            )

    def write(self, case: str, payload: dict[str, Any]) -> Path:
        return write_artifact(case, payload, self.labels())


def write_artifact(case: str, payload: dict[str, Any], labels: dict[str, str]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{case}.json"
    path.write_text(normalize(payload, labels) + "\n", encoding="utf-8")
    return path
