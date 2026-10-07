"""Opt-in, offline replay of a private production backup before legacy cleanup.

Set YUKI_LEGACY_REPLAY_DB and YUKI_LEGACY_REPLAY_PROFILES to local backup
copies. No backup or credentials are committed, and this test has no gateway.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
import tomlkit
from tests.support.work_session import WorkSession

from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import parse_model_profile_catalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript


def _backup_rows(path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return list(
            db.execute(
                """SELECT w.id, w.conversation_id, w.generation, w.revision,
                          w.goal, w.source_json, w.state, w.model_requests,
                          w.tool_calls, w.sent_messages, w.checkpoint_json,
                          j.contract, j.source_revision, j.payload_json
                   FROM runtime_work AS w
                   JOIN runtime_work_journal AS j ON j.work_id = w.id
                   WHERE w.state = 'suspended'"""
            )
        )


def _pro_rows(path: Path) -> list[sqlite3.Row]:
    return [
        row
        for row in _backup_rows(path)
        if (json.loads(row["payload_json"])["transcript"].get("continuation") or {}).get(
            "profile_id"
        )
        == "pro"
    ]


def _work_counters(rows: list[sqlite3.Row]) -> dict[str, tuple[object, ...]]:
    return {
        row["id"]: tuple(
            row[column]
            for column in (
                "conversation_id",
                "generation",
                "revision",
                "state",
                "model_requests",
                "tool_calls",
                "sent_messages",
            )
        )
        for row in rows
    }


def _effect_states(path: Path, work_ids: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
        return {
            key: (work_id, state)
            for key, work_id, state in db.execute(
                "SELECT effect_key, work_id, state FROM runtime_work_effects"
            )
            if work_id in work_ids
        }


def _catalog_without_pro(config: Path) -> tuple[str, str]:
    document = tomlkit.parse(config.read_text(encoding="utf-8"))
    assert "pro" in document["profiles"]
    assert "pro" not in document["routes"].values()
    assert document.get("search_connection") != "pro"
    env: dict[str, str] = {}
    for profile in document["profiles"].values():
        for field, alias in profile.items():
            if field.endswith("_env") and isinstance(alias, str):
                env[alias] = (
                    "https://example.invalid"
                    if field == "base_url_env"
                    else "test-model"
                    if field == "model_env"
                    else "low"
                    if field == "reasoning_effort_env"
                    else "private-test-key"
                )

    before = parse_model_profile_catalog(tomlkit.dumps(document), environment=env)
    del document["profiles"]["pro"]
    after = parse_model_profile_catalog(tomlkit.dumps(document), environment=env)
    original = TaskModelExecutor(router=ModelRouter(before), pool=ModelClientPool())
    cleaned = TaskModelExecutor(router=ModelRouter(after), pool=ModelClientPool())
    assert original.profile_id(ModelTask.CHAT_AGENT) == cleaned.profile_id(ModelTask.CHAT_AGENT)
    assert original.profile_revision(ModelTask.CHAT_AGENT) == cleaned.profile_revision(
        ModelTask.CHAT_AGENT
    )
    return cleaned.profile_id(ModelTask.CHAT_AGENT), cleaned.profile_revision(ModelTask.CHAT_AGENT)


def _private_sources() -> tuple[Path, Path]:
    source_name = os.getenv("YUKI_LEGACY_REPLAY_DB")
    config_name = os.getenv("YUKI_LEGACY_REPLAY_PROFILES")
    if not source_name or not config_name:
        pytest.skip("private production backup paths not provided")
    source, config = Path(source_name), Path(config_name)
    assert source.is_file() and config.is_file()
    return source, config


@pytest.mark.asyncio
async def test_production_backup_preserves_five_legacy_works_without_gateway(
    tmp_path: Path,
) -> None:
    source, config = _private_sources()

    # SQLite's backup API captures one private, consistent test copy. All
    # recovery writes, if any, remain in this copy; the source is read only.
    replay = tmp_path / "legacy-replay.sqlite3"
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as original:
        with sqlite3.connect(replay) as copy:
            original.backup(copy)
    rows = _pro_rows(replay)
    assert len(rows) == 5
    before = _work_counters(rows)
    work_ids = tuple(before)
    effects_before = _effect_states(replay, work_ids)
    assert Counter(state for _, state in effects_before.values())["prepared"] >= 1
    assert (
        sum(
            bool(effect.get("uncertain"))
            for row in rows
            for effect in json.loads(row["payload_json"])["metadata"].get("effects", [])
        )
        >= 2
    )
    current_profile, current_revision = _catalog_without_pro(config)
    assert current_profile != "pro"

    database = Database(f"sqlite+aiosqlite:///{replay.as_posix()}")
    repository = WorkRepository(database)
    reasons: Counter[str] = Counter()
    try:
        for row in rows:
            lease = await repository.acquire(row["conversation_id"], row["generation"])
            assert lease is not None

            async def validate(current_lease=lease) -> None:
                assert await repository.valid(current_lease)

            control = WorkControl(
                repository,
                lease,
                "offline-legacy-audit",
                json.loads(row["source_json"]),
                validate,
            )
            control.current = await repository.get(row["id"])
            assert control.current is not None
            # A distinct contract models the currently routed provider. The
            # original Work identity and counters remain from the backup.
            contract = current_revision
            assert contract != row["contract"]
            session = WorkSession(control, contract)
            loaded = await session.journal.load(lease, row["id"], contract)
            assert loaded.reason in {"contract_changed", "source_changed"}
            reasons[loaded.reason] += 1
            initial = TurnTranscript(
                (
                    ChatMessage("system", "offline recovery contract"),
                    ChatMessage("user", row["goal"]),
                )
            )
            await session.restore(initial, compaction_brief=initial.request().messages[-1])
            assert control.current["id"] == row["id"]
            original_evidence = json.loads(row["checkpoint_json"]).get("execution_evidence", [])
            expected_uncertain = sum(bool(effect.get("uncertain")) for effect in original_evidence)
            assert sum(bool(effect.get("uncertain")) for effect in control.known_effects) >= (
                expected_uncertain
            )
            for effect in control.known_effects:
                if effect.get("uncertain"):
                    assert effect.get("side_effecting") or effect.get("effect_key")
            for key, (owner, state) in effects_before.items():
                if owner == row["id"] and state == "prepared":
                    outcome = json.loads(await session.journal.effect_result(key))
                    assert outcome["error"] == "execution_outcome_unknown"
                    assert outcome["replay_forbidden"] is True
            if expected_uncertain:
                invoked = False

                async def forbidden_send() -> str:
                    nonlocal invoked
                    invoked = True
                    return "unexpected-send"

                outcome = json.loads(
                    await session.execute(
                        ToolCall("offline-audit-send", ToolFunction("send_message", "{}")),
                        forbidden_send,
                    )
                )
                assert not invoked and outcome["executed"] is False
                assert outcome["error"] in {"unresolved_prior_effect", "new_input_before_execution"}
            await repository.release(lease)
    finally:
        await database.close()

    assert reasons["contract_changed"] >= 1
    assert _work_counters(_pro_rows(replay)) == before
    assert _effect_states(replay, work_ids) == effects_before
    # Only aggregate states are printed. No content, internal IDs or secrets.
    print(f"legacy replay: 5 works, {dict(reasons)}, effects unchanged")
