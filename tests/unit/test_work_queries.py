"""Scoped terminal metadata remains readable without taking the SQLite writer."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import insert, text, tuple_, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_queries import WorkQueries
from qq_ai_bot.runtime.work_query_schema import SOURCE_SCOPE_FIELDS, work_statuses
from qq_ai_bot.runtime.work_repository import WorkRepository, bounded_json
from qq_ai_bot.runtime.work_schema_v1 import work

SOURCE = {"actor_user_id": "10001", "origin": "user_message"}


async def setup_work(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    item = await repository.accept(
        lease, source_key="original", source={**SOURCE, "actor_person_id": env.person}, goal="test"
    )
    item = await repository.transition(
        lease, item["id"], item["revision"], "completed", reason="test_completed"
    )
    return repository, lease, item


@pytest.mark.asyncio
async def test_terminal_queries_are_read_only_with_writer_held(database, tmp_path):
    repository, lease, item = await setup_work(database, tmp_path)
    queries = WorkQueries(repository)
    # A competing writer is held throughout both queries. A hidden writer
    # acquisition would wait for SQLite's busy timeout and fail this deadline.
    async with database.immediate_session():
        found, recent = await asyncio.wait_for(
            asyncio.gather(
                queries.get(lease, SOURCE, item["id"]),
                queries.list(lease, SOURCE, limit=1, status="terminal"),
            ),
            timeout=1,
        )
    assert found == recent["works"][0]
    assert found["work_id"] == item["id"]
    assert found["state"] == "completed"
    assert found["revision"] == item["revision"]
    assert found["reason"] == "test_completed"
    assert {"model_requests", "tool_calls", "sent_messages"} <= found.keys()
    assert not {"source_json", "checkpoint_json", "payload_json"} & found.keys()
    assert await repository.get(item["id"]) == item


@pytest.mark.asyncio
@pytest.mark.parametrize("field", SOURCE_SCOPE_FIELDS)
async def test_recent_is_local_but_directory_is_owner_independent(database, tmp_path, field):
    repository, lease, item = await setup_work(database, tmp_path)
    queries = WorkQueries(repository)
    other_source = {**json.loads(item["source_json"]), field: "other-owner"}
    assert await queries.recent(lease, other_source) is None
    assert (await queries.get(lease, other_source, item["id"]))["work_id"] == item["id"]
    assert (await queries.list(lease, other_source, status="terminal"))["works"][0][
        "work_id"
    ] == item["id"]


@pytest.mark.asyncio
async def test_list_filters_scope_before_limit_and_uses_exact_index(database, tmp_path):
    repository, lease, item = await setup_work(database, tmp_path)
    # More recent foreign-source records must not push the matching completed
    # item out of a candidate page before authorization is applied.
    async with database.immediate_session() as session:
        await session.execute(
            insert(work),
            [
                {
                    **item,
                    "id": str(uuid4()),
                    "source_key": f"other:{i}",
                    "source_json": bounded_json({**SOURCE, "delegation_id": f"other:{i}"}),
                    "updated": item["updated"] + i + 1,
                }
                for i in range(24)
            ],
        )
    queries = WorkQueries(repository)
    local_source = json.loads(item["source_json"])
    assert (await queries.recent(lease, local_source))["work_id"] == item["id"]
    statement = (
        queries._query(lease, local_source, local=True)
        .order_by(work.c.updated.desc(), work.c.id.desc())
        .limit(1)
    )
    compiled = statement.compile(
        database.engine.sync_engine, compile_kwargs={"literal_binds": True}
    )
    async with database.sessions() as session:
        plan = (await session.execute(text("EXPLAIN QUERY PLAN " + str(compiled)))).all()
    details = "\n".join(str(row[-1]) for row in plan)
    assert "ix_runtime_work_query_scope_updated" in details
    assert "USE TEMP B-TREE" not in details
    assert "SCAN runtime_work" not in details


@pytest.mark.asyncio
async def test_generation_reset_and_child_ownership_restrict_queries(database, tmp_path):
    repository, lease, root = await setup_work(database, tmp_path)
    child_id = str(uuid4())
    async with database.immediate_session() as session:
        await session.execute(
            insert(work).values(**{**root, "id": child_id, "source_key": "child"})
        )
        await session.execute(
            insert(children).values(
                work_id=child_id, root_id=root["id"], source_key="child", brief_json="{}"
            )
        )
    queries = WorkQueries(repository)
    child_lease = replace(lease, work_id=child_id)
    child_source = json.loads(root["source_json"])
    assert await queries.get(lease, SOURCE, child_id) is None
    assert [
        row["work_id"] for row in (await queries.list(lease, SOURCE, status="all"))["works"]
    ] == [root["id"]]
    assert await queries.get(child_lease, child_source, root["id"]) is None
    assert [
        row["work_id"]
        for row in (await queries.list(child_lease, child_source, status="all"))["works"]
    ] == [child_id]
    assert await queries.recent(replace(lease, conversation_id="another"), SOURCE) is None
    assert await queries.recent(replace(lease, generation=2), SOURCE) is None
    async with database.immediate_session() as session:
        await session.execute(
            update(children).where(children.c.work_id == child_id).values(archived_at=1)
        )
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == lease.conversation_id)
            .values(generation=2)
        )
    assert await queries.get(child_lease, child_source, child_id) is None
    assert await queries.recent(lease, SOURCE) is None
    assert (await queries.get(lease, SOURCE, root["id"]))["work_id"] == root["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 51, True, "1"])
async def test_work_query_limit_is_bounded(database, tmp_path, limit):
    repository, lease, _ = await setup_work(database, tmp_path)
    with pytest.raises(ValueError, match="invalid_work_query_limit"):
        await WorkQueries(repository).list(lease, SOURCE, limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "terminal", "all"])
async def test_global_directory_order_is_indexed_and_cursor_is_bounded(database, tmp_path, status):
    repository, lease, item = await setup_work(database, tmp_path)
    identities = [str(uuid4()) for _ in range(4)]
    async with database.immediate_session() as session:
        await session.execute(
            insert(work),
            [
                {
                    **item,
                    "id": identity,
                    "source_key": f"global:{i}",
                    "source_json": bounded_json({"actor_user_id": "other-owner"}),
                    "state": "queued" if i < 2 else "failed",
                    "updated": item["updated"] + i + 1,
                }
                for i, identity in enumerate(identities)
            ],
        )
    queries = WorkQueries(repository)
    first = await queries.list(lease, SOURCE, limit=1, status=status)
    second = await queries.list(lease, SOURCE, limit=1, status=status, cursor=first["next_cursor"])
    assert first["next_cursor"]
    assert first["works"][0]["work_id"] != second["works"][0]["work_id"]
    expected = identities[1::-1] if status == "active" else identities[:1:-1]
    assert [first["works"][0]["work_id"], second["works"][0]["work_id"]] == expected
    candidates = (None,) if status == "all" else work_statuses(status)
    for state in candidates:
        statement = queries._query(lease, SOURCE)
        if state is not None:
            statement = statement.where(work.c.state == state)
        # Include continuation pages: their keyset must also seek the same
        # ordering index instead of building or scanning an offset page.
        statement = statement.where(
            tuple_(work.c.updated, work.c.id) < (item["updated"] + 4, identities[-1])
        )
        statement = statement.order_by(work.c.updated.desc(), work.c.id.desc()).limit(2)
        compiled = statement.compile(
            database.engine.sync_engine, compile_kwargs={"literal_binds": True}
        )
        async with database.sessions() as session:
            plan = (await session.execute(text("EXPLAIN QUERY PLAN " + str(compiled)))).all()
        details = "\n".join(str(row[-1]) for row in plan)
        expected_index = (
            f"ix_runtime_work_query_{'updated' if status == 'all' else 'state_updated'}"
        )
        assert expected_index in details, details
        assert "USE TEMP B-TREE" not in details, details
        assert "SCAN runtime_work\n" not in details, details


@pytest.mark.asyncio
async def test_creator_projection_preserves_canonical_owner_on_binding_change(database, tmp_path):
    repository, lease, item = await setup_work(database, tmp_path)
    queries = WorkQueries(repository)
    async with database.immediate_session() as session:
        await session.execute(
            update(IdentityBindingModel)
            .where(IdentityBindingModel.external_account_id == SOURCE["actor_user_id"])
            .values(display_name="Original creator")
        )
    original = await queries.get(lease, SOURCE, item["id"])
    assert original["creator_display_name"] == "Original creator"
    original_person = original["creator_person_id"]
    async with database.immediate_session() as session:
        other_person = await ensure_person(session, "another-account")
        await session.execute(
            update(IdentityBindingModel)
            .where(IdentityBindingModel.external_account_id == SOURCE["actor_user_id"])
            .values(person_id=other_person, display_name="Another person")
        )
    changed = await queries.get(lease, SOURCE, item["id"])
    assert changed["creator_person_id"] == original_person
    assert changed["creator_display_name"] is None


@pytest.mark.asyncio
async def test_self_creator_is_not_presented_as_unknown_person(database, tmp_path):
    repository, lease, item = await setup_work(database, tmp_path)
    queries = WorkQueries(repository)
    async with database.immediate_session() as session:
        await session.execute(
            update(work)
            .where(work.c.id == item["id"])
            .values(source_json=bounded_json({"principal_kind": "self", "actor_user_id": ""}))
        )
    found = await queries.get(lease, SOURCE, item["id"])
    assert found["creator_kind"] == "self"
    assert found["creator_display_name"] == "Yuki / SELF"
    assert found["creator_person_id"] is None
    async with database.immediate_session() as session:
        await session.execute(update(work).where(work.c.id == item["id"]).values(source_json="{}"))
    assert (await queries.get(lease, SOURCE, item["id"]))["creator_kind"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["bad", "nan:" + str(uuid4()), "-1:" + str(uuid4()), "x" * 257])
async def test_work_query_rejects_invalid_cursors(database, tmp_path, cursor):
    repository, lease, _ = await setup_work(database, tmp_path)
    with pytest.raises(ValueError, match="invalid_work_query_cursor"):
        await WorkQueries(repository).list(lease, SOURCE, cursor=cursor)
