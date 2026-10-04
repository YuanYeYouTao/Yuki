"""One-prepare display metadata and body-before-identity fences."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_commands_and_chat import inbound
from tests.unit.test_rollup_snapshot_read_budget import _seed

from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import PersonAliasModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.time.service import TimeContextService


@pytest.mark.asyncio
async def test_real_ordinary_turn_reads_timezone_once_and_preserves_relationship_create(
    database, monkeypatch
):
    provider = FakeLLMProvider("synthetic ordinary response")
    harness = build_harness(database, make_settings(database.url), provider)
    time = harness.processor._chat._time
    statements = []

    async def forbidden_second_resolution(_user_id):
        pytest.fail("Runner must use this preparation's timezone without a second owner read")

    monkeypatch.setattr(time, "timezone_for", forbidden_second_resolution)

    def capture(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await harness.processor.handle(
            inbound("synthetic ordinary input", message_id="one-timezone", user_id="1001"),
            MemorySender(),
        )
        assert provider.requests
        assert sum("FROM person_time_settings" in sql for sql in statements) == 1
        assert any(sql.startswith("INSERT INTO person_relationships") for sql in statements)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_metadata_matches_existing_views_and_reduces_repeated_reads(database):
    now = datetime(2026, 10, 4, 23, 59, tzinfo=UTC)
    async with database.immediate_session() as session:
        person_id = await ensure_person(session, "1001")
        session.add(
            IdentityBindingModel(
                id="3d1dcf40-5a40-4fd6-9973-76a9e17253cd",
                person_id=person_id,
                platform="qq",
                external_account_id="50102",
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        for index, name in enumerate(("older", "newer")):
            session.add(
                PersonAliasModel(
                    canonical_person_id=person_id,
                    alias=name,
                    alias_type="nickname",
                    first_seen_at=now,
                    last_seen_at=now + timedelta(seconds=index),
                )
            )
    time = TimeContextService(database)
    await time.set_timezone("1001", "Asia/Taipei")
    people = PeopleRepository(database)
    statements = []

    def capture(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        old = (await people.aliases("1001"), await time.timezone_for("1001"))
        old_selects = sum(sql.lstrip().upper().startswith("SELECT") for sql in statements)
        statements.clear()
        metadata = await people.prompt_metadata(
            "1001", default_timezone=time.default_timezone, expected_person_id=person_id
        )
        new_selects = sum(sql.lstrip().upper().startswith("SELECT") for sql in statements)
        print(f"metadata_stage_selects old={old_selects} prepared={new_selects}")
        assert statements[0] == "BEGIN"
        assert (metadata.aliases, metadata.timezone) == old
        assert new_selects < old_selects
        assert not any(
            sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
        )
        assert (
            await people.prompt_metadata(
                "50102", default_timezone=time.default_timezone, expected_person_id=person_id
            )
            == metadata
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_metadata_is_per_prepare_clock_is_fresh_and_invalid_owners_fail(database):
    class Clock:
        moment = datetime(2026, 10, 4, 23, 59, tzinfo=UTC)

        def now(self):
            return self.moment

    async with database.immediate_session() as session:
        person_id = await ensure_person(session, "1001")
        presence_id = await ensure_presence(session, "8000")
        space_id = await ensure_space(session, "2001")
    clock = Clock()
    time = TimeContextService(database, clock=clock)
    people = PeopleRepository(database)
    initial = await people.prompt_metadata("1001", default_timezone="UTC")
    first = time.current_in_timezone(initial.timezone)
    clock.moment += timedelta(minutes=2)
    assert time.current_in_timezone(initial.timezone).local.day != first.local.day
    await time.set_timezone("1001", "America/New_York")
    changed = await people.prompt_metadata("1001", default_timezone="UTC")
    assert changed.timezone == "America/New_York" and initial.timezone == "UTC"
    for invalid in (presence_id, space_id, "8000", "self", "missing"):
        with pytest.raises(CanonicalIdentityError):
            await people.prompt_metadata(invalid, default_timezone="UTC")
    with pytest.raises(CanonicalIdentityError) as failure:
        await people.prompt_metadata("1001", default_timezone="UTC", expected_person_id=space_id)
    assert failure.value.category == "canonical_owner_mismatch"
    async with database.immediate_session() as session:
        row = await session.get(CanonicalPersonModel, person_id)
        row.enabled = False
    with pytest.raises(CanonicalIdentityError):
        await people.prompt_metadata("1001", default_timezone="UTC")


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["scope_id", "generation", "scope_key", "transport_scope_key"])
async def test_expected_turn_rejected_before_history_body_read(database, wrong):
    repository, scope, events = await _seed(database, count=3)
    snapshot = await repository.load_prompt_snapshot(scope, token_budget=100000)
    turn = ConversationTurnSnapshot(
        scope_id=snapshot.scope.id,
        scope_key=snapshot.scope.runtime_scope_key or scope.key,
        generation=snapshot.scope.generation,
        trigger_event_id=events[-1].id,
        coordinator_version=1,
    )
    mismatched = replace(
        turn,
        **{wrong: getattr(turn, wrong) + 1 if wrong in {"scope_id", "generation"} else "wrong-key"},
    )
    statements = []

    def capture(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(ConversationCoverageError, match="turn identity changed"):
            await repository.load_prompt_snapshot(scope, expected_turn=mismatched)
        assert statements[0] == "BEGIN"
        assert not any("chat_events.content" in sql for sql in statements)
        assert (
            await repository.load_prompt_snapshot(scope, expected_turn=turn, token_budget=100000)
            == snapshot
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
