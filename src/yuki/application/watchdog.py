"""Read-only watchdog: find stuck facts, register each occurrence once, let Yuki tell.

Each check reads existing facts (database rows through existing indexes, or the
in-memory health the loops already keep) and costs no model call. A problem is
*present*, *healthy* or *undetermined*; only an observed healthy state counts as
cleared, so a restart or a still-debouncing health never re-opens or drops a row.

``watchdog_issues`` holds one row per superuser and open occurrence. A key that is
clear for less than ``no_progress_seconds`` and comes back is the same occurrence
(the absence clock lives in memory: losing it on restart only delays a deletion).
New rows are batched per person into one ``proactive_turns`` row; the turn entry
runs it under the person's minimum notification interval and one retry.

Nothing here gates real work: every interval is reporting pace.
"""

from __future__ import annotations

import asyncio
import collections
import functools
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.repository import failing_automations
from yuki.external_agents.delegation import watch_records
from yuki.identity.db_models import CanonicalPersonModel
from yuki.runtime.proactive_schema import proactive_turns, watchdog_issues
from yuki.runtime.protocol_store import GC_GRACE_SECONDS, stalled_unowned_object, usage_bytes
from yuki.runtime.work_repository import suspended_works
from yuki.services.proactive_turns import (
    MAX_ATTEMPTS,
    ProactivePrivateTurns,
    ProactiveSource,
)
from yuki.time.formatting import local_iso

if TYPE_CHECKING:
    from yuki.admin.config_service import RuntimeConfigService
    from yuki.admin.models import RuntimeConfigSnapshot
    from yuki.persistence.database import Database

logger = logging.getLogger(__name__)

SOURCE_KIND = "watchdog"
#: Works suspended for these reasons are one storage occurrence, not one per Work.
CAPACITY_REASONS = frozenset({"work_protocol_storage_capacity", "work_journal_capacity"})
#: How many IDs one issue lists; the count is always exact.
_LISTED_IDS = 20
#: While turned off (interval 0), how often the hot setting is re-read.
_OFF_RECHECK_SECONDS = 60.0

CONTENT = (
    "看门狗发现了上面运行资料 watchdog 里的新问题。用你自己的话告诉对方发生了什么、影响什么、"
    "建议怎么处理，并写出处理需要的内部 ID；这次只汇报，不执行任何管理操作，"
    "对方同意后会在普通对话里处理。"
)

# A loop/component fact: {"running": bool | None, "errors": {field: category | None}}
# and {"healthy": bool | None, "state": str, "durable": bool}.
FactSource = Callable[[], Awaitable[Mapping[str, Mapping[str, Any]]]]


@dataclass(slots=True)
class Observation:
    present: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Keys (or key prefixes) that must be neither opened nor cleared this time.
    undetermined: set[str] = field(default_factory=set)

    def unknown(self, key: str) -> bool:
        return any(key.startswith(prefix) for prefix in self.undetermined)


