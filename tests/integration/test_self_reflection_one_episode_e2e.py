"""Self-reflection one-activity-one-episode (memory layering P2.1 §8, §13, §14.1).

Failure cases written before the change (every one is asserted below):

Instruction and schema (§8.1)
  F1  the old "一次可以输出多条 Episode …" sentences still reach the request, or the new
      "同一段活动只写一条" paragraph does not;
  F2  the kept sentence "同一天、同一群或都是自己参与，不足以合并为一条" is dropped;
  F3  the ``episodes`` field description in the schema sent to the model lacks
      "同一段活动只写一条";
  F4  importance changes: the field leaves the schema or "只是判断资料" leaves the instruction;
  F5  the request is fed existing episodes (no key beyond the current batch input);
  F6  a batch with several unrelated activities can no longer write several episodes
      (no per-batch cap: three proposed episodes are all committed).

Metric (§8.2)
  F7  proposals are counted as episodes;
  F8  initiative-receipt runs (slot ``self:…``, no messages) are counted as batches;
  F9  runs outside the requested local-date window are counted;
  F10 runs that are not completed are counted;
  F11 batches are bucketed into the wrong ISO week;
  F12 the query scans ``memory_self_reflection_runs`` instead of using the slot index, or
      counts results without the ``(run_id, result_kind, result_index)`` unique index;
  F13 ``yuki memory stats`` does not report the same rows.

Offline replay (§14.1)
  F14 the replay writes to the source database;
  F15 old and new instructions are not both sent for each selected batch;
  F16 an old episode without a new counterpart (no shared evidence) is not listed;
  F17 the most recent N completed chat batches are not the ones replayed.

Model behaviour is not tested here (the fake model decides the output); §14.1 replays
the deployed model. Each test writes normalized JSON to
``test-results/self-reflection-one-episode/<case>.json``; identical input gives
byte-identical files.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.replay_self_reflection_one_episode import _copy_read_only, replay
from sqlalchemy import event as sql_event
from sqlalchemy import func, insert, select
from tests.conftest import make_settings
from tests.support.memory_resident_scene import run_cli
from tests.support.model_executor import InjectedModelExecutor
from tests.support.service_dependencies import tool_dependencies
from tests.unit.test_memory_mutation import _event, _service

from yuki.llm.fake import FakeLLMProvider
from yuki.memory.self_reflection.reporting import self_reflection_weekly_stats
from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.memory.self_reflection.service import SelfReflectionService
from yuki.memory.self_reflection.worker import SelfReflectionWorker
from yuki.model_runtime.models import ModelTask, StructuredOutputMode
from yuki.persistence.database import Database
from yuki.persistence.models import (
    MemoryFactModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)

RESULTS = Path(__file__).resolve().parents[2] / "test-results" / "self-reflection-one-episode"
SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_EPISODE_RULE = (
    "episodes 是创建 Episode 的唯一输出位置，记录你在当前群聊或私聊中真实参与过、"
    "值得长期记住的经历。同一段活动只写一条：一局游戏、一次讨论、一次排查、一次闲聊各算一条，"
    "中间的回合、插话和阶段不拆开；本批里互不相干的几段活动才分成几条。"
    "本批只是 context_events 里已经开始的同一段活动的继续、又没有新的结果或转折时，不再另写。"
    "没有值得长期记住的事时 episodes 为空。"
)
KEPT_SENTENCE = "同一天、同一群或都是自己参与，不足以合并为一条。"
OLD_SENTENCE = "一次可以输出多条 Episode"
FIELD_RULE = "同一段活动只写一条"


class SchemaExecutor(InjectedModelExecutor):
    """Send the output schema in the request, as JSON-schema profiles do."""

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.JSON_SCHEMA


def _flat(text: str) -> str:
    return re.sub(r"\s*\n\s*", "", text)


def _episode(title: str, *refs: str) -> dict[str, object]:
    return {
        "title": title,
        "passages": [{"evidence_refs": list(refs), "content": f"我参与了{title}。"}],
        "importance": 3,
    }


def _proposal(ref: str) -> dict[str, object]:
    return {
        "category": "self_reflection",
        "kind": "fact",
        "memory_key": "self:one-episode-proposal",
        "content": "我在群里玩接龙时会先确认规则。",
        "evidence_refs": [ref],
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(case: str, payload: object) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{case}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8"
    )


class Scene:
    """Real ledger, real reflection worker/service/repository; only the model is fake."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.plan: dict[str, list[dict[str, object]]] = {}
        self.proposals: dict[str, list[dict[str, object]]] = {}
        self.provider = FakeLLMProvider(self._respond)
        self.mutations, _, self.ledger, _ = _service(database, self_memory_enabled=True)
        self.repository = SelfReflectionRepository(database)
        self.settings = make_settings(
            database.url,
            self_memory_enabled=True,
            memory_self_reflection_enabled=True,
            memory_self_reflection_event_threshold=1,
            memory_self_reflection_character_threshold=1,
        )
        self.worker = SelfReflectionWorker(
            settings=self.settings,
            repository=self.repository,
            service=SelfReflectionService(
                settings=self.settings,
                repository=self.repository,
                mutations=self.mutations,
                models=SchemaExecutor(self.provider),
            ),
            social=tool_dependencies(database, self.settings)["social_service"],
        )
        self.sequence = 0

    def _respond(self, request) -> str:
        payload = json.loads(request.messages[-1].content)
        scope = str(payload["scope_type"])
        return json.dumps(
            {"proposals": self.proposals.get(scope, []), "episodes": self.plan.get(scope, [])},
            ensure_ascii=False,
        )

    async def start(self) -> Scene:
        await self.repository.scan_new_events()
        return self

    async def say(self, *, group: int = 0, private: int = 0) -> None:
        for _ in range(group):
            self.sequence += 1
            await _event(
                self.ledger,
                message_id=f"one-episode-group-{self.sequence}",
                sender_user_id="1001" if self.sequence % 2 else "1002",
                content=f"接龙第 {self.sequence} 句",
                group_id="2001",
            )
        for _ in range(private):
            self.sequence += 1
            await _event(
                self.ledger,
                message_id=f"one-episode-private-{self.sequence}",
                sender_user_id="1001",
                content=f"私聊第 {self.sequence} 句",
            )

    async def cycle(self, day: date) -> int:
        return await self.worker.process_once(
            datetime(day.year, day.month, day.day, 12, tzinfo=SHANGHAI), force=True
        )


