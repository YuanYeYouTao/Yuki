"""Metadata-first history preserves the full model view and WAL source fences."""

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.projections import ProjectionConflict, PromptProjectionRepository
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatMessage
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.services.context_assembler import AssembledContext, ContextMetrics
from yuki.services.history_projection import prepare_history
from yuki.services.turn_coordinator import HistorySourceChangedError


@contextmanager
def capture(database):
    statements = []

    def recorded(_conn, cursor, sql, *_args):
        rows = getattr(cursor, "_rows", ())
        statements.append((sql, len(rows)))

    event.listen(database.engine.sync_engine, "after_cursor_execute", recorded)
    try:
        yield statements
    finally:
        event.remove(database.engine.sync_engine, "after_cursor_execute", recorded)


def body_rows(statements):
    return sum(
        rows
        for sql, rows in statements
        if "FROM chat_events" in sql and "chat_events.content" in sql
    )


def metadata_rows(statements):
    return sum(
        rows
        for sql, rows in statements
        if "FROM chat_events" in sql and "chat_events.content" not in sql
    )


async def scene(database, count):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    registry.connect(bot, provider_id="snowluma")
    admitted = await resolver.pre_admit(bot, _message(message_id="missing-history-seed"))
    assert admitted is not None and not admitted.dropped
    receipt = await uow.append_inbound(admitted.message, admitted)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        source = await writer.get(ChatEventModel, receipt.event.id)
        seed = {
            col.name: getattr(source, col.name)
            for col in ChatEventModel.__table__.columns
            if col.name != "id"
        }
        await writer.execute(
            insert(ChatEventModel),
            [
                dict(
                    seed,
                    canonical_event_id=str(uuid4()),
                    platform_message_id=f"missing-history-{i}",
                    content=f"synthetic history {i}",
                    occurred_at=now + timedelta(seconds=i),
                    reply_to_event_id=receipt.event.id if i % 3 == 0 else None,
                )
                for i in range(count)
            ],
        )
        current = await writer.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id.desc()))
        owner = await writer.get(CanonicalConversationModel, admitted.conversation_id)
        owner.last_event_id = current
    ledger = EventLedgerRepository(database)
    version, rows = await ledger.read_scope_context(admitted.message.scope(), limit=count + 2)
    parts = tuple(((row.id,), ChatMessage("user", row.content)) for row in rows[:-1])
    context = AssembledContext(
        metadata_payload={},
        history_messages=tuple(message for _, message in parts),
        current_message=ChatMessage("user", "current"),
        metrics=ContextMetrics(0, 0, 0, 0, False),
        read_version=version,
        history_fragments=parts,
        history_event_fragments=parts,
        visible_event_ids=frozenset(row.id for row in rows),
        current_event_id=current,
        prompt_raw_tail_end_event_id=current,
    )
    return ledger, context, seed


async def legacy_missing(
    ledger, expected, *, after_event_id, through_event_id, frozen_event_ids, current_event_id
):
    """The prior complete-body path, only for output/read-count comparison."""
    rows = []
    after = after_event_id
    while after < through_event_id:
        page = await ledger.list_scope_after(
            expected.scope,
            after_event_id=after,
            through_event_id=through_event_id,
            limit=256,
            message_only=True,
        )
        if not page:
            break
        rows.extend(
            row for row in page if row.id not in frozen_event_ids and row.id != current_event_id
        )
        after = page[-1].id
    return expected, tuple(rows)


