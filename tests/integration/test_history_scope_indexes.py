"""History source seeks use migrated indexes without changing original facts."""

import shutil
import sqlite3
from collections import Counter
from contextlib import closing

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.dialects import sqlite
from tests.support.event_seed import seed_scoped_event
from tests.support.social_history_cases import stored_private_entry
from tests.support.social_identity_cases import social_env
from tests.unit.test_migration_0119 import _seed_0118

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ConversationScope
from yuki.identity.db_models import SpaceBindingModel
from yuki.services.history_query import HistorySource, HistoryTarget
from yuki.social.history_sources import history_source_statement

INDEXES = {
    "ix_conversations_space": "space_id",
    "ix_conversations_identity_binding": "identity_binding_id",
    "ix_conversations_space_binding": "space_binding_id",
}


@pytest.fixture(scope="module")
def previous_schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("history-source-upgrade") / "0147.db"
    with pytest.MonkeyPatch.context() as patch:
        _seed_0118(path, patch)
        command.upgrade(Config("alembic.ini"), "0147")
    return path


def test_source_index_upgrade_preserves_all_original_rows_and_schema(
    previous_schema, tmp_path, monkeypatch
):
    path = tmp_path / "scope-indexes.db"
    shutil.copyfile(previous_schema, path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    with closing(sqlite3.connect(path)) as connection:
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        facts = {
            name: Counter(connection.execute(f'SELECT * FROM "{name}"').fetchall())
            for kind, name, _table, _sql in schema
            if kind == "table" and name != "alembic_version"
        }
    command.upgrade(Config("alembic.ini"), "0148")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0148",)
        after = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        assert [row for row in after if row[1] not in INDEXES] == schema
        assert {row[1] for row in after} - {row[1] for row in schema} == INDEXES.keys()
        for table, rows in facts.items():
            assert Counter(connection.execute(f'SELECT * FROM "{table}"').fetchall()) == rows
        for name, column in INDEXES.items():
            assert [row[2] for row in connection.execute(f"PRAGMA index_info({name})")] == [
                column,
                "id",
            ]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("selector", ["space", "personal_binding", "space_binding"])
@pytest.mark.parametrize("projection", ["scope", "display"])
async def test_actual_source_statement_seeks_bound_entries_and_threads(
    database, tmp_path, selector, projection
):
    env = await social_env(database, tmp_path)
    private = await stored_private_entry(env, "telegram")
    for thread in ("radar-north", "radar-south"):
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group("80001", "20001", thread_id=thread),
            platform_message_id=thread,
            sender_user_id="10001",
            direction="inbound",
            content=f"original {thread}",
        )
    async with database.sessions() as session:
        group_binding = await session.scalar(
            select(SpaceBindingModel.id).where(SpaceBindingModel.space_id == env.space)
        )
        if selector == "space":
            target = HistoryTarget(space_id=env.space)
            expected = {"ix_conversations_space"}
            platform = "qq"
        else:
            target = HistoryTarget(
                binding_id=private.binding_id if selector == "personal_binding" else group_binding
            )
            expected = {"ix_conversations_identity_binding", "ix_conversations_space_binding"}
            platform = "telegram" if selector == "personal_binding" else "qq"
        statement = await history_source_statement(
            session,
            context=env.context,
            target=target,
            source=HistorySource(platform=platform),
        )
        if projection == "scope":
            statement = statement.with_only_columns(
                statement.selected_columns.conversation_id, statement.selected_columns.platform
            )
        statement = statement.order_by(CanonicalConversationModel.id).limit(21)
        sql = str(
            statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
        )
        plan = [row[3] for row in await session.execute(text("EXPLAIN QUERY PLAN " + sql))]
        assert not any("SCAN canonical_conversations" in row for row in plan), plan
        assert all(any(name in row for row in plan) for name in expected), plan
        results = (await session.execute(statement)).mappings().all()
        if selector == "personal_binding":
            assert [row["conversation_id"] for row in results] == [
                private.event.canonical_conversation_id
            ]
        else:
            assert len(results) == 3
        assert {row["platform"] for row in results} == {platform}
