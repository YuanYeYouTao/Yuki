"""SELF "look around" (#541 P3): once a day Yuki looks at the things she cares about.

Off by default (``SELF_LOOKAROUND_ENABLED``). After the configured local hour (in
``memory_dream_timezone``; after the nightly Dream by default) it registers one
``proactive_turns`` row with ``source_kind='lookaround'`` per enabled root superuser
that has none since that day's slot. The shared proactive private-turn entry runs it
in the superuser's private chat as SELF: route, busy conversation, minimum interval,
one retry and settlement by Social receipts are the watchdog's rules, and every call
except ``send`` to that chat is refused (``report_only_source``). The turn's material is
the concerns visible in that chat (global ones and those private to that person), read
when the turn starts; with none, no model is called. One turn pays at most
``SELF_LOOKAROUND_MAX_MODEL_REQUESTS`` model requests and is not retried after that.

Nothing here gates other work; the slot is reporting pace, not a completion condition.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import person_root_id
from yuki.memory.resident import CONCERN_CATEGORY
from yuki.persistence.models import MemoryFactModel
from yuki.runtime.proactive_schema import proactive_turns
from yuki.services.proactive_turns import ProactivePrivateTurns, ProactiveSource

if TYPE_CHECKING:
    from yuki.admin.config_service import RuntimeConfigService
    from yuki.config import Settings
    from yuki.persistence.database import Database

logger = logging.getLogger(__name__)

SOURCE_KIND = "lookaround"
_POLL_SECONDS = 60.0

CONTENT = (
    "这是你每天一次的“看一看”：上面运行资料 lookaround 里是你在意的事。只用只读工具（联网搜索、"
    "读网页、read_chat、search_memory）看看它们有没有新进展；有值得对方知道或需要对方决定的，"
    "用 send 简短告诉对方一次，写出相关的 fact 编号；没有就什么都不发。这次只看和告诉，"
    "不执行任何写入或管理操作。"
)


def slot_start(now: float, *, hour: int, timezone: str) -> float:
    """The latest local ``hour:00`` at or before ``now`` (a day's slot)."""
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    start = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if start > local:
        start -= timedelta(days=1)
    return start.timestamp()


class SelfLookaround:
    def __init__(
        self,
        database: Database,
        turns: ProactivePrivateTurns,
        runtime_config: RuntimeConfigService,
        *,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.turns = turns
        self._config = runtime_config
        self._settings = settings
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._attempts: dict[str, asyncio.Task[str]] = {}
        self._last_error: str | None = None
        self.outcomes: list[str] = []
        turns.register(
            SOURCE_KIND,
            ProactiveSource(
                content=CONTENT,
                material=self._material,
                on_claim=self._on_claim,
                max_model_requests=settings.self_lookaround_max_model_requests,
            ),
        )

    async def start(self) -> None:
        if self._settings.self_lookaround_enabled:
            self._task = asyncio.create_task(self._loop(), name="self-lookaround")

    async def close(self) -> None:
        tasks = [task for task in (self._task, *self._attempts.values()) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def health(self) -> dict[str, Any]:
        return {
            "enabled": self._settings.self_lookaround_enabled,
            "running": self._task is not None and not self._task.done(),
            "attempts_running": len(self._attempts),
            "last_error_category": self._last_error,
        }

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
                self._last_error = None
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("self_lookaround_tick_failed category=%s", self._last_error)
            await asyncio.sleep(_POLL_SECONDS)

    async def tick(self) -> None:
        """Register today's turns once the slot has passed, then start due turns."""
        if not self._settings.self_lookaround_enabled:
            return
        now = self._clock()
        start = slot_start(
            now,
            hour=self._settings.self_lookaround_hour,
            timezone=self._settings.memory_dream_timezone,
        )
        async with self.database.read_session() as reader:
            superusers = set(
                await reader.scalars(
                    select(CanonicalPersonModel.id).where(
                        CanonicalPersonModel.is_superuser.is_(True),
                        CanonicalPersonModel.enabled.is_(True),
                        CanonicalPersonModel.merged_into_person_id.is_(None),
                    )
                )
            )
            registered = set(
                await reader.scalars(
                    select(proactive_turns.c.person_id).where(
                        proactive_turns.c.source_kind == SOURCE_KIND,
                        proactive_turns.c.created >= start,
                    )
                )
            )
        missing = sorted(superusers - registered)
        if missing:
            async with self.database.immediate_session() as writer:
                for person_id in missing:
                    await ProactivePrivateTurns.enqueue(writer, person_id, SOURCE_KIND, now)
        pace = (await self._config.snapshot()).watchdog
        await self.turns.recover(active=frozenset(self._attempts))
        for turn_id in await self.turns.due(
            now,
            min_interval=pace.notify_min_interval_seconds,
            retry_after=max(pace.check_interval_seconds, _POLL_SECONDS),
            source_kind=SOURCE_KIND,
        ):
            if turn_id not in self._attempts:
                task = asyncio.create_task(
                    self.turns.attempt(turn_id), name=f"lookaround-turn-{turn_id}"
                )
                self._attempts[turn_id] = task
                task.add_done_callback(functools.partial(self._attempt_done, turn_id))

    def _attempt_done(self, turn_id: str, task: asyncio.Task[str]) -> None:
        if self._attempts.get(turn_id) is task:
            del self._attempts[turn_id]
        if not task.cancelled() and task.exception() is None:
            self.outcomes.append(task.result())

    async def drain(self) -> list[str]:
        """Wait for the attempts started so far; their outcomes (tests and shutdown)."""
        await asyncio.gather(*self._attempts.values(), return_exceptions=True)
        outcomes, self.outcomes = self.outcomes, []
        return outcomes

    async def _material(self, turn_id: str) -> dict[str, Any] | None:
        """The concerns visible in the recipient's private chat, read at the start."""
        fact = MemoryFactModel
        async with self.database.read_session() as reader:
            person_id = await reader.scalar(
                select(proactive_turns.c.person_id).where(proactive_turns.c.id == turn_id)
            )
            root = await reader.scalar(select(person_root_id(person_id)))
            visible = person_root_id(fact.canonical_visibility_person_id)
            rows = (
                await reader.execute(
                    select(fact.id, fact.content, fact.visibility_type)
                    .where(
                        fact.scope_type == "self",
                        # A literal: the predicate of ix_memory_facts_self_concern.
                        text(f"memory_facts.category = '{CONCERN_CATEGORY}'"),
                        fact.status == "active",
                        fact.conflict_state == "clear",
                        fact.review_state != "quarantined",
                        (fact.visibility_type == "global")
                        | ((fact.visibility_type == "private") & (visible == root)),
                    )
                    .order_by(fact.id)
                )
            ).all()
        if not rows:
            return None
        return {
            "lookaround": {
                "concerns": [
                    {
                        "fact_id": row.id,
                        "content": " ".join(row.content.split()),
                        "visibility": row.visibility_type,
                    }
                    for row in rows
                ]
            }
        }

    @staticmethod
    async def _on_claim(
        writer: AsyncSession, turn_id: str, material: dict[str, Any] | None
    ) -> None:
        """The material is read live from memory; nothing is attached to the turn row."""