async def _counts(database: Database) -> dict[str, int]:
    async with database.read_session() as reader:
        return {
            "episode_facts": int(
                await reader.scalar(
                    select(func.count(MemoryFactModel.id)).where(MemoryFactModel.kind == "episode")
                )
                or 0
            ),
            "episode_results": int(
                await reader.scalar(
                    select(func.count(MemorySelfReflectionResultModel.id)).where(
                        MemorySelfReflectionResultModel.result_kind == "episode"
                    )
                )
                or 0
            ),
            "runs": int(
                await reader.scalar(select(func.count(MemorySelfReflectionRunModel.id))) or 0
            ),
        }


async def test_one_episode_instruction_reaches_request_and_weekly_metric(database):
    scene = await Scene(database).start()

    # Outside the window (W45): one group batch with one episode.
    await scene.say(group=2)
    scene.plan = {"group": [_episode("早先的接龙", "event_1")]}
    assert await scene.cycle(date(2026, 11, 3)) == 1

    # W46: a group batch with three unrelated activities and one proposal (F6, F7),
    # and a private batch with no episode.
    await scene.say(group=3, private=2)
    scene.plan = {
        "group": [
            _episode("一局成语接龙", "event_1"),
            _episode("排查机器人掉线", "event_2"),
            _episode("讨论周末聚餐", "event_3"),
        ],
        "private": [],
    }
    scene.proposals = {"group": [_proposal("event_1")]}
    requests_before = len(scene.provider.requests)
    assert await scene.cycle(date(2026, 11, 10)) == 2
    week_requests = scene.provider.requests[requests_before:]
    scene.proposals = {}

    # W47: one group batch, one episode.
    await scene.say(group=4)
    scene.plan = {"group": [_episode("又一局接龙", "event_1", "event_4")]}
    assert await scene.cycle(date(2026, 11, 17)) == 1

    # F8, F10: an initiative-receipt run and an unfinished run inside the window.
    async with database.read_session() as reader:
        owner = await reader.scalar(
            select(MemorySelfReflectionRunModel)
            .where(MemorySelfReflectionRunModel.canonical_space_id.is_not(None))
            .limit(1)
        )
        owner_space, owner_hash = owner.canonical_space_id, owner.conversation_key_hash
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        for slot, status, processed in (
            ("self:" + "a" * 27, "completed", 0),
            ("2026-11-18:unfinished:0", "failed", 5),
        ):
            await writer.execute(
                insert(MemorySelfReflectionRunModel).values(
                    conversation_key_hash=owner_hash,
                    bot_user_id="8000",
                    canonical_space_id=owner_space,
                    scheduled_slot=slot,
                    trigger_reason="event_count",
                    first_event_id=0,
                    last_event_id=0,
                    status=status,
                    started_at=now,
                    processed_events=processed,
                )
            )

    # F1–F5: the instruction and schema of every request.
    request = week_requests[0]
    system = request.messages[0].content
    assert _flat(NEW_EPISODE_RULE) in _flat(system)
    assert KEPT_SENTENCE in _flat(system)
    assert OLD_SENTENCE not in system
    assert "importance 只是判断资料" in system
    schema = request.response_format["json_schema"]["schema"]
    assert FIELD_RULE in schema["properties"]["episodes"]["description"]
    assert "importance" in schema["$defs"]["SelfEpisodeProposal"]["properties"]
    payload_keys = sorted(json.loads(request.messages[-1].content))
    assert set(payload_keys) <= {
        "source_kind",
        "scope_type",
        "group_id",
        "private_peer_user_id",
        "person_id",
        "space_id",
        "context_events",
        "events",
        "tool_receipts",
    }
    counts = await _counts(database)
    assert counts["episode_facts"] == counts["episode_results"] == 5

    # F9, F11, F12: the metric for 11-09..11-22 with its executed statements' plans.
    captured: list[tuple[str, object]] = []

    def capture(_connection, _cursor, sql, parameters, _context, _many):
        captured.append((sql, parameters))

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        weekly = await self_reflection_weekly_stats(
            database, since=date(2026, 11, 9), until=date(2026, 11, 22)
        )
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert weekly == [
        {
            "week": "2026-W46",
            "batches": 2,
            "processed_events": 5,
            "episodes": 3,
            "episodes_per_100_events": 60.0,
            "batches_with_3_plus_episodes": 1,
            "batches_without_episode": 1,
        },
        {
            "week": "2026-W47",
            "batches": 1,
            "processed_events": 4,
            "episodes": 1,
            "episodes_per_100_events": 25.0,
            "batches_with_3_plus_episodes": 0,
            "batches_without_episode": 0,
        },
    ]
    plans: list[str] = []
    async with database.read_session() as reader:
        connection = await reader.connection()
        for sql, parameters in captured:
            plans.extend(
                str(row[3])
                for row in (
                    await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)
                ).all()
            )
    assert any("ix_memory_self_reflection_runs_slot" in line for line in plans), plans
    # The (run_id, result_kind, result_index) unique constraint's index.
    assert any(
        "memory_self_reflection_results USING COVERING INDEX" in line
        and "(run_id=? AND result_kind=?)" in line
        for line in plans
    ), plans
    assert not any(
        line.startswith("SCAN") and "memory_self_reflection" in line for line in plans
    ), plans

    # F13: the real `yuki memory stats` child process reports the same rows for its window.
    stats = await run_cli(database.url, "memory", "stats", "--hours", str(24 * 90))
    assert stats.returncode == 0, stats.stderr
    local_now = datetime.now(SHANGHAI)
    assert json.loads(stats.stdout)["self_reflection_by_week"] == (
        await self_reflection_weekly_stats(
            database, since=(local_now - timedelta(days=90)).date(), until=local_now.date()
        )
    )

    _write(
        "instruction-and-metric",
        {
            "system_instruction_has_new_rule": _flat(NEW_EPISODE_RULE) in _flat(system),
            "system_instruction_has_old_sentence": OLD_SENTENCE in system,
            "episodes_field_description": schema["properties"]["episodes"]["description"],
            "payload_keys": payload_keys,
            "counts": counts,
            "weekly": weekly,
            "query_plan": sorted(set(plans)),
        },
    )


