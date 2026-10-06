"""Real SQLite role-aware debt and exact original-run reconciliation."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from tests.support.social_identity_cases import social_env
from tests.unit.test_semantic_participation_host import _event_and_route

from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository, bounded_json
from qq_ai_bot.runtime.work_schema_v1 import effects, scope, work
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository


@pytest_asyncio.fixture
async def owned(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    assert lease
    row = await repo.accept(lease, source_key="lifecycle", source={}, goal="preserve originals")
    return repo, lease, row["id"]


async def receipt(owned, key, outcome, state="accepted"):
    repo, lease, identity = owned
    await repo.prepare_effect(lease, identity, key, "tool", outcome=outcome)
    if state != "prepared":
        await repo.record_effect(key, state, {"outcome": outcome})


async def read_receipt(repo, key):
    async with repo.database.sessions() as reader:
        return dict(
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )


def finished(**changes):
    return {
        "run_id": "original-run",
        "status": "succeeded",
        "pending": False,
        "uncertain": False,
        "ok": True,
        "tool": "terminal_read",
        "side_effecting": False,
        "mutation_committed": False,
        **changes,
    }


@pytest.mark.parametrize("state", ["failed", "cancelled"])
async def test_terminal_child_late_execution_receipt_settles_without_reviving_child(owned, state):
    repo, lease, identity = owned
    workers = SubagentRepository(repo)
    child = await workers.start(
        lease, identity, "late-child", {"goal": "inspect", "output_kind": "answer"}
    )
    child_lease = await workers.acquire(child)
    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await receipt(
        (repo, child_lease, child),
        "child-launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": run_id,
            "pending": True,
            "mutation_committed": True,
        },
    )
    tasks = SandboxTaskRepository(repo.database)
    await tasks.prepare(
        "late-original",
        {"tool": "terminal_exec"},
        {
            "conversation_id": lease.conversation_id,
            "generation": lease.generation,
            "work_id": child,
            "origin": "user_message",
            "actor_user_id": "fixture",
            "trigger_event_id": 1,
        },
    )
    await tasks.receive(
        {
            "request_id": "late-original",
            "run_id": run_id,
            "result": {"run_id": run_id, "status": "succeeded", "pending": False, "exit_code": 0},
        }
    )
    async with repo.database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == child).values(state=state))
    before = await repo.get(child)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "lifecycle", {}, validate)
    control.current = await repo.get(identity)
    assert await control.has_unresolved_effects()
    await control.reconcile_completed_children()
    assert not await control.has_unresolved_effects()
    assert await repo.get(child) == before
    assert (
        json.loads((await read_receipt(repo, "child-launch"))["receipt_json"])["outcome"][
            "mutation_committed"
        ]
        is True
    )


@pytest.mark.parametrize("mismatch", ["request", "effect", "state", "tool", "run"])
async def test_request_resolution_cannot_rebind_an_unrelated_original(owned, mismatch):
    repo, lease, identity = owned
    previous = {
        "tool": "terminal_exec" if mismatch != "tool" else "plugin_write",
        "side_effecting": True,
        "run_id": "different-run" if mismatch == "run" else None,
        "request_id": "original-request",
        "uncertain": True,
        "mutation_committed": None,
    }
    await receipt(
        owned, "original-effect", previous, state="unknown" if mismatch == "state" else "accepted"
    )
    original = await read_receipt(repo, "original-effect")
    request_id = "wrong-request" if mismatch == "request" else "original-request"
    await repo.resolve_run_effects(
        lease,
        identity,
        "original-run",
        finished(request_id=request_id),
        effect_key="wrong-effect" if mismatch == "effect" else "original-effect",
        request_id=request_id,
    )
    assert await read_receipt(repo, "original-effect") == original
    assert await repo.has_unresolved_effects(lease, identity)


@pytest.mark.parametrize("by_request", [False, True])
async def test_completion_query_uses_identity_index_and_small_projection(owned, by_request):
    repo, lease, identity = owned
    tasks = SandboxTaskRepository(repo.database)
    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await tasks.prepare(
        "large-completion",
        {"tool": "terminal_exec"},
        {
            "conversation_id": lease.conversation_id,
            "generation": lease.generation,
            "work_id": identity,
            "origin": "user_message",
            "actor_user_id": "fixture",
            "trigger_event_id": 1,
        },
    )
    await tasks.receive(
        {
            "request_id": "large-completion",
            "run_id": run_id,
            "result": {
                "run_id": run_id,
                "status": "succeeded",
                "pending": False,
                "exit_code": 0,
                "output": "x" * 120000,
            },
        }
    )
    captures = []

    def capture(_conn, _cursor, sql, params, *_args):
        if sql.startswith("SELECT") and "FROM sandbox_task_runs" in sql:
            captures.append((sql, params))

    event.listen(repo.database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await repo.completed_children(
            lease,
            identity,
            [] if by_request else [run_id],
            request_ids=["large-completion"] if by_request else [],
        )
    finally:
        event.remove(repo.database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(result) == 1 and len(json.dumps(result).encode()) < 1024
    assert len(captures) == 1
    sql, params = captures[0]
    assert "json_extract(sandbox_task_runs.completion_json" in sql
    async with repo.database.engine.connect() as connection:
        plan = (await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)).all()
    assert any("SEARCH sandbox_task_runs USING INDEX" in row[-1] for row in plan)
    assert not any("SCAN sandbox_task_runs" in row[-1] for row in plan)


@pytest.mark.parametrize("role", [False, True, None, "missing", 0])
@pytest.mark.parametrize("flag", ["pending", "uncertain"])
async def test_only_explicit_readonly_boolean_is_not_lifecycle_debt(owned, role, flag):
    repo, lease, identity = owned
    outcome = {"tool": "get_code_run", flag: True, "run_id": "external-run"}
    if role != "missing":
        outcome["side_effecting"] = role
    await receipt(owned, "observation", outcome)
    assert await repo.has_unresolved_effects(lease, identity) is (role is not False)
    assert len(await repo.effect_evidence(lease, identity, only_unresolved=True)) == (
        role is not False
    )
    # Presentation retains the real observation; it does not invent execution ownership.
    assert (await repo.effect_evidence(lease, identity))[0][flag] is True


@pytest.mark.parametrize("state", ["prepared", "unknown"])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_readonly_state_keeps_explicit_observation_uncertainty(owned, state, uncertain):
    repo, lease, identity = owned
    await receipt(owned, "read", {"side_effecting": False, "uncertain": uncertain}, state)
    assert not await repo.has_unresolved_effects(lease, identity)
    assert (await repo.effect_evidence(lease, identity))[0]["uncertain"] is uncertain


@pytest.mark.parametrize("committed", [True, False, None, "missing"])
async def test_terminal_read_preserves_original_known_mutation_fact(owned, committed):
    repo, lease, identity = owned
    await receipt(
        owned,
        "launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": "original-run",
            "pending": True,
            **({"mutation_committed": committed} if committed != "missing" else {}),
        },
    )
    await receipt(owned, "poll", finished())
    poll = await read_receipt(repo, "poll")
    await repo.resolve_run_effects(lease, identity, "original-run", finished())
    row = await read_receipt(repo, "launch")
    outcome = json.loads(row["receipt_json"])["outcome"]
    assert outcome["tool"] == "terminal_exec" and outcome["side_effecting"] is True
    assert outcome["mutation_committed"] is (None if committed == "missing" else committed)
    assert not await repo.has_unresolved_effects(lease, identity)
    assert await read_receipt(repo, "poll") == poll


@pytest.mark.parametrize(
    "change",
    [
        {"status": None, "ok": False},
        {"status": "running"},
        {"status": "unknown"},
        {"pending": True},
        {"uncertain": True},
        {"run_id": "other-run"},
    ],
)
async def test_no_negative_or_unrelated_status_can_settle_original(owned, monkeypatch, change):
    repo, lease, identity = owned
    await receipt(
        owned,
        "launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": "original-run",
            "pending": True,
        },
    )
    original = await read_receipt(repo, "launch")

    def forbidden():
        raise AssertionError("rejected preparation acquired writer")

    monkeypatch.setattr(repo.database, "immediate_session", forbidden)
    await repo.resolve_run_effects(lease, identity, "original-run", finished(**change))
    assert await read_receipt(repo, "launch") == original


async def test_terminal_receipt_does_not_remove_unknown_mutating_state(owned):
    repo, lease, identity = owned
    await receipt(
        owned,
        "launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": "original-run",
            "uncertain": True,
        },
        "unknown",
    )
    await repo.resolve_run_effects(lease, identity, "original-run", finished())
    assert (await read_receipt(repo, "launch"))["state"] == "unknown"
    assert await repo.has_unresolved_effects(lease, identity)


@pytest.mark.parametrize("tool", ["terminal_exec", "legacy_tool", "get_code_run"])
async def test_missing_role_only_known_original_mutation_can_be_settled(owned, tool):
    repo, lease, identity = owned
    await receipt(owned, "legacy", {"tool": tool, "run_id": "original-run", "pending": True})
    await repo.resolve_run_effects(lease, identity, "original-run", finished())
    assert await repo.has_unresolved_effects(lease, identity) is (tool != "terminal_exec")


async def test_same_run_identifier_does_not_cross_plugin_mutation_domain(owned):
    repo, lease, identity = owned
    await receipt(
        owned,
        "plugin",
        {
            "tool": "external_write",
            "side_effecting": True,
            "run_id": "original-run",
            "pending": True,
        },
    )
    original = await read_receipt(repo, "plugin")
    await repo.resolve_run_effects(lease, identity, "original-run", finished())
    assert await read_receipt(repo, "plugin") == original
    assert await repo.has_unresolved_effects(lease, identity)


async def test_empty_and_unchanged_resolution_read_while_wal_writer_held(owned):
    repo, lease, identity = owned
    await receipt(owned, "launch", {**finished(), "tool": "terminal_exec", "side_effecting": True})
    statements = []

    def capture(_conn, _cursor, sql, *_args):
        statements.append(sql)

    async with repo.database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == identity).values(reason="uncommitted"))
        event.listen(repo.database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            await asyncio.wait_for(
                repo.resolve_run_effects(lease, identity, "no-match", finished(run_id="no-match")),
                1,
            )
            await asyncio.wait_for(
                repo.resolve_run_effects(lease, identity, "original-run", finished()), 1
            )
        finally:
            event.remove(repo.database.engine.sync_engine, "before_cursor_execute", capture)
    assert not any(
        sql.lstrip().upper().startswith(("UPDATE", "INSERT", "BEGIN IMMEDIATE"))
        for sql in statements
    )


@pytest.mark.parametrize("count", [129, 257])
async def test_exact_run_pages_use_existing_index_and_preserve_other_effects(owned, count):
    repo, lease, identity = owned
    # Fixture insertion is not the measured reconciliation path.
    async with repo.database.immediate_session() as writer:
        await writer.execute(
            effects.insert(),
            [
                dict(
                    effect_key=f"launch-{i:03}",
                    work_id=identity,
                    kind="tool",
                    state="accepted",
                    created=time.time(),
                    updated=time.time(),
                    receipt_json=bounded_json(
                        {
                            "outcome": {
                                "tool": "terminal_exec",
                                "side_effecting": True,
                                "run_id": "original-run",
                                "pending": True,
                            }
                        }
                    ),
                )
                for i in range(count)
            ],
        )
    await receipt(
        owned, "unrelated", {"side_effecting": True, "run_id": "other-run", "pending": True}
    )
    original = await read_receipt(repo, "unrelated")
    captures = []
    writes_per_page = []

    def capture(_conn, _cursor, sql, params, *_args):
        if sql.startswith("BEGIN IMMEDIATE"):
            writes_per_page.append(0)
        elif sql.startswith("UPDATE runtime_work_effects"):
            writes_per_page[-1] += 1
        if sql.lstrip().upper().startswith("SELECT") and "runtime_work_effects.receipt_json" in sql:
            captures.append((sql, params))

    event.listen(repo.database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await repo.resolve_run_effects(lease, identity, "original-run", finished())
    finally:
        event.remove(repo.database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(captures) == (count + 127) // 128 + 1
    assert writes_per_page == [128] * (count // 128) + [count % 128]
    async with repo.database.engine.connect() as connection:
        plan = (
            await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + captures[0][0], captures[0][1])
        ).all()
    assert any("ix_runtime_effects_work_run_id" in str(row) for row in plan)
    facts = await repo.effect_evidence(lease, identity)
    assert sum(not fact.get("pending") for fact in facts) == count
    assert await read_receipt(repo, "unrelated") == original


@pytest.mark.parametrize("boundary", ["owner", "expired", "generation", "foreign_work"])
async def test_resolution_rejects_obsolete_or_foreign_ownership(owned, boundary):
    repo, lease, identity = owned
    await receipt(
        owned, "launch", {"side_effecting": True, "run_id": "original-run", "pending": True}
    )
    original = await read_receipt(repo, "launch")
    if boundary == "foreign_work":
        selected = "missing-work"
    else:
        selected = identity
        async with repo.database.immediate_session() as writer:
            if boundary == "generation":
                await writer.execute(update(work).where(work.c.id == identity).values(generation=2))
            else:
                await writer.execute(
                    update(scope)
                    .where(scope.c.conversation_id == lease.conversation_id)
                    .values(**({"owner": "stolen"} if boundary == "owner" else {"lease_until": 0}))
                )
    with pytest.raises(WorkConflict):
        await repo.resolve_run_effects(lease, selected, "original-run", finished())
    assert await read_receipt(repo, "launch") == original


async def test_receipt_race_does_not_overwrite_newer_fact(owned, monkeypatch):
    repo, lease, identity = owned
    await receipt(
        owned,
        "launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": "original-run",
            "pending": True,
        },
    )
    original_session = repo.database.immediate_session
    newer = bounded_json(
        {
            "outcome": {
                "tool": "terminal_exec",
                "side_effecting": True,
                "run_id": "original-run",
                "uncertain": True,
            }
        }
    )

    @asynccontextmanager
    async def interleaved():
        async with original_session() as race:
            await race.execute(
                update(effects).where(effects.c.effect_key == "launch").values(receipt_json=newer)
            )
        async with original_session() as writer:
            yield writer

    monkeypatch.setattr(repo.database, "immediate_session", interleaved)
    await repo.resolve_run_effects(lease, identity, "original-run", finished())
    assert (await read_receipt(repo, "launch"))["receipt_json"] == newer


async def test_foreign_conversation_effect_cannot_be_settled_by_valid_scope_lease(owned):
    repo, lease, identity = owned
    await receipt(
        owned,
        "launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": "original-run",
            "pending": True,
        },
    )
    foreign = await _event_and_route(
        repo.database, EventLedgerRepository(repo.database), group="2999"
    )
    original = await read_receipt(repo, "launch")
    async with repo.database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == identity)
            .values(conversation_id=foreign.canonical_conversation_id)
        )
    assert await repo.valid(lease)
    with pytest.raises(WorkConflict, match="work_effect_obsolete"):
        await repo.resolve_run_effects(lease, identity, "original-run", finished())
    assert await read_receipt(repo, "launch") == original


async def test_legitimate_root_child_and_child_own_lease_resolution(owned):
    repo, lease, identity = owned
    workers = SubagentRepository(repo)
    child = await workers.start(
        lease, identity, "original-child", {"goal": "inspect", "output_kind": "answer"}
    )
    child_lease = await workers.acquire(child)
    assert child_lease
    child_owned = repo, child_lease, child
    await receipt(
        child_owned,
        "child-launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": "original-run",
            "pending": True,
        },
    )
    # Parent's trusted reconciliation enumerates its registered child's effects.
    assert await repo.has_unresolved_effects(lease, identity)
    await repo.resolve_run_effects(lease, child, "original-run", finished())
    assert not await repo.has_unresolved_effects(lease, identity)
    await receipt(
        child_owned,
        "child-own-launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": "child-own-run",
            "pending": True,
        },
    )
    await repo.resolve_run_effects(
        child_lease, child, "child-own-run", finished(run_id="child-own-run")
    )
    assert not await repo.has_unresolved_effects(child_lease, child)
    with pytest.raises(WorkConflict, match="work_effect_obsolete"):
        await repo.resolve_run_effects(child_lease, identity, "original-run", finished())


async def test_lease_loss_after_preparation_prevents_writer_resolution(owned, monkeypatch):
    repo, lease, identity = owned
    await receipt(
        owned,
        "launch",
        {
            "tool": "terminal_exec",
            "side_effecting": True,
            "run_id": "original-run",
            "pending": True,
        },
    )
    original = await read_receipt(repo, "launch")
    original_session = repo.database.immediate_session

    @asynccontextmanager
    async def interleaved():
        async with original_session() as race:
            await race.execute(
                update(scope)
                .where(scope.c.conversation_id == lease.conversation_id)
                .values(owner="next-owner")
            )
        async with original_session() as writer:
            yield writer

    monkeypatch.setattr(repo.database, "immediate_session", interleaved)
    with pytest.raises(WorkConflict, match="work_activation_obsolete"):
        await repo.resolve_run_effects(lease, identity, "original-run", finished())
    assert await read_receipt(repo, "launch") == original


@pytest.mark.parametrize("failure", [asyncio.CancelledError, OSError])
async def test_interrupted_second_page_retains_exact_unsettled_originals(
    owned, monkeypatch, failure
):
    repo, lease, identity = owned
    original_work = await repo.get(identity)
    async with repo.database.immediate_session() as writer:
        await writer.execute(
            effects.insert(),
            [
                dict(
                    effect_key=f"launch-{i:03}",
                    work_id=identity,
                    kind="tool",
                    state="accepted",
                    created=time.time(),
                    updated=time.time(),
                    receipt_json=bounded_json(
                        {
                            "outcome": {
                                "tool": "terminal_exec",
                                "side_effecting": True,
                                "run_id": "original-run",
                                "pending": True,
                            }
                        }
                    ),
                )
                for i in range(129)
            ],
        )
    original_session = repo.database.immediate_session
    pages = 0

    @asynccontextmanager
    async def interrupted():
        nonlocal pages
        pages += 1
        if pages == 2:
            raise failure()
        async with original_session() as writer:
            yield writer

    monkeypatch.setattr(repo.database, "immediate_session", interrupted)
    with pytest.raises(failure):
        await repo.resolve_run_effects(lease, identity, "original-run", finished())
    facts = await repo.effect_evidence(lease, identity)
    assert len(facts) == 129 and sum(fact.get("pending", False) for fact in facts) == 1
    assert await repo.has_unresolved_effects(lease, identity)
    assert await repo.get(identity) == original_work
    monkeypatch.setattr(repo.database, "immediate_session", original_session)
    await repo.resolve_run_effects(lease, identity, "original-run", finished())
    assert not await repo.has_unresolved_effects(lease, identity)
    assert len(await repo.effect_evidence(lease, identity)) == 129
    assert await repo.get(identity) == original_work