@pytest.mark.parametrize("legacy_renderer", [None, 1])
async def test_empty_legacy_checkpoint_appends_without_rebuilding_frozen_history(
    database, legacy_renderer
):
    _ledger, context, _seed = await scene(database, 4)
    original = FrozenFragments.load([]).extend_history(
        (
            (
                tuple(identity for ids, _ in context.history_fragments[:2] for identity in ids),
                ChatMessage("user", "original frozen group rendering"),
            ),
        ),
        context.history_event_fragments,
    )
    repository = PromptProjectionRepository(database)
    version = context.read_version
    publication = await repository.prepare_commit(
        view_key=version.conversation_id,
        conversation_id=version.conversation_id,
        generation=version.generation,
        expected_source_revision=version.prompt_source_revision,
        starts_after_event_id=version.starts_after_event_id,
        context_key="existing-public-window",
        fragments=FrozenFragments.load(list(original.items)),
        rebuild_reason="bootstrap",
        selected_summary_text="",
        selected_summary_coverage=version.starts_after_event_id,
        selected_summary_renderer=legacy_renderer,
    )
    async with database.immediate_session() as writer:
        previous = await publication(writer)

    prepared = await prepare_history(
        repository,
        context,
        view_key=version.conversation_id,
        context_key="existing-public-window",
    )
    assert prepared.reason is None
    publication = await prepared.prepare_commit(prepared.fragments)
    async with database.immediate_session() as writer:
        appended = await publication(writer)

    assert appended.epoch_id == previous.epoch_id
    assert appended.revision == previous.revision + 1
    assert appended.selected_summary_text == ""
    assert appended.selected_summary_renderer == 2
    assert appended.items()[: len(original.items)] == list(original.items)
    assert FrozenFragments.load(appended.items()).event_ids == frozenset(
        identity for ids, _ in context.history_fragments for identity in ids
    )


@pytest.mark.parametrize(
    "case", ["16", "64", "160", "600", "coverage", "holes", "grouped", "summary"]
)
async def test_full_prepare_equivalence_and_only_missing_body_reads(database, monkeypatch, case):
    count = int(case) if case.isdecimal() else 600 if case == "coverage" else 20
    _ledger, context, _seed = await scene(database, count)
    old_parts = context.history_fragments
    if case == "coverage":
        old_parts = old_parts[:200]
    elif case == "holes":
        old_parts = old_parts[::3]
    elif case == "summary":
        old_parts = old_parts[5:10]
    elif case == "grouped":
        grouped = tuple(
            (
                tuple(identity for ids, _ in old_parts[i : i + 2] for identity in ids),
                ChatMessage("user", "frozen grouped input"),
            )
            for i in range(0, len(old_parts), 2)
        )
        context = replace(
            context,
            history_fragments=grouped,
            history_messages=tuple(message for _, message in grouped),
        )
        old_parts = grouped[:1]
    frozen = FrozenFragments.load([]).extend_history(old_parts, context.history_event_fragments)
    repository = PromptProjectionRepository(database)
    version = context.read_version
    publication = await repository.prepare_commit(
        view_key="a" * 64,
        context_key="b" * 64,
        conversation_id=version.conversation_id,
        generation=version.generation,
        expected_source_revision=version.prompt_source_revision,
        starts_after_event_id=version.starts_after_event_id,
        fragments=FrozenFragments.load(list(frozen.items)),
        rebuild_reason="bootstrap",
        # Current producer records the full representation. Legacy NULL metadata
        # is separately tested as an explicit epoch rebuild, never silent reuse.
        selected_summary_kind="model" if case == "summary" else context.metrics.checkpoint_kind,
        selected_summary_renderer=2,
        selected_summary_text="selected summary" if case == "summary" else "",
        selected_summary_coverage=5 if case == "summary" else 0,
    )
    async with database.immediate_session() as writer:
        saved = await publication(writer)
    if case == "coverage":
        suffix = context.history_fragments[400:]
        context = replace(
            context,
            history_fragments=suffix,
            history_event_fragments=suffix,
            history_messages=tuple(message for _, message in suffix),
            checkpoint_text="new derived summary",
            prompt_effective_coverage=400,
            visible_event_ids=frozenset(range(401, context.current_event_id + 1)),
        )
    args = dict(
        view_key="a" * 64,
        context_key="b" * 64,
    )
    with monkeypatch.context() as patch:
        patch.setattr(EventLedgerRepository, "read_scope_missing_history", legacy_missing)
        with capture(database) as before_sql:
            baseline = await prepare_history(repository, context, **args)
    with capture(database) as after_sql:
        actual = await prepare_history(repository, context, **args)
    assert actual.context == baseline.context
    assert actual.fragments.items == baseline.fragments.items
    assert actual.fragments.messages() == baseline.fragments.messages()
    assert actual.reason == baseline.reason
    assert (await repository.read("a" * 64)).payload_json == saved.payload_json
    if case.isdecimal():
        assert body_rows(before_sql) == count + 1
        assert body_rows(after_sql) == 0 and metadata_rows(after_sql) == count + 1
    else:
        assert body_rows(after_sql) < body_rows(before_sql)
    if case == "coverage":
        assert set(range(201, 401)) <= actual.fragments.event_ids
    assert not any(
        sql.startswith(("UPDATE", "INSERT", "DELETE", "BEGIN IMMEDIATE")) for sql, _ in after_sql
    )