class Watchdog:
    def __init__(
        self,
        database: Database,
        turns: ProactivePrivateTurns,
        runtime_config: RuntimeConfigService,
        *,
        loops: FactSource,
        components: FactSource,
        device_online: Callable[[str], bool],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.turns = turns
        self._config = runtime_config
        self._loops = loops
        self._components = components
        self._device_online = device_online
        self._clock = clock
        self._first_seen: dict[str, float] = {}
        self._absent_since: dict[tuple[str, str], float] = {}
        self._present: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._attempts: dict[str, asyncio.Task[str]] = {}
        self._attempt_error: str | None = None
        # Recent attempt outcomes (tests read them through ``drain``); bounded.
        self.outcomes: collections.deque[str] = collections.deque(maxlen=64)
        self._enabled: bool | None = None
        self._last_check: float | None = None
        self._last_error: str | None = None
        self._check_errors: dict[str, str] = {}
        self.writes = 0
        turns.register(
            SOURCE_KIND,
            ProactiveSource(content=CONTENT, material=self._material, on_claim=self._on_claim),
        )

    # ---- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        await self.turns.recover()
        self._task = asyncio.create_task(self._loop(), name="watchdog")

    async def close(self) -> None:
        tasks = [task for task in (self._task, *self._attempts.values()) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self) -> None:
        interval = _OFF_RECHECK_SECONDS
        while True:
            try:
                # Any failure, the config read included, is one failed round: next round
                # tries again; the loop never ends silently.
                pace = (await self._config.snapshot()).watchdog.check_interval_seconds
                self._enabled = pace > 0
                # A failed round retries after the last interval read.
                interval = pace if self._enabled else _OFF_RECHECK_SECONDS
                if self._enabled:
                    await self.tick()
                self._last_error = None
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("watchdog_check_failed category=%s", self._last_error)
            await asyncio.sleep(interval)

    def status(self) -> dict[str, object]:
        """In-memory facts only (public ``/healthz``: no IDs, no queries)."""
        return {
            "enabled": self._enabled,
            "running": self._task is not None and not self._task.done(),
            "last_check_at": self._last_check,
            "last_error_category": self._last_error,
            "check_errors": dict(self._check_errors),
            "open_issues": len(self._present),
            "attempts_running": len(self._attempts),
            "last_attempt_error_category": self._attempt_error,
        }

    async def health(self) -> dict[str, Any]:
        return {**self.status(), **await self.turns.health()}

    # ---- one check -------------------------------------------------------------------

    async def tick(self) -> None:
        """Observe, register changes in one short writer, then start due turns."""
        snapshot = await self._config.snapshot()
        pace = snapshot.watchdog
        now = self._clock()
        observation = await self.observe(now, snapshot)
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
            rows = [
                dict(row)
                for row in (
                    await reader.execute(
                        select(
                            watchdog_issues.c.person_id,
                            watchdog_issues.c.issue_key,
                            watchdog_issues.c.turn_id,
                            proactive_turns.c.state,
                            proactive_turns.c.attempts,
                        ).outerjoin(
                            proactive_turns, proactive_turns.c.id == watchdog_issues.c.turn_id
                        )
                    )
                ).mappings()
            ]
            open_turns = {
                row["person_id"]: dict(row)
                for row in (
                    await reader.execute(
                        select(
                            proactive_turns.c.id,
                            proactive_turns.c.person_id,
                            proactive_turns.c.state,
                        ).where(
                            proactive_turns.c.source_kind == SOURCE_KIND,
                            or_(
                                proactive_turns.c.state.in_(("pending", "running")),
                                and_(
                                    proactive_turns.c.state == "failed",
                                    proactive_turns.c.attempts < MAX_ATTEMPTS,
                                ),
                            ),
                        )
                    )
                ).mappings()
            }
        plan = self._plan(now, pace.no_progress_seconds, observation, superusers, rows, open_turns)
        if plan.changed:
            async with self.database.immediate_session() as writer:
                await self._apply(writer, plan, now)
            self.writes += 1
        self._present = set(observation.present)
        self._last_check = now
        # A turn left running with no live attempt here (its attempt or process was
        # lost) is settled from its receipts, so the person is not blocked forever.
        await self.turns.recover(active=frozenset(self._attempts))
        for turn_id in await self.turns.due(
            now,
            min_interval=pace.notify_min_interval_seconds,
            retry_after=pace.check_interval_seconds,
        ):
            if turn_id not in self._attempts:
                task = asyncio.create_task(
                    self.turns.attempt(turn_id), name=f"proactive-turn-{turn_id}"
                )
                self._attempts[turn_id] = task
                task.add_done_callback(functools.partial(self._attempt_done, turn_id))

    def _attempt_done(self, turn_id: str, task: asyncio.Task[str]) -> None:
        if self._attempts.get(turn_id) is task:
            del self._attempts[turn_id]
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._attempt_error = type(error).__name__
            logger.warning(
                "proactive_turn_attempt_failed turn=%s category=%s", turn_id, self._attempt_error
            )
        else:
            self.outcomes.append(task.result())

    async def drain(self) -> list[str]:
        """Wait for the turn attempts started so far; their outcomes (tests and shutdown)."""
        tasks = list(self._attempts.values())
        await asyncio.gather(*tasks, return_exceptions=True)
        outcomes = list(self.outcomes)
        self.outcomes.clear()
        return outcomes

    # ---- planning (pure) ---------------------------------------------------------------

    def _plan(
        self,
        now: float,
        window: float,
        observation: Observation,
        superusers: set[str],
        rows: Sequence[Mapping[str, Any]],
        open_turns: Mapping[str, Mapping[str, Any]],
    ) -> _Plan:
        plan = _Plan()
        existing = {(row["person_id"], row["issue_key"]): row for row in rows}
        for person_id, key in existing:
            if person_id not in superusers:
                plan.deletes.append((person_id, key))
            elif key in observation.present:
                self._absent_since.pop((person_id, key), None)
            elif observation.unknown(key):
                continue
            else:
                since = self._absent_since.setdefault((person_id, key), now)
                if now - since >= window:
                    plan.deletes.append((person_id, key))
                    self._absent_since.pop((person_id, key), None)
        for person_id in superusers:
            untold: list[str] = []
            fresh = False
            for key, facts in observation.present.items():
                row = existing.get((person_id, key))
                if row is None:
                    plan.inserts.append((person_id, key, facts))
                    untold.append(key)
                    fresh = True
                elif row["turn_id"] is None:
                    untold.append(key)
                    fresh = True
                elif row["state"] == "failed" and row["attempts"] >= MAX_ATTEMPTS:
                    # Told nothing after its retry: it joins the next new batch.
                    untold.append(key)
            if not fresh:
                continue
            turn = open_turns.get(person_id)
            if turn is not None and turn["state"] != "pending":
                continue  # running or awaiting its retry: the next batch takes these
            plan.batches.append((person_id, turn["id"] if turn is not None else None, untold))
        return plan

    async def _apply(self, writer: AsyncSession, plan: _Plan, now: float) -> None:
        if plan.inserts:
            await writer.execute(
                insert(watchdog_issues).on_conflict_do_nothing(),
                [
                    {
                        "person_id": person_id,
                        "issue_key": key,
                        "opened": now,
                        "facts_json": json.dumps(facts, ensure_ascii=False, sort_keys=True),
                        "turn_id": None,
                    }
                    for person_id, key, facts in plan.inserts
                ],
            )
        for person_id, key in plan.deletes:
            await writer.execute(
                delete(watchdog_issues).where(
                    watchdog_issues.c.person_id == person_id, watchdog_issues.c.issue_key == key
                )
            )
        for person_id, turn_id, keys in plan.batches:
            target = watchdog_issues.c.person_id == person_id
            if turn_id is None:
                turn_id = await ProactivePrivateTurns.enqueue(writer, person_id, SOURCE_KIND, now)
            else:
                # Only a turn not yet claimed takes new rows; a claimed one keeps what
                # it read, and these rows wait for the next batch.
                target = and_(
                    target,
                    select(proactive_turns.c.id)
                    .where(proactive_turns.c.id == turn_id, proactive_turns.c.state == "pending")
                    .exists(),
                )
            await writer.execute(
                update(watchdog_issues)
                .where(target, watchdog_issues.c.issue_key.in_(keys))
                .values(turn_id=turn_id)
            )

    # ---- turn material ---------------------------------------------------------------

    async def _material(self, turn_id: str) -> dict[str, Any] | None:
        async with self.database.read_session() as reader:
            rows = list(
                (
                    await reader.execute(
                        select(
                            watchdog_issues.c.person_id,
                            watchdog_issues.c.issue_key,
                            watchdog_issues.c.opened,
                            watchdog_issues.c.facts_json,
                        )
                        .where(watchdog_issues.c.turn_id == turn_id)
                        .order_by(watchdog_issues.c.issue_key)
                    )
                ).mappings()
            )
        issues = [
            {
                "key": row["issue_key"],
                "opened_at": _iso(row["opened"]),
                "facts": json.loads(row["facts_json"]),
            }
            for row in rows
            # Only a problem observed healthy is left out; an undetermined one is said.
            if (row["person_id"], row["issue_key"]) not in self._absent_since
        ]
        if not issues:
            return None
        return {"watchdog": {"issues": issues, "issues_total": len(issues)}}

    @staticmethod
    async def _on_claim(
        writer: AsyncSession, turn_id: str, material: dict[str, Any] | None
    ) -> None:
        """Rows this turn will not mention are not told by it (all, when it says nothing)."""
        said = [issue["key"] for issue in material["watchdog"]["issues"]] if material else []
        await writer.execute(
            update(watchdog_issues)
            .where(watchdog_issues.c.turn_id == turn_id, watchdog_issues.c.issue_key.not_in(said))
            .values(turn_id=None)
        )

    # ---- checks ------------------------------------------------------------------------

    async def observe(self, now: float, snapshot: RuntimeConfigSnapshot) -> Observation:
        observation = Observation()
        checks: tuple[tuple[str, tuple[str, ...], Callable[..., Awaitable[None]]], ...] = (
            ("works", ("work_suspended:", "protocol_storage_"), self._check_works),
            ("protocol_gc", ("protocol_gc_stalled",), self._check_gc),
            ("delegations", ("delegation_", "operation_unconfirmed:"), self._check_delegations),
            ("automations", ("automation:",), self._check_automations),
            ("loops", ("loop_",), self._check_loops),
            ("components", ("component:",), self._check_components),
        )
        self._check_errors.clear()
        for name, prefixes, check in checks:
            try:
                await check(now, snapshot, observation)
            except Exception as exc:
                observation.undetermined.update(prefixes)
                observation.present = {
                    key: facts
                    for key, facts in observation.present.items()
                    if not key.startswith(prefixes)
                }
                self._check_errors[name] = type(exc).__name__
                logger.warning(
                    "watchdog_check_failed check=%s category=%s", name, type(exc).__name__
                )
        return observation

    async def _check_works(
        self, now: float, snapshot: RuntimeConfigSnapshot, observation: Observation
    ) -> None:
        async with self.database.read_session() as reader:
            suspended = await suspended_works(reader)
            used = await usage_bytes(reader)
        capacity = [row for row in suspended if row["reason"] in CAPACITY_REASONS]
        for row in suspended:
            if row["reason"] not in CAPACITY_REASONS:
                observation.present[f"work_suspended:{row['id']}"] = {
                    "work_id": row["id"],
                    "reason": _code(row["reason"]),
                    "parent_work_id": row["parent_work_id"],
                    "suspended_at": _iso(row["updated"]),
                }
        cap = snapshot.work_storage.total_max_bytes
        storage = {
            "usage_bytes": used,
            "cap_bytes": cap,
            "suspended_count": len(capacity),
            "suspended_work_ids": [row["id"] for row in capacity[-_LISTED_IDS:]],
        }
        # Two levels, two keys: near-cap stays open while at cap, so reaching the cap
        # is told again (an escalation) and falling back from it is not.
        if used >= cap * snapshot.watchdog.protocol_storage_warn_ratio or capacity:
            observation.present["protocol_storage_near_cap"] = {
                **storage,
                "warn_ratio": snapshot.watchdog.protocol_storage_warn_ratio,
            }
        if used >= cap or capacity:
            observation.present["protocol_storage_capacity"] = storage

    async def _check_gc(
        self, now: float, snapshot: RuntimeConfigSnapshot, observation: Observation
    ) -> None:
        before = now - GC_GRACE_SECONDS - snapshot.watchdog.no_progress_seconds
        async with self.database.read_session() as reader:
            stalled = await stalled_unowned_object(reader, before)
        if stalled is not None:
            observation.present["protocol_gc_stalled"] = {
                "oldest_unowned_prepared_at": _iso(stalled["prepared_at"]),
                "grace_seconds": GC_GRACE_SECONDS,
            }

    async def _check_delegations(
        self, now: float, snapshot: RuntimeConfigSnapshot, observation: Observation
    ) -> None:
        cutoff = now - snapshot.watchdog.no_progress_seconds
        async with self.database.read_session() as reader:
            records = await watch_records(reader)
        for record in records:
            for operation in record["operations"]:
                if (
                    not operation["final"]
                    and operation["unknown"]
                    and operation["created"] < cutoff
                ):
                    observation.present[f"operation_unconfirmed:{operation['id']}"] = {
                        "operation_id": operation["id"],
                        "action": operation["action"],
                        "record_id": record["id"],
                        "since": _iso(operation["created"]),
                    }
            if record["relationship"] == "observe":
                continue  # observation records tell their own owner
            base = {
                "record_id": record["id"],
                "parent_work_id": record["parent_work_id"],
                "state": record["state"],
                "reason": _code(record["reason"]),
            }
            waiting = record["waiting_on_parent"]
            if record["state"] == "waiting_user" and waiting:
                oldest = min(waiting, key=lambda item: item["received_at"])
                if _parse(oldest["received_at"]) < cutoff:
                    observation.present[f"delegation_waiting_parent:{record['id']}"] = {
                        **base,
                        "requests": [
                            {
                                "permission_id": item["permission_id"],
                                "kind": _code(item["kind"]),
                                "tool": _code(item["tool"]),
                                "received_at": item["received_at"],
                            }
                            for item in waiting[:_LISTED_IDS]
                        ],
                        "request_count": len(waiting),
                    }
                continue
            if record["state"] != "waiting_external" or record["updated"] >= cutoff:
                continue
            operations = record["operations"]
            if operations and all(item["final"] for item in operations) and not waiting:
                observation.present[f"delegation_orphaned:{record['id']}"] = base
                continue
            last = record["last_event"] if record["last_event"] is not None else record["updated"]
            if last < cutoff:
                online = record["reason"] != "device_offline" and bool(
                    record["device_id"] and self._device_online(record["device_id"])
                )
                mode = "online" if online else "offline"
                observation.present[f"delegation_silent:{record['id']}:{mode}"] = {
                    **base,
                    "device_id": record["device_id"],
                    "device_online": online,
                    "last_event_at": _iso(last),
                }

    async def _check_automations(
        self, now: float, snapshot: RuntimeConfigSnapshot, observation: Observation
    ) -> None:
        async with self.database.read_session() as reader:
            failing = await failing_automations(reader)
        for item in failing:
            item["error_category"] = _code(item["error_category"])
            observation.present[f"automation:{item['automation_id']}"] = item

    async def _check_loops(
        self, now: float, snapshot: RuntimeConfigSnapshot, observation: Observation
    ) -> None:
        window = snapshot.watchdog.no_progress_seconds
        for name, fact in (await self._loops()).items():
            if fact.get("running") is False:
                observation.present[f"loop_stopped:{name}"] = {"loop": name}
            for error_field, category in (fact.get("errors") or {}).items():
                prefix = f"loop_error:{name}:{error_field}:"
                key = f"{prefix}{_code(category)}" if category is not None else None
                # Another category (or none) restarts the persistence clock.
                for seen in [item for item in self._first_seen if item.startswith(prefix)]:
                    if seen != key:
                        del self._first_seen[seen]
                if key is None:
                    continue
                category = _code(category)
                first = self._first_seen.setdefault(key, now)
                if now - first >= window:
                    observation.present[key] = {
                        "loop": name,
                        "field": error_field,
                        "category": category,
                        "since": _iso(first),
                    }
                else:
                    observation.undetermined.add(key)

    async def _check_components(
        self, now: float, snapshot: RuntimeConfigSnapshot, observation: Observation
    ) -> None:
        window = snapshot.watchdog.no_progress_seconds
        for name, fact in (await self._components()).items():
            prefix = f"component:{name}:"
            healthy = fact.get("healthy")
            if healthy is None:
                observation.undetermined.add(prefix)
                continue
            if healthy:
                for seen in [key for key in self._first_seen if key.startswith(prefix)]:
                    del self._first_seen[seen]
                continue
            state = _code(fact.get("state")) or "unhealthy"
            key = f"{prefix}{state}"
            for seen in [item for item in self._first_seen if item.startswith(prefix)]:
                if seen != key:
                    del self._first_seen[seen]
            first = self._first_seen.setdefault(key, now)
            if fact.get("durable") or now - first >= window:
                observation.present[key] = {
                    "component": name,
                    "state": state,
                    "since": _iso(first),
                }
            else:
                observation.undetermined.add(key)


@dataclass(slots=True)
class _Plan:
    inserts: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    deletes: list[tuple[str, str]] = field(default_factory=list)
    batches: list[tuple[str, str | None, list[str]]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.inserts or self.deletes or self.batches)


_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _code(value: Any) -> str | None:
    """A reason/category code as facts carry it; free text never passes."""
    if value is None:
        return None
    text = str(value)
    return text if _CODE.fullmatch(text) else "other"


def _iso(epoch: float) -> str:
    """Display time (UTC+8 ISO), as every other runtime material shows it."""
    return local_iso(datetime.fromtimestamp(epoch, UTC), "Asia/Shanghai")


def _parse(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()