async def test_offline_replay_compares_old_and_new_instructions_without_writing(database, tmp_path):
    scene = await Scene(database).start()
    for day, group in ((date(2026, 11, 3), 2), (date(2026, 11, 10), 3), (date(2026, 11, 17), 4)):
        await scene.say(group=group)
        scene.plan = {"group": [_episode("接龙", "event_1")]}
        assert await scene.cycle(day) == 1
    async with database.read_session() as reader:
        recent = list(
            await reader.scalars(
                select(MemorySelfReflectionRunModel.id).order_by(MemorySelfReflectionRunModel.id)
            )
        )[-2:]
    source = Path(database.engine.url.database)
    await database.close()
    digest = _digest(source)

    def respond(request) -> str:
        system = request.messages[0].content
        payload = json.loads(request.messages[-1].content)
        refs = [item["ref"] for item in payload["events"]]
        if OLD_SENTENCE in system:
            episodes = [_episode(f"第 {index} 回合", ref) for index, ref in enumerate(refs, 1)]
        elif len(refs) == 4:
            # The new instruction keeps one episode but drops the last round (F16).
            episodes = [_episode("一局接龙", *refs[:-1])]
        else:
            episodes = [_episode("一局接龙", *refs)]
        return json.dumps({"proposals": [], "episodes": episodes}, ensure_ascii=False)

    provider = FakeLLMProvider(respond)
    copy = tmp_path / "replay.db"
    _copy_read_only(source, copy)
    replica = Database(f"sqlite+aiosqlite:///{copy.as_posix()}")
    try:
        report = await replay(replica, scene.settings, SchemaExecutor(provider), runs=2)
    finally:
        await replica.close()
    assert _digest(source) == digest

    # F15, F17: the two most recent batches, each sent once per instruction.
    assert [batch["run_id"] for batch in report["batches"]] == recent
    assert len(provider.requests) == 4
    systems = [request.messages[0].content for request in provider.requests]
    assert sum(OLD_SENTENCE in text for text in systems) == 2
    assert sum(_flat(NEW_EPISODE_RULE) in _flat(text) for text in systems) == 2
    for request in provider.requests:
        description = request.response_format["json_schema"]["schema"]["properties"][
            "episodes"
        ].get("description", "")
        assert (FIELD_RULE in description) is (OLD_SENTENCE not in request.messages[0].content)
    first, second = report["batches"]
    assert (first["old"]["count"], first["new"]["count"]) == (3, 1)
    assert first["old_without_new_counterpart"] == []
    assert (second["old"]["count"], second["new"]["count"]) == (4, 1)
    assert [item["title"] for item in second["old_without_new_counterpart"]] == ["第 4 回合"]
    assert report["totals"] == {
        "batches": 2,
        "processed_events": 7,
        "old": {
            "episodes": 7,
            "episodes_per_100_events": 100.0,
            "batches_with_3_plus_episodes": 2,
            "batches_without_episode": 0,
            "errors": 0,
        },
        "new": {
            "episodes": 2,
            "episodes_per_100_events": 28.57,
            "batches_with_3_plus_episodes": 0,
            "batches_without_episode": 0,
            "errors": 0,
        },
    }
    # Slots carry the random cycle ID after the local date.
    assert [batch["scheduled_slot"][:11] for batch in report["batches"]] == [
        "2026-11-10:",
        "2026-11-17:",
    ]
    for batch in report["batches"]:
        batch["scheduled_slot"] = batch["scheduled_slot"][:10]
    _write("offline-replay", report)