async def test_excluded_pages_advance_to_one_unseen_body_and_exclude_trigger(database):
    ledger, context, _seed = await scene(database, 600)
    ids = sorted(context.visible_event_ids)
    missing = ids[-2]
    with capture(database) as statements:
        actual_version, records = await ledger.read_scope_missing_history(
            context.read_version,
            after_event_id=0,
            through_event_id=context.current_event_id,
            frozen_event_ids=frozenset(ids[:-2]),
            current_event_id=context.current_event_id,
        )
    assert actual_version == context.read_version and [row.id for row in records] == [missing]
    assert body_rows(statements) == 1 and metadata_rows(statements) == 601
    assert all("NOT IN" not in sql for sql, _ in statements)


@pytest.mark.parametrize("change", ["generation", "revision", "boundary", "erase"])
async def test_changed_source_before_snapshot_loads_no_body(database, change):
    ledger, context, _seed = await scene(database, 16)
    version = context.read_version
    async with database.immediate_session() as writer:
        if change == "erase":
            await writer.execute(
                delete(ChatEventModel).where(
                    ChatEventModel.id == sorted(context.visible_event_ids)[1]
                )
            )
        field = {
            "generation": "generation",
            "revision": "prompt_source_revision",
            "boundary": "starts_after_event_id",
            "erase": "prompt_source_revision",
        }[change]
        values = {field: getattr(version, field) + 1}
        await writer.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == version.conversation_id)
            .values(values)
        )
    with capture(database) as statements:
        actual, rows = await ledger.read_scope_missing_history(
            version,
            after_event_id=0,
            through_event_id=context.current_event_id,
            frozen_event_ids=frozenset(),
            current_event_id=context.current_event_id,
        )
    assert actual != version and rows == ()
    assert body_rows(statements) == 0 and metadata_rows(statements) == 0


@pytest.mark.parametrize("change", ["edit", "erase", "new_event"])
async def test_metadata_and_bodies_share_wal_snapshot_and_fixed_highwater(
    database, monkeypatch, change
):
    ledger, context, seed = await scene(database, 16)
    original = AsyncSession.scalars
    injected = False
    target = sorted(context.visible_event_ids)[1]

    async def raced(session, statement, *args, **kwargs):
        nonlocal injected
        result = await original(session, statement, *args, **kwargs)
        sql = str(statement)
        if not injected and sql.startswith("SELECT chat_events.id"):
            injected = True
            async with database.immediate_session() as writer:
                if change == "new_event":
                    await writer.execute(
                        insert(ChatEventModel).values(
                            dict(
                                seed,
                                canonical_event_id=str(uuid4()),
                                platform_message_id="beyond-highwater",
                                content="next request only",
                            )
                        )
                    )
                else:
                    if change == "erase":
                        await writer.execute(
                            delete(ChatEventModel).where(ChatEventModel.id == target)
                        )
                    else:
                        await writer.execute(
                            update(ChatEventModel)
                            .where(ChatEventModel.id == target)
                            .values(content="edited body")
                        )
                    await writer.execute(
                        update(CanonicalConversationModel)
                        .where(
                            CanonicalConversationModel.id == context.read_version.conversation_id
                        )
                        .values(
                            prompt_source_revision=context.read_version.prompt_source_revision + 1
                        )
                    )
        return result

    monkeypatch.setattr(AsyncSession, "scalars", raced)
    version, records = await ledger.read_scope_missing_history(
        context.read_version,
        after_event_id=0,
        through_event_id=context.current_event_id,
        frozen_event_ids=frozenset(),
        current_event_id=context.current_event_id,
    )
    assert injected and version == context.read_version
    assert [row.id for row in records] == sorted(
        context.visible_event_ids - {context.current_event_id}
    )
    assert next(row for row in records if row.id == target).content != "edited body"
    assert await ledger.read_version_matches(context.read_version) == (change == "new_event")


