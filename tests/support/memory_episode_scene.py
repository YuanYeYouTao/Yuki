"""Shared real scene for the memory layering P2 (episode index) E2E suite.

Task book: docs/architecture/Yuki-记忆分层P2经历目录任务书-2026-11-22.md §10. It
extends the P1 resident scene (Person A the owner, B, C, B2; groups G and H; A and B
in G, C only in H) with:

- episodes written through real write paths: ``MemoryFactService.remember`` for
  fixture rows with fixed evidence, the real ``SelfReflectionService`` over real
  claimed batches of chat events, the real batch extraction worker, Dream and Main
  ``save_memory``;
- the watermark: a real Dream run created, started and drained through the worker
  loop body until ``finalize_run`` sets ``completed_at``;
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
INDEX_HEADER = "经历目录（"
RECENT_HEADER = "今天新记下的经历（还没进入目录）"
UNTITLED = "（未命名经历）"
TIMEZONE = "Asia/Shanghai"
_FACT_MARK = re.compile(r"（fact:(\d+)）")
_INDEX_LINE = re.compile(r"^- (\d{4}-\d{2}：共 \d+ 条|\d{2}-\d{2} .*（fact:\d+）)$")
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
        # The first scan only sets the self-reflection baseline (events after it count).
        from yuki.memory.self_reflection.repository import SelfReflectionRepository

        await SelfReflectionRepository(self.database).scan_new_events()
        return self

    def _respond(self, request):
        if self.reflection_responder is not None and _is_reflection(request):
            return self.reflection_responder(request)
        return super()._respond(request)

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

    async def render(self, key: str, *, through_fact_id: int | None = None):
        return await self.episodes.render_conversation(
            self.conversations[key], through_fact_id=through_fact_id
        )

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
        "recent_fact_ids": list(rendered.recent_fact_ids),
        "through_fact_id": rendered.through_fact_id,
    }


def normalize_days(text: str) -> str:
    """Dates that equal today (local) become ``<today>`` so reruns stay byte-identical."""
    today = local_day()
    month = datetime.now(UTC).astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m")
    text = text.replace(f"- {today} ", "- <today> ").replace(f"截至 {today}", "截至 <today>")
    return text.replace(f"- {month}：", "- <this-month>：")


def write_episode_artifact(case: str, payload: dict[str, Any], labels: dict[str, str]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{case}.json"
    path.write_text(normalize_days(normalize(payload, labels)) + "\n", encoding="utf-8")
    return path


def _is_reflection(request) -> bool:
    try:
        payload = json.loads(request.messages[-1].content or "")
    except ValueError:
        return False
    # SelfReflectionInput (defaults dropped); extraction inputs always carry bot_name.
    return isinstance(payload, dict) and "events" in payload and "bot_name" not in payload


__all__ = [
    "INDEX_HEADER",
    "RECENT_HEADER",
    "UNTITLED",
    "EpisodeScene",
    "EpisodeTurn",
    "capture_episodes",
    "fact_marks",
    "index_segment",
    "item_lines",
    "local_day",
    "month_counts",
    "recent_segment",
    "render_record",
    "request_text",
    "turn_record",
    "write_episode_artifact",
]
