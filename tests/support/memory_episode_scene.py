"""Shared real scene for the memory layering P2 / P2.1 (episode index) E2E suites.

Task books: docs/architecture/Yuki-记忆分层P2经历目录任务书-2026-11-22.md §10 and
docs/architecture/Yuki-记忆分层P2.1经历目录精简任务书-2026-11-23.md §13. It
extends the P1 resident scene (Person A the owner, B, C, B2; groups G and H; A and B
in G, C only in H) with:

- episodes written through real write paths: ``MemoryFactService.remember`` for
  fixture rows with fixed evidence, the real ``SelfReflectionService`` over real
  claimed batches of chat events, the real batch extraction worker, Dream and Main
  ``save_memory``;
- the nightly index: a real Dream run created, started and drained through the
  worker loop body; its selection step writes ``memory_episode_indexes`` (P2.1);
- the P2.1 comparison call (``c_N`` candidates) routed to ``memorable_responder``,
  every such request kept in ``memorable_requests``;
- a capture of the episode index (the last part of the system message) and of the
  "today's new episodes" block (inside the current message), parsed independently
  of the renderer.

Episode index imports stay inside functions so the scene builds before the
implementation lands.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from tests.support.memory_resident_scene import (
    ResidentScene,
    normalize,
    request_text,
    resident_segment,
    system_text,
)

from yuki.memory.enums import MemoryKind
from yuki.persistence.models import MemoryFactModel

RESULTS = Path(__file__).resolve().parents[2] / "test-results" / "memory-episode-index"
MEMORABLE_RESULTS = (
    Path(__file__).resolve().parents[2] / "test-results" / "memory-episode-memorable"
)
INDEX_HEADER = "经历目录（"
RECENT_HEADER = "今天新记下的经历（还没进入目录）"
UNTITLED = "（未命名经历）"
TIMEZONE = "Asia/Shanghai"
_FACT_MARK = re.compile(r"（fact:(\d+)）")
# Section titles, month counts and item lines (``MM-DD`` in the as-of year, else
# ``YYYY-MM-DD``).
_INDEX_LINE = re.compile(
    r"^(\[[^\]]+\]|- (\d{4}-\d{2}：共 \d+ 条|(\d{4}-)?\d{2}-\d{2} .*（fact:\d+）))$"
)
MEMORABLE_SECTION = "[长期印象]"
RECENT_SECTION = "[最近的经历]"
REST_SECTION = "[其余]"
_RECENT_LINE = re.compile(
    r"^(- \d{2}-\d{2} .*（fact:\d+）|（另有 \d+ 条，可用 search_memory 查）)$"
)


def _block(content: str, header: str, pattern: re.Pattern[str]) -> str:
    lines = content.split("\n")
    starts = [index for index, line in enumerate(lines) if line.startswith(header)]
    if not starts:
        return ""
    assert len(starts) == 1, f"{header} must appear exactly once"
    kept = [lines[starts[0]]]
    for line in lines[starts[0] + 1 :]:
        if not pattern.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def index_segment(content: str) -> str:
    """The episode index block: header plus month-count and item lines."""
    return _block(content, INDEX_HEADER, _INDEX_LINE)


def recent_segment(content: str) -> str:
    """The "today's new episodes" block: header, optional overflow line, item lines."""
    return _block(content, RECENT_HEADER, _RECENT_LINE)


def fact_marks(segment: str) -> list[int]:
    return [int(value) for value in _FACT_MARK.findall(segment)]


def month_counts(segment: str) -> dict[str, int]:
    found = re.findall(r"^- (\d{4}-\d{2})：共 (\d+) 条$", segment, flags=re.MULTILINE)
    return {month: int(count) for month, count in found}


def item_lines(segment: str) -> list[str]:
    return [line for line in segment.split("\n") if _FACT_MARK.search(line)]


def index_sections(segment: str) -> dict[str, list[str]]:
    """Section title -> its lines, parsed independently of the renderer."""
    result: dict[str, list[str]] = {}
    current = ""
    for line in segment.split("\n")[1:]:
        if line.startswith("["):
            current = line
            result[current] = []
        elif current:
            result[current].append(line)
    return result


def section_ids(segment: str, title: str) -> list[int]:
    return [mark for line in index_sections(segment).get(title, []) for mark in fact_marks(line)]


def memorable_reply(refs: list[str], reason: str = "这几件事值得长期记得") -> str:
    """Alias-only structured output of the comparison call (never database IDs)."""
    return json.dumps({"selected": list(refs), "reason": reason}, ensure_ascii=False)


def pool_of(request) -> list[dict[str, Any]]:
    """The candidate pool a comparison request offered (``c_N`` in order)."""
    payload = json.loads(request.messages[-1].content)
    return list(payload["candidates"])


@dataclass
class EpisodeTurn:
    """One real Main turn as the model saw it."""

    requests: list[Any]
    current: str
    system: str
    index: str
    recent: str
    index_ids: list[int]
    recent_ids: list[int]
    resident: str
    result: Any = None


def capture_episodes(requests: list[Any], result: Any = None) -> EpisodeTurn:
    first = requests[0]
    current = str(first.messages[-1].content or "")
    system = system_text(first)
    # The index lives at the end of the system message; the recent block only in
    # the current message.
    assert INDEX_HEADER not in current, "the episode index must not be in the current message"
    assert RECENT_HEADER not in system, "today's episodes must not be in the system message"
    for message in first.messages[1:-1]:
        text = str(message.content or "")
        assert INDEX_HEADER not in text and RECENT_HEADER not in text, "history holds no segment"
    index = index_segment(system)
    recent = recent_segment(current)
    return EpisodeTurn(
        requests=requests,
        current=current,
        system=system,
        index=index,
        recent=recent,
        index_ids=fact_marks(index),
        recent_ids=fact_marks(recent),
        resident=resident_segment(system),
        result=result,
    )


def local_day(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(ZoneInfo(TIMEZONE)).strftime("%m-%d")


class EpisodeScene(ResidentScene):
    """P1 resident scene plus the episode index wiring and helpers."""

    async def build(  # type: ignore[override]
        self,
        *,
        work_enabled: bool = False,
        **settings: Any,
    ) -> EpisodeScene:
        settings.setdefault("memory_self_reflection_enabled", True)
        settings.setdefault("self_memory_enabled", True)
        await super().build(work_enabled=work_enabled, **settings)
        install_memory_segments(self.chat, self.database)
        # The container wires Main's memory writes (save_memory / edit_memory).
        self.chat._tools._memory_mutations = self.harness_mutations()
        self.reflection_responder = None
        self.memorable_responder: Any = None
        self.memorable_requests: list[Any] = []
        # The first scan only sets the self-reflection baseline (events after it count).
        from yuki.memory.self_reflection.repository import SelfReflectionRepository

        await SelfReflectionRepository(self.database).scan_new_events()
        return self

    def _respond(self, request):
        if self.reflection_responder is not None and _is_reflection(request):
            return self.reflection_responder(request)
        if _is_comparison(request):
            self.memorable_requests.append(request)
            if self.memorable_responder is not None:
                return self.memorable_responder(request)
        return super()._respond(request)

    # ------------------------------------------------------------- comparison
    def pool(self, request) -> list[dict[str, Any]]:
        """The offered candidates, each with the ``fact_id`` whose stored text it carries.

        Read with plain sqlite3 from the committed database so Dream outputs written
        in the same run map too; texts are compared whitespace-folded.
        """
        import sqlite3

        path = self.database.url.removeprefix("sqlite+aiosqlite:///")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT id, content FROM memory_facts WHERE kind = 'episode'"
            ).fetchall()
        finally:
            connection.close()
        by_text: dict[str, int] = {}
        for fact_id, content in rows:
            by_text.setdefault(" ".join(str(content).split()), int(fact_id))
        return [
            {**item, "fact_id": by_text.get(" ".join(str(item["content"]).split()))}
            for item in pool_of(request)
        ]

    def _ids_by_text(self) -> dict[str, int]:
        import sqlite3

        path = self.database.url.removeprefix("sqlite+aiosqlite:///")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT id, content FROM memory_facts").fetchall()
        finally:
            connection.close()
        by_text: dict[str, int] = {}
        for fact_id, content in rows:
            by_text.setdefault(" ".join(str(content).split()), int(fact_id))
        return by_text

    def dream_refs(self, request) -> dict[int, str]:
        """``fact_id -> memory_N`` of a Dream request, matched by stored text."""
        by_text = self._ids_by_text()
        found: dict[int, str] = {}

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                ref = value.get("ref")
                if isinstance(ref, str) and ref.startswith("memory_") and "content" in value:
                    fact_id = by_text.get(" ".join(str(value["content"]).split()))
                    if fact_id is not None:
                        found[fact_id] = ref
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(json.loads(request.messages[-1].content))
        return found

    def choose(self, wanted):
        """A comparison responder: ``wanted(pool fact IDs)`` returns the IDs to keep."""

        def respond(request):
            pool = self.pool(request)
            chosen = set(wanted([item["fact_id"] for item in pool]))
            return memorable_reply([item["ref"] for item in pool if item["fact_id"] in chosen])

        return respond

    def pooled_ids(self, start: int = 0) -> list[list[int | None]]:
        """Fact IDs of every comparison pool since request ``start``."""
        return [
            [item["fact_id"] for item in self.pool(request)]
            for request in self.memorable_requests[start:]
        ]

    # ------------------------------------------------------------ index rows
    def index_owner(self, key: str) -> tuple[str, str]:
        kind, who = key.split(":")
        if kind == "private":
            return "private", self.persons[who]
        return "space", self.spaces[who]

    async def index_row(self, key: str) -> dict[str, Any] | None:
        """The ``memory_episode_indexes`` row of ``private:X`` / ``space:X`` (raw SQL)."""
        from sqlalchemy import text

        kind, owner = self.index_owner(key)
        column = "owner_person_id" if kind == "private" else "owner_space_id"
        async with self.database.read_session() as reader:
            row = (
                (
                    await reader.execute(
                        text(
                            "SELECT recent_fact_ids_json, memorable_json, month_counts_json,"
                            " through_fact_id, locked, revision, as_of"
                            f" FROM memory_episode_indexes WHERE owner_kind = :kind"
                            f" AND {column} = :owner"
                        ),
                        {"kind": kind, "owner": owner},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {
            "recent": json.loads(row["recent_fact_ids_json"]),
            "memorable": json.loads(row["memorable_json"]),
            "months": json.loads(row["month_counts_json"]),
            "through": int(row["through_fact_id"]),
            "locked": bool(row["locked"]),
            "revision": int(row["revision"]),
            "as_of": row["as_of"],
        }

    async def memorable_ids(self, key: str) -> list[int]:
        row = await self.index_row(key)
        return [] if row is None else [item["fact_id"] for item in row["memorable"]]

    async def age_memorable(self, key: str, *, days: int) -> None:
        """Fixture-only: pretend every current entry entered ``days`` days ago."""
        from datetime import timedelta

        from sqlalchemy import text

        kind, owner = self.index_owner(key)
        column = "owner_person_id" if kind == "private" else "owner_space_id"
        row = await self.index_row(key)
        assert row is not None
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        aged = [{**item, "since": since} for item in row["memorable"]]
        async with self.database.immediate_session() as writer:
            await writer.execute(
                text(
                    "UPDATE memory_episode_indexes SET memorable_json = :value"
                    f" WHERE owner_kind = :kind AND {column} = :owner"
                ),
                {"value": json.dumps(aged), "kind": kind, "owner": owner},
            )

    async def max_fact_id(self) -> int:
        return max(await self.fact_ids_after(0), default=0)

    # --------------------------------------------------------------- episodes
    async def episode(
        self, name: str, owner: str, content: str, *, title: str | None = None, **fields: Any
    ) -> int:
        """An episode through ``MemoryFactService.remember`` (see ``ResidentScene.fact``)."""
        return await self.fact(name, owner, content, kind=MemoryKind.EPISODE, title=title, **fields)

    async def titles(self, fact_ids: list[int]) -> dict[int, str | None]:
        async with self.database.read_session() as reader:
            rows = await reader.execute(
                select(MemoryFactModel.id, MemoryFactModel.title).where(
                    MemoryFactModel.id.in_(fact_ids)
                )
            )
            return {row.id: row.title for row in rows}

    async def fact_row(self, fact_id: int) -> dict[str, Any] | None:
        async with self.database.read_session() as reader:
            row = await reader.get(MemoryFactModel, fact_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "kind": row.kind,
                "title": row.title,
                "status": row.status,
                "updated_at": row.updated_at,
                "content": row.content,
            }

    async def reflect(self, plan: dict[str, list[dict[str, Any]]]) -> dict[str, list[int]]:
        """Run the real self-reflection over every due batch; returns created fact IDs.

        ``plan`` maps ``private:X`` / ``space:X`` to the episodes its batch outputs; each
        is ``{"title": ..., "content": ..., "cites": [substring of an event body, ...]}``
        (omit ``title`` to model an output without one). Batches of other
        conversations get an empty reflection.
        """
        from tests.support.model_executor import InjectedModelExecutor

        from yuki.memory.self_reflection.repository import SelfReflectionRepository
        from yuki.memory.self_reflection.service import SelfReflectionService

        by_conversation = {self.conversations[key]: key for key in plan}
        repository = SelfReflectionRepository(self.database)
        service = SelfReflectionService(
            settings=self.settings,
            repository=repository,
            mutations=self.harness_mutations(),
            models=InjectedModelExecutor(self.provider),
        )
        current: dict[str, str | None] = {"key": None}
        created: dict[str, list[int]] = {key: [] for key in plan}

        def respond(request):
            payload = json.loads(request.messages[-1].content)
            key = current["key"]
            if key is None:
                return json.dumps({"proposals": [], "episodes": []})
            refs = {item["ref"]: item["rendered"] for item in payload["events"]}
            output = []
            for item in plan[key]:
                cited = [
                    ref
                    for ref, rendered in refs.items()
                    if any(quote in rendered for quote in item["cites"])
                ]
                assert cited, (item, refs)
                episode: dict[str, Any] = {
                    "passages": [{"evidence_refs": cited, "content": item["content"]}],
                    "importance": 4,
                }
                if "title" in item:
                    episode["title"] = item["title"]
                output.append(episode)
            return json.dumps({"proposals": [], "episodes": output}, ensure_ascii=False)

        self.reflection_responder = respond
        try:
            await repository.scan_new_events()
            for attempt in range(40):
                batches = await repository.claim_due(
                    scheduled_slot=f"episode-e2e:{self._next()}:{attempt}",
                    event_threshold=1,
                    character_threshold=1,
                    max_wait_seconds=0,
                    max_events=200,
                    max_characters=200_000,
                    force=True,
                    self_name=self.settings.character_identity.display_name,
                )
                if not batches:
                    break
                batch = batches[0]
                conversation = batch.events[0].canonical_conversation_id if batch.events else None
                current["key"] = by_conversation.get(conversation or "")
                before = set(await self.fact_ids_after(0))
                proposals, committed = await service.reflect(batch)
                await repository.complete(batch, proposals=proposals, committed=committed)
                if current["key"] is not None:
                    created[current["key"]].extend(
                        fact_id for fact_id in await self.fact_ids_after(0) if fact_id not in before
                    )
        finally:
            self.reflection_responder = None
        for key, episodes in plan.items():
            assert len(created[key]) == len(episodes), (key, created[key], episodes)
        return created

    # -------------------------------------------------------------- watermark
    async def advance(self) -> int:
        """One real Dream run drained to completion; returns its snapshot (the watermark)."""
        run = await self.dream_run()
        assert run.status.value == "completed", run.status
        assert run.completed_at is not None
        return run.snapshot_max_fact_id

    async def stuck_run(self) -> Any:
        """A Dream run planned and started but never drained (stays ``running``)."""
        from yuki.memory.dream.models import DreamPlanStatistics, DreamRunMode

        dream = self.dream()
        run = await dream.repository.create_run(
            mode=DreamRunMode.FULL,
            statistics=DreamPlanStatistics(
                eligible_facts=0,
                ready_facts=0,
                missing_embeddings=0,
                ambiguous_bot_facts=0,
                partitions=0,
                candidate_clusters=0,
                isolated_facts=0,
                estimated_model_calls=0,
            ),
            clusters=(),
            snapshot_max_fact_id=max(await self.fact_ids_after(0), default=0),
            actor_user_id="10001",
            scheduled_slot=None,
        )
        assert await dream.repository.start_run(run.public_id)
        return await dream.repository.get_run(run.public_id)

    # ------------------------------------------------------------------ index
    @property
    def episodes(self):
        from yuki.memory.episode_index import EpisodeIndex

        return EpisodeIndex(self.database, timezone=TIMEZONE)

    async def render(self, key: str):
        return await self.episodes.render_conversation(self.conversations[key])

    # ------------------------------------------------------------------ turns
    async def episode_turn(
        self, who: str, text: str, *, group: str | None = None, person: str | None = None
    ) -> EpisodeTurn:
        turn = await self.turn(who, text, group=group, person=person)
        return capture_episodes(turn.requests, turn.result)

    async def episode_tool_turn(
        self,
        who: str,
        text: str,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        group: str | None = None,
    ) -> tuple[list[Any], EpisodeTurn]:
        replies, turn = await self.tool_turn(who, text, calls, group=group)
        return replies, capture_episodes(turn.requests, turn.result)

    def write(self, case: str, payload: dict[str, Any]) -> Path:  # type: ignore[override]
        return write_episode_artifact(case, payload, self.labels())

    def write_memorable(self, case: str, payload: dict[str, Any]) -> Path:
        return write_episode_artifact(case, payload, self.labels(), results=MEMORABLE_RESULTS)

    async def reordered_copy(self, path: Path, *, reverse: bool) -> str:
        """A copy whose fact, evidence and index rows were re-inserted in another order."""
        import asyncio
        import shutil
        import sqlite3

        async with self.database.engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        source = Path(self.database.url.removeprefix("sqlite+aiosqlite:///"))

        def rebuild() -> None:
            shutil.copyfile(source, path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                for table in (
                    "memory_evidence",
                    "memory_resident_sets",
                    "memory_episode_indexes",
                    "memory_facts",
                ):
                    cursor = connection.execute(f"SELECT * FROM {table} ORDER BY id")
                    columns = [item[0] for item in cursor.description]
                    rows = cursor.fetchall()
                    connection.execute(f"DELETE FROM {table}")
                    placeholders = ",".join("?" for _ in columns)
                    for row in reversed(rows) if reverse else rows:
                        connection.execute(
                            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                            row,
                        )
                connection.commit()
            finally:
                connection.close()

        await asyncio.to_thread(rebuild)
        return f"sqlite+aiosqlite:///{path.as_posix()}"


def install_memory_segments(chat, database) -> None:
    """Give the real ChatService a ContextAssembler owning both memory renderers.

    The container wires ``ResidentMemory`` and ``EpisodeIndex`` into
    ``ContextAssembler``; the shared harness predates them.
    """
    from yuki.memory.episode_index import EpisodeIndex
    from yuki.memory.resident import ResidentMemory
    from yuki.services.context_assembler import ContextAssembler

    chat._context_assembler = ContextAssembler(
        settings=chat._settings,
        ledger=chat._ledger,
        conversation_scopes=chat._conversation_scopes,
        resident=ResidentMemory(database),
        episodes=EpisodeIndex(database, timezone=chat._settings.memory_dream_timezone),
    )


def turn_record(turn: EpisodeTurn, contract: str, watermark: int | None = None) -> dict[str, Any]:
    return {
        "index": turn.index,
        "recent": turn.recent,
        "index_fact_ids": turn.index_ids,
        "recent_fact_ids": turn.recent_ids,
        "through_fact_id": watermark,
        "recovery_contract": contract,
    }


def render_record(rendered: Any) -> dict[str, Any]:
    return {
        "index": rendered.index_text,
        "recent": rendered.recent_text,
        "index_fact_ids": list(rendered.index_fact_ids),
        "memorable_fact_ids": list(rendered.memorable_fact_ids),
        "window_fact_ids": list(rendered.window_fact_ids),
        "recent_fact_ids": list(rendered.recent_fact_ids),
        "through_fact_id": rendered.through_fact_id,
    }


def normalize_days(text: str) -> str:
    """Dates that equal today (local) become ``<today>`` so reruns stay byte-identical."""
    today = local_day()
    now = datetime.now(UTC).astimezone(ZoneInfo(TIMEZONE))
    month = now.strftime("%Y-%m")
    text = text.replace(f"- {today} ", "- <today> ").replace(f"截至 {today}", "截至 <today>")
    text = text.replace(f'"{now:%Y-%m-%d}"', '"<today-date>"')
    text = text.replace(f'"{month}"', '"<this-month>"')
    return text.replace(f"- {month}：", "- <this-month>：")


def write_episode_artifact(
    case: str, payload: dict[str, Any], labels: dict[str, str], *, results: Path = RESULTS
) -> Path:
    results.mkdir(parents=True, exist_ok=True)
    path = results / f"{case}.json"
    path.write_text(normalize_days(normalize(payload, labels)) + "\n", encoding="utf-8")
    return path


def _is_comparison(request) -> bool:
    """The P2.1 comparison call: its input is ``{"candidates": [{"ref": "c_N", ...}]}``."""
    try:
        payload = json.loads(request.messages[-1].content or "")
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("candidates"), list)
        and all(re.fullmatch(r"c_\d+", str(item.get("ref"))) for item in payload["candidates"])
    )


def _is_reflection(request) -> bool:
    try:
        payload = json.loads(request.messages[-1].content or "")
    except ValueError:
        return False
    # SelfReflectionInput (defaults dropped); extraction inputs always carry bot_name.
    return isinstance(payload, dict) and "events" in payload and "bot_name" not in payload


__all__ = [
    "INDEX_HEADER",
    "MEMORABLE_RESULTS",
    "MEMORABLE_SECTION",
    "RECENT_HEADER",
    "RECENT_SECTION",
    "REST_SECTION",
    "UNTITLED",
    "EpisodeScene",
    "EpisodeTurn",
    "capture_episodes",
    "fact_marks",
    "index_sections",
    "index_segment",
    "item_lines",
    "local_day",
    "memorable_reply",
    "month_counts",
    "pool_of",
    "recent_segment",
    "render_record",
    "request_text",
    "section_ids",
    "turn_record",
    "write_episode_artifact",
]