async def test_zero_missing_can_finish_with_writer_held_and_does_not_claim_commit_authority(
    database,
):
    ledger, context, _seed = await scene(database, 16)
    async with database.immediate_session():
        with capture(database) as statements:
            version, rows = await ledger.read_scope_missing_history(
                context.read_version,
                after_event_id=0,
                through_event_id=context.current_event_id,
                frozen_event_ids=context.visible_event_ids,
                current_event_id=context.current_event_id,
            )
    assert version == context.read_version and rows == () and body_rows(statements) == 0
    assert not any(
        sql.startswith(("UPDATE", "INSERT", "DELETE", "BEGIN IMMEDIATE")) for sql, _ in statements
    )


async def test_history_preparation_version_change_is_source_error(database, monkeypatch):
    _ledger, context, _seed = await scene(database, 16)
    repository = PromptProjectionRepository(database)
    version = context.read_version
    frozen = FrozenFragments.load([]).extend_history(
        context.history_fragments, context.history_event_fragments
    )
    publication = await repository.prepare_commit(
        view_key="a" * 64,
        context_key="b" * 64,
        conversation_id=version.conversation_id,
        generation=version.generation,
        expected_source_revision=version.prompt_source_revision,
        starts_after_event_id=version.starts_after_event_id,
        fragments=FrozenFragments.load(list(frozen.items)),
        rebuild_reason="bootstrap",
        selected_summary_text="",
        selected_summary_coverage=0,
    )
    async with database.immediate_session() as writer:
        await publication(writer)
    original = EventLedgerRepository.read_scope_missing_history

    async def changed(*args, **kwargs):
        async with database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == version.conversation_id)
                .values(prompt_source_revision=version.prompt_source_revision + 1)
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(EventLedgerRepository, "read_scope_missing_history", changed)
    with pytest.raises(HistorySourceChangedError):
        await prepare_history(
            repository,
            context,
            view_key="a" * 64,
            context_key="b" * 64,
        )


async def test_missing_bodies_are_parameter_bounded_on_real_sqlite(database, monkeypatch):
    ledger, context, _seed = await scene(database, 600)
    async with database.engine.connect() as connection:
        await connection.run_sync(
            lambda sync: sync.connection.dbapi_connection.run_async(
                lambda driver: driver._execute(
                    driver._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 300
                )
            )
        )
        monkeypatch.setattr(
            database, "sessions", async_sessionmaker(connection, expire_on_commit=False)
        )
        with capture(database) as statements:
            version, records = await ledger.read_scope_missing_history(
                context.read_version,
                after_event_id=0,
                through_event_id=context.current_event_id,
                frozen_event_ids=frozenset(),
                current_event_id=context.current_event_id,
            )
    assert version == context.read_version and len(records) == 600
    assert body_rows(statements) == 600 and metadata_rows(statements) == 601


async def test_missing_history_uses_saved_conversation_after_alias_removal(database):
    from yuki.conversation.canonical_db_models import ConversationLegacyAliasModel

    ledger, context, _seed = await scene(database, 16)
    secondary = ConversationScope.private(context.read_version.scope.bot_user_id, "secondary")
    expected = replace(context.read_version, scope=secondary)
    async with database.immediate_session() as writer:
        writer.add(
            ConversationLegacyAliasModel(
                id=str(uuid4()),
                conversation_id=expected.conversation_id,
                scope_key=secondary.key,
                is_primary=0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    async with database.immediate_session() as writer:
        await writer.execute(
            delete(ConversationLegacyAliasModel).where(
                ConversationLegacyAliasModel.scope_key == secondary.key
            )
        )
    with capture(database) as statements:
        version, records = await ledger.read_scope_missing_history(
            expected,
            after_event_id=0,
            through_event_id=context.current_event_id,
            frozen_event_ids=frozenset(),
            current_event_id=context.current_event_id,
        )
    assert version == expected
    assert {row.id for row in records} == context.visible_event_ids - {context.current_event_id}
    assert body_rows(statements) == len(records)


async def test_missing_history_filters_duplicate_ids_and_keeps_explicit_keeper(database):
    ledger, context, _seed = await scene(database, 16)
    duplicate = sorted(context.visible_event_ids)[2]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == duplicate)
            .values(suppression_status="duplicate", utterance_fingerprint="d" * 64)
        )
    # The real suppression trigger advances source revision. Use the new plan's
    # version to test filtering; the preceding tests cover rejecting old plans.
    current_version, _ = await ledger.read_scope_context(context.read_version.scope, limit=20)
    context = replace(context, read_version=current_version)
    version, records = await ledger.read_scope_missing_history(
        context.read_version,
        after_event_id=0,
        through_event_id=context.current_event_id,
        frozen_event_ids=frozenset(),
        current_event_id=context.current_event_id,
    )
    assert version == context.read_version
    assert {record.id for record in records} == context.visible_event_ids - {
        duplicate,
        context.current_event_id,
    }


