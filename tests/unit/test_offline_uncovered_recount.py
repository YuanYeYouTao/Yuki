"""Stopped/offline uncovered recount command: counts only, fail closed."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.conftest import make_settings

from qq_ai_bot.conversation.offline_recount import (
    UncoveredRecountError,
    UncoveredRecountReport,
    rollup_policy_from_settings,
    run_stopped_offline_uncovered_recount,
)
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.persistence.database import Database

_NOW = datetime(2026, 8, 26, tzinfo=UTC)


def test_recount_report_exposes_counts_only() -> None:
    report = UncoveredRecountReport(
        conversation_count=3,
        uncovered_event_count=12,
        uncovered_character_count=400,
    )
    payload = report.as_counts()
    assert payload == {
        "conversation_count": 3,
        "uncovered_event_count": 12,
        "uncovered_character_count": 400,
    }
    encoded = json.dumps(payload)
    assert "conversation_id" not in encoded
    assert "summary" not in encoded
    assert "content" not in encoded


async def test_offline_recount_fails_closed_on_incompatible_schema(tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'empty.db').as_posix()}"
    with pytest.raises(UncoveredRecountError, match="incompatible schema"):
        await run_stopped_offline_uncovered_recount(url, RollupPolicyConfig())


async def test_offline_recount_command_outputs_counts_without_ids(
    database: Database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.cli import _conversation_command
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.conversation.offline_recount import (
        check_all_canonical_uncovered,
        recount_all_canonical_uncovered,
    )
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    settings = make_settings(database.url)
    policy = rollup_policy_from_settings(settings)
    async with database.sessions() as session, session.begin():
        await ensure_presence(session, "8000")
        await ensure_person(session, "1001", now=_NOW)
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    scope = ConversationScope.private("8000", "1001")
    appended = await uow.append(
        scope=scope,
        platform_message_id="recount-human",
        sender_user_id="1001",
        direction="inbound",
        content="visible",
        occurred_at=_NOW,
    )
    await uow.append_external(
        scope=scope,
        platform_message_id="recount-ext",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key="recount",
        external_event_type="PushEvent",
        external_payload={"body": "do-not-print"},
        external_target_id="1001",
        content="external-secret-summary",
        occurred_at=_NOW,
    )
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, appended.event.canonical_conversation_id
        )
        assert conversation is not None
        conversation.uncovered_character_count = 99_999
        drift = await check_all_canonical_uncovered(session, policy)
        assert drift.mismatch_count == 1
        assert conversation.uncovered_character_count == 99_999
        report = await recount_all_canonical_uncovered(session, policy)
        repaired = await check_all_canonical_uncovered(session, policy)
        assert repaired.mismatch_count == 0
    assert report.uncovered_event_count == 2
    assert report.uncovered_character_count < 99_999
    assert report.uncovered_character_count > 0

    async def _fake_run(database_url: str, config: RollupPolicyConfig) -> UncoveredRecountReport:
        assert database_url == settings.database_url
        assert config.bot_display_name == policy.bot_display_name
        return report

    monkeypatch.setattr(
        "qq_ai_bot.cli.run_stopped_offline_uncovered_recount",
        _fake_run,
    )
    args = type("Args", (), {"conversation_command": "recount-uncovered"})()
    status = await _conversation_command(settings, args)
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert status == 0
    assert payload["ok"] is True
    assert payload["conversation_count"] == report.conversation_count
    assert payload["uncovered_event_count"] == 2
    assert "external-secret-summary" not in captured
    assert "do-not-print" not in captured
    assert appended.event.canonical_conversation_id not in captured
    assert "1001" not in captured
    assert "github-monitor" not in captured


async def test_offline_recount_check_returns_nonzero_for_drift(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.cli import _conversation_command
    from qq_ai_bot.conversation.offline_recount import UncoveredCheckReport

    settings = make_settings("sqlite+aiosqlite:///:memory:")

    async def _check(database_url: str, config: RollupPolicyConfig) -> UncoveredCheckReport:
        del database_url, config
        return UncoveredCheckReport(
            conversation_count=2,
            mismatch_count=1,
            uncovered_event_count=4,
            uncovered_character_count=20,
        )

    monkeypatch.setattr("qq_ai_bot.cli.run_offline_uncovered_check", _check)
    status = await _conversation_command(
        settings,
        type(
            "Args",
            (),
            {"conversation_command": "recount-uncovered", "check": True},
        )(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert status == 1
    assert payload == {
        "ok": False,
        "conversation_count": 2,
        "mismatch_count": 1,
        "uncovered_event_count": 4,
        "uncovered_character_count": 20,
    }


async def test_offline_check_does_not_change_sqlite_journal_mode(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.conversation.offline_recount import run_offline_uncovered_check

    async def _skip_schema(database_url: str) -> None:
        del database_url

    monkeypatch.setattr(
        "qq_ai_bot.conversation.offline_recount.require_canonical_schema",
        _skip_schema,
    )

    await database.close()
    path = database.url.removeprefix("sqlite+aiosqlite:///")
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    connection.close()

    await run_offline_uncovered_check(
        database.url,
        RollupPolicyConfig(),
    )

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    connection.close()


async def test_offline_check_does_not_create_a_missing_database(tmp_path: Path) -> None:
    from qq_ai_bot.conversation.offline_recount import run_offline_uncovered_check

    path = tmp_path / "missing.db"
    with pytest.raises(UncoveredRecountError, match="incompatible schema"):
        await run_offline_uncovered_check(
            f"sqlite+aiosqlite:///{path.as_posix()}",
            RollupPolicyConfig(),
        )
    assert not path.exists()


async def test_offline_recount_command_fail_closed_prints_no_ids(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.cli import _conversation_command

    settings = make_settings("sqlite+aiosqlite:///:memory:")

    async def _raise(database_url: str, config: RollupPolicyConfig) -> UncoveredRecountReport:
        del database_url, config
        raise UncoveredRecountError("incompatible schema")

    monkeypatch.setattr("qq_ai_bot.cli.run_stopped_offline_uncovered_recount", _raise)
    status = await _conversation_command(
        settings, type("Args", (), {"conversation_command": "recount-uncovered"})()
    )
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert status == 1
    assert payload == {"ok": False, "error": "incompatible schema"}
    assert "conversation_id" not in captured


async def test_offline_recount_rolls_back_when_later_generation_mismatches(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationModel,
        CanonicalConversationRollupModel,
    )
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    async def _skip_schema(database_url: str) -> None:
        del database_url

    monkeypatch.setattr(
        "qq_ai_bot.conversation.offline_recount.require_canonical_schema",
        _skip_schema,
    )
    policy = RollupPolicyConfig()
    now = _NOW
    async with database.sessions() as session, session.begin():
        await ensure_presence(session, "8000")
        await ensure_person(session, "1011", now=now)
        await ensure_person(session, "1012", now=now)
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    first = await uow.append(
        scope=ConversationScope.private("8000", "1011"),
        platform_message_id="rb-1",
        sender_user_id="1011",
        direction="inbound",
        content="keep-me",
        occurred_at=now,
    )
    second = await uow.append(
        scope=ConversationScope.private("8000", "1012"),
        platform_message_id="rb-2",
        sender_user_id="1012",
        direction="inbound",
        content="mismatch-me",
        occurred_at=now,
    )
    seeded = 11_111
    first_id = first.event.canonical_conversation_id
    second_id = second.event.canonical_conversation_id
    assert first_id and second_id
    async with database.immediate_session() as session:
        conversations = sorted(
            (await session.scalars(select(CanonicalConversationModel))).all(),
            key=lambda row: row.id,
        )
        by_id = {row.id: row for row in conversations}
        earlier = by_id[min(first_id, second_id)]
        later = by_id[max(first_id, second_id)]
        earlier.uncovered_character_count = seeded
        later.generation = 2
        session.add(
            CanonicalConversationRollupModel(
                conversation_id=later.id,
                generation=1,
                covered_through_event_id=0,
                summary_text="seed",
                summary_kind="extractive",
                source_fingerprint="ab" * 32,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        earlier_id = earlier.id
    with pytest.raises(UncoveredRecountError, match="invariant violation"):
        await run_stopped_offline_uncovered_recount(database.url, policy)
    async with database.sessions() as session:
        stored = await session.get(CanonicalConversationModel, earlier_id)
        assert stored is not None
        assert stored.uncovered_character_count == seeded


async def test_offline_recount_command_invariant_failure_json_is_content_free(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.cli import _conversation_command
    from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError

    settings = make_settings("sqlite+aiosqlite:///:memory:")

    async def _raise_invariant(
        database_url: str, config: RollupPolicyConfig
    ) -> UncoveredRecountReport:
        del database_url, config
        raise UncoveredRecountError("invariant violation")

    monkeypatch.setattr("qq_ai_bot.cli.run_stopped_offline_uncovered_recount", _raise_invariant)
    status = await _conversation_command(
        settings, type("Args", (), {"conversation_command": "recount-uncovered"})()
    )
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert status == 1
    assert payload == {"ok": False, "error": "invariant violation"}

    async def _raise_leaky(database_url: str, config: RollupPolicyConfig) -> UncoveredRecountReport:
        del database_url, config
        raise ConversationCoverageError(
            "cannot recount across rollup generations conversation_id=abc sqlite:////secret.db"
        )

    monkeypatch.setattr("qq_ai_bot.cli.run_stopped_offline_uncovered_recount", _raise_leaky)
    status = await _conversation_command(
        settings, type("Args", (), {"conversation_command": "recount-uncovered"})()
    )
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert status == 1
    assert payload == {"ok": False, "error": "recount failed"}
    assert "conversation_id" not in captured
    assert "secret.db" not in captured
    assert "abc" not in captured
