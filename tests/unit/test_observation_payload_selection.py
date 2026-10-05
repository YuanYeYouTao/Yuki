"""Payload IO follows selected coverage, while metadata pages remain complete."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text, update
from tests.support.projection_sql_counts import capture_sql
from tests.unit.test_context_observation_sources import summary
from tests.unit.test_projection_selection_delta import chat, scene

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from qq_ai_bot.conversation.observations import ContextObservationRepository, validate_observations
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.mcp.artifact_schema import artifact_refs
from qq_ai_bot.mcp.repository import ToolArtifactRepository


async def test_observation_payloads_exclude_covered_parents_and_unselected_candidates(
    database, tmp_path
):
    env, repository, arguments = await scene(database, tmp_path)
    now = datetime.now(UTC)
    parents = [f"parent-{index:03}" for index in range(270)]
    common = dict(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        version=1,
        created_at=now,
    )
    async with database.immediate_session() as writer:
        for identity in parents:
            writer.add(
                ContextObservationModel(
                    **common,
                    id=identity,
                    source_key="fixture:" + identity,
                    payload_json=json.dumps({"text": "x" * 4096}),
                    parent_sources_json="[]",
                )
            )
        writer.add(
            ContextObservationModel(
                **common,
                id="selected-root",
                source_key="fixture:root",
                payload_json='{"text":"selected summary"}',
                parent_sources_json=json.dumps([[identity, 1] for identity in parents]),
                summary_view_key=arguments["view_key"],
            )
        )
    fragments = FrozenFragments.load([]).append_observation(
        "selected-root", 1, ChatMessage("user", "selected summary")
    )
    await repository.commit(**arguments, items=list(fragments.items), rebuild_reason="bootstrap")
    async with database.immediate_session() as writer:
        writer.add_all(
            [
                ContextObservationModel(
                    **common,
                    id="new-work-note",
                    source_key="work-note:new:1",
                    payload_json='{"text":"new note"}',
                    parent_sources_json="[]",
                ),
                ContextObservationModel(
                    **common,
                    id="unselected-snapshot",
                    source_key="snapshot:unselected",
                    payload_json='{"text":"private candidate"}',
                    parent_sources_json="[]",
                    summary_view_key=arguments["view_key"],
                ),
                ContextObservationModel(
                    **common,
                    id="unselected-paid-summary",
                    source_key="fixture:paid",
                    payload_json='{"text":"paid candidate"}',
                    parent_sources_json='[["new-work-note",1]]',
                    summary_view_key=arguments["view_key"],
                ),
                ContextObservationModel(
                    **common,
                    id="other-view-summary",
                    source_key="fixture:other-view",
                    payload_json='{"text":"other view"}',
                    parent_sources_json="[]",
                    summary_view_key="f" * 64,
                ),
            ]
        )
    with capture_sql(database) as statements:
        rows = await ContextObservationRepository(database).read(
            conversation_id=arguments["conversation_id"],
            generation=1,
            actor_id=env.person,
            read_scope="main",
            view_key=arguments["view_key"],
        )
    assert {row.id for row in rows} == {"selected-root", "new-work-note"}
    metadata = [
        sql
        for sql, _ in statements
        if sql.startswith("SELECT model_context_observations.id,") and "payload_json" not in sql
    ]
    assert len(metadata) >= 3  # Real >256 page plus final empty page, never truncate.
    payload_queries = [
        (sql, params)
        for sql, params in statements
        if sql.startswith("SELECT") and "model_context_observations.payload_json" in sql
    ]
    assert len(payload_queries) == 1
    assert set(payload_queries[0][1]) == {"selected-root", "new-work-note"}
    payload_bytes = sum(len(row.payload_json.encode("utf-8")) for row in rows)
    assert payload_bytes < 100
    print(
        "observation_payload_io",
        {
            "covered_parents": 270,
            "payload_rows": len(rows),
            "payload_bytes": payload_bytes,
            "omitted_parent_payload_bytes": 270
            * len(json.dumps({"text": "x" * 4096}).encode("utf-8")),
            "metadata_pages": len(metadata),
        },
    )


async def test_selected_history_filters_metadata_before_payload_and_preserves_order(
    database, tmp_path
):
    _env, repository, arguments = await scene(database, tmp_path)
    await repository.commit(**arguments, items=list(chat(270).items), rebuild_reason="bootstrap")
    with capture_sql(database) as statements:
        rows = await ContextObservationRepository(database).selected(
            view_key=arguments["view_key"],
            conversation_id=arguments["conversation_id"],
            generation=1,
            actor_id=arguments["actor_id"],
            read_scope="main",
            visible_event_ids=frozenset({269, 270}),
            allowed_observation_ids=frozenset(),
        )
    assert [item["event_ids"] for item in rows] == [[269], [270]]
    payload_queries = [
        (sql, params)
        for sql, params in statements
        if sql.startswith("SELECT") and "model_context_selections.payload_json" in sql
    ]
    assert len(payload_queries) == 1 and len(payload_queries[0][1]) == 2
    async with database.sessions() as reader:
        selected_ids = (
            await reader.scalars(
                select(ContextSelectionModel.id).order_by(ContextSelectionModel.id)
            )
        ).all()
    assert payload_queries[0][1] == tuple(selected_ids[-2:])
    metadata = [
        sql
        for sql, _ in statements
        if sql.startswith("SELECT model_context_selections.id,") and "payload_json" not in sql
    ]
    assert len(metadata) == 3


@pytest.mark.parametrize(
    "change", ["version", "payload", "scope", "root_version", "selection_version"]
)
async def test_changed_or_invalid_selected_root_never_covers_valid_notes(
    database, tmp_path, change
):
    env, repository, arguments = await scene(database, tmp_path)
    common = dict(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        version=1,
        created_at=datetime.now(UTC),
    )
    async with database.immediate_session() as writer:
        for name in ("a", "b"):
            writer.add_all(
                [
                    ContextObservationModel(
                        **common,
                        id="parent-" + name,
                        source_key="work-note:" + name,
                        payload_json=json.dumps({"text": "note-" + name}),
                        parent_sources_json="[]",
                    ),
                    ContextObservationModel(
                        **common,
                        id="root-" + name,
                        source_key="summary:" + name,
                        payload_json=json.dumps({"text": "summary-" + name}),
                        parent_sources_json=json.dumps([["parent-" + name, 1]]),
                        summary_view_key=arguments["view_key"],
                    ),
                ]
            )
    fragments = FrozenFragments.load([])
    for name in ("a", "b"):
        fragments = fragments.append_observation(
            "root-" + name, 1, ChatMessage("user", "summary-" + name)
        )
    await repository.commit(**arguments, items=list(fragments.items), rebuild_reason="bootstrap")
    async with database.immediate_session() as competing:
        competing.add(
            ContextObservationModel(
                **common,
                id="new-note",
                source_key="work-note:new",
                payload_json='{"text":"new note"}',
                parent_sources_json="[]",
            )
        )
        if change == "selection_version":
            await competing.execute(
                update(ContextSelectionModel)
                .where(ContextSelectionModel.observation_sources_json == '[["root-a", 1]]')
                .values(observation_sources_json='[["root-a",99]]')
            )
        else:
            values = {
                "version": {"version": 2},
                "payload": {"payload_json": '{"text":"changed note"}'},
                "scope": {"read_scope": "hidden"},
                "root_version": {"version": 2},
            }[change]
            identity = "root-a" if change == "root_version" else "parent-a"
            await competing.execute(
                update(ContextObservationModel)
                .where(ContextObservationModel.id == identity)
                .values(**values)
            )
    rows = await ContextObservationRepository(database).read(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        view_key=arguments["view_key"],
    )
    expected = {"root-b", "new-note"}
    if change != "scope":
        expected.add("parent-a")
    assert {row.id for row in rows} == expected


@pytest.mark.parametrize("missing", [False, True])
async def test_wide_parent_closure_chunks_real_sqlite_variable_limit(database, tmp_path, missing):
    env, _, arguments = await scene(database, tmp_path)
    common = dict(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        version=1,
        created_at=datetime.now(UTC),
        payload_json='{"text":"note"}',
    )
    identities = [f"wide-{index}" for index in range(640)]
    async with database.immediate_session() as writer:
        for identity in identities:
            writer.add(
                ContextObservationModel(
                    **common,
                    id=identity,
                    source_key=identity,
                    parent_sources_json="[]",
                )
            )
        parents = [*identities, "missing-last"] if missing else identities
        writer.add(
            ContextObservationModel(
                **common,
                id="wide-root",
                source_key="wide-root",
                parent_sources_json=json.dumps([[identity, 1] for identity in parents]),
                summary_view_key=arguments["view_key"],
            )
        )
    async with database.sessions() as reader:
        connection = await reader.connection()
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection
        previous = await driver._execute(
            driver._conn.setlimit,
            sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
            300,
        )
        try:
            await reader.execute(text("BEGIN"))
            with capture_sql(database) as statements:
                result = await validate_observations(
                    reader,
                    arguments["conversation_id"],
                    1,
                    env.person,
                    "main",
                    (("wide-root", 1),),
                )
            assert result is not missing
            queries = [
                (sql, params)
                for sql, params in statements
                if "model_context_observations.id IN" in sql
            ]
            # Root plus every metadata page, including a missing ID on the last
            # page; no artificial truncation or catch of too-many-variables.
            assert len(queries) == 4
            assert max(len(params) for _, params in queries) <= 261
        finally:
            await driver._execute(
                driver._conn.setlimit,
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
                previous,
            )


async def test_consistent_version_cycle_is_not_valid_projection_source(database, tmp_path):
    env, _, arguments = await scene(database, tmp_path)
    common = dict(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        version=1,
        created_at=datetime.now(UTC),
        payload_json='{"text":"corrupt cycle"}',
        summary_view_key=arguments["view_key"],
    )
    # Factories publish a new UUID over existing verified parents and cannot
    # create this graph. Exercise rejection of corrupted persistent metadata.
    async with database.immediate_session() as writer:
        for identity, parent in (("cycle-a", "cycle-b"), ("cycle-b", "cycle-a")):
            writer.add(
                ContextObservationModel(
                    **common,
                    id=identity,
                    source_key=identity,
                    parent_sources_json=json.dumps([[parent, 1]]),
                )
            )
    async with database.sessions() as reader:
        assert not await validate_observations(
            reader,
            arguments["conversation_id"],
            1,
            env.person,
            "main",
            (("cycle-a", 1),),
        )


async def test_paid_summary_wide_parents_uses_real_bounded_queries_and_deduplicated_refs(
    database, tmp_path
):
    env, projections, arguments = await scene(database, tmp_path)
    common = dict(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        version=1,
        created_at=datetime.now(UTC),
        payload_json='{"text":"note"}',
        parent_sources_json="[]",
    )
    identities = [f"summary-parent-{index}" for index in range(640)]
    store = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="search",
        content="retained source",
        media_type="text/plain",
    )
    async with database.immediate_session() as writer:
        writer.add_all(
            [
                ContextObservationModel(**common, id=identity, source_key=identity)
                for identity in identities
            ]
        )
        await writer.execute(
            artifact_refs.insert(),
            [
                dict(owner_kind="observation", owner_id=identity, handle_id=handle)
                for identity in identities
            ],
        )
    repository = ContextObservationRepository(database)
    observations = await repository.read(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        view_key=arguments["view_key"],
    )
    drivers = {}

    def lower_limit(dbapi, _record, _proxy):
        async def lower(driver):
            if id(driver) not in drivers:
                previous = await driver._execute(
                    driver._conn.setlimit,
                    sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
                    300,
                )
                drivers[id(driver)] = (driver, previous)

        dbapi.run_async(lower)

    # Lower every real checked-out connection, including the publication writer.
    event.listen(database.engine.sync_engine, "checkout", lower_limit)
    try:
        with capture_sql(database) as statements:
            paid = await repository.publish_scope_summary(
                view_key=arguments["view_key"],
                observations=observations,
                payload=summary(observations),
                conversation_id=arguments["conversation_id"],
                generation=1,
                actor_id=env.person,
                read_scope="main",
                expected_source_revision=arguments["expected_source_revision"],
            )
        parent_queries = [
            params for sql, params in statements if "tool_artifact_refs.owner_id IN" in sql
        ]
        assert len(parent_queries) == 3 and max(map(len, parent_queries)) <= 257
        inserts = [sql for sql, _ in statements if sql.startswith("INSERT INTO tool_artifact_refs")]
        assert len(inserts) == 1
        async with database.sessions() as reader:
            refs = (
                await reader.execute(select(artifact_refs.c.owner_id, artifact_refs.c.handle_id))
            ).all()
        # Paid candidates retain parent ownership until the selection CAS.
        assert (paid.id, handle) in refs
        assert all((identity, handle) in refs for identity in identities)
        fragments = FrozenFragments.load([]).append_observation(
            paid.id, paid.version, paid.message()
        )
        await projections.commit(
            **arguments, items=list(fragments.items), rebuild_reason="bootstrap"
        )
    finally:
        event.remove(database.engine.sync_engine, "checkout", lower_limit)
        for driver, previous in drivers.values():
            await driver._execute(
                driver._conn.setlimit,
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
                previous,
            )
    async with database.sessions() as reader:
        refs = (
            await reader.execute(select(artifact_refs.c.owner_id, artifact_refs.c.handle_id))
        ).all()
    assert refs == [(paid.id, handle)]