@pytest.mark.parametrize("change", ["erase", "generation", "privacy"])
async def test_publication_still_rejects_change_after_missing_history_prepare(database, change):
    _ledger, context, _seed = await scene(database, 16)
    version = context.read_version
    repository = PromptProjectionRepository(database)
    frozen = FrozenFragments.load([]).extend_history(
        context.history_fragments, context.history_event_fragments
    )
    publication = await repository.prepare_commit(
        view_key="a" * 64,
        context_key="b" * 64,
        conversation_id=version.conversation_id,
        generation=version.generation,
        expected_source_revision=version.prompt_source_revision,
        starts_after_event_id=version.starts_after_event_id,
        fragments=FrozenFragments.load(list(frozen.items)),
        rebuild_reason="bootstrap",
        selected_summary_text="",
        selected_summary_coverage=0,
    )
    async with database.immediate_session() as writer:
        await publication(writer)
    prepared = await prepare_history(
        repository,
        context,
        view_key="a" * 64,
        context_key="b" * 64,
    )
    publication = await prepared.prepare_commit(
        prepared.fragments.append_current(context.current_event_id, context.current_message)
    )
    async with database.immediate_session() as writer:
        if change == "privacy":
            from sqlalchemy import text

            await writer.execute(
                text("INSERT INTO execution_trace_state(id,privacy_generation) VALUES(1,1)")
            )
        else:
            if change == "erase":
                await writer.execute(
                    delete(ChatEventModel).where(
                        ChatEventModel.id == sorted(context.visible_event_ids)[1]
                    )
                )
                values = {"prompt_source_revision": version.prompt_source_revision + 1}
            else:
                values = {"generation": version.generation + 1}
            await writer.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == version.conversation_id)
                .values(values)
            )
    with pytest.raises(ProjectionConflict):
        async with database.immediate_session() as writer:
            await publication(writer)


async def test_projection_uses_privacy_from_its_source_snapshot(database, monkeypatch):
    _ledger, context, _seed = await scene(database, 16)
    version = context.read_version
    repository = PromptProjectionRepository(database)
    frozen = FrozenFragments.load([]).extend_history(
        context.history_fragments, context.history_event_fragments
    )
    execute = AsyncSession.execute
    changed = False

    async def change_before_snapshot(session, statement, *args, **kwargs):
        nonlocal changed
        if str(statement) == "BEGIN" and not changed:
            changed = True
            async with database.immediate_session() as writer:
                await writer.execute(
                    text("INSERT INTO execution_trace_state(id,privacy_generation) VALUES(1,1)")
                )
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", change_before_snapshot)
    publication = await repository.prepare_commit(
        view_key="a" * 64,
        context_key="b" * 64,
        conversation_id=version.conversation_id,
        generation=version.generation,
        expected_source_revision=version.prompt_source_revision,
        starts_after_event_id=version.starts_after_event_id,
        fragments=FrozenFragments.load(list(frozen.items)),
        rebuild_reason="bootstrap",
    )
    async with database.immediate_session() as writer:
        published = await publication(writer)
    assert changed
    assert published.items() == list(frozen.items)
    restored = await repository.read("a" * 64)
    assert restored._prefix_stamp == published._prefix_stamp
    assert restored.items() == published.items()
