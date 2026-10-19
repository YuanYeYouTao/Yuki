"""Public work facts: paging, original execution updates, and bounded active snapshots."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import insert, text, update
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner, seeded

from yuki.client_api.works import ClientWorks
from yuki.runtime.work_schema_v1 import effects, work
from yuki.runtime.work_wait_schema import waits

admin = admin_fixture
app_env = app_fixture


async def test_step_cursor_handles_equal_times_and_never_exposes_internal_arguments(
    app_env, database
):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database)
    async with database.immediate_session() as session:
        for key, state, outcome in [
            ("internal-a", "prepared", {}),
            ("internal-b", "accepted", {"tool": "terminal_exec", "pending": True, "ok": True}),
            ("internal-c", "accepted", {"ok": True, "uncertain": True}),
            ("internal-d", "accepted", {"ok": False}),
            ("internal-e", "accepted", {"ok": True, "status": "completed"}),
            ("internal-f", "accepted", {"ok": True}),
            ("internal-g", "failed", {"ok": False, "uncertain": True}),
        ]:
            await session.execute(
                insert(effects).values(
                    effect_key=key,
                    work_id=original.row["id"],
                    kind="tool",
                    state=state,
                    receipt_json=json.dumps(
                        {
                            "invocation": {
                                "tool_id": "terminal_exec",
                                "arguments": "PRIVATE COMMAND",
                            },
                            "outcome": outcome,
                        }
                    ),
                    created=10.0,
                    updated=11.0,
                )
            )
    path = f"/client/v1/works/{original.row['id']}/steps"
    found, cursor = [], None
    while True:
        page = (
            await env.client.get(
                path,
                params={"limit": 2, **({"after": cursor} if cursor else {})},
                headers=bearer(device["token"]),
            )
        ).json()["data"]
        assert "PRIVATE COMMAND" not in json.dumps(page) and "internal-" not in json.dumps(page)
        found.extend(page["steps"])
        cursor = page["next_after"]
        if cursor is None:
            break
    assert [item["status"] for item in found] == [
        "recorded",
        "running",
        "unknown",
        "failed",
        "succeeded",
        "unknown",
        "failed",
    ]
    assert len({item["step_id"] for item in found}) == 7
    principal = await env.identity.authenticate(device["token"])
    restarted = ClientWorks(env.identity, env.gateway)
    assert (await restarted.steps(principal, original.row["id"], None, 100))["steps"] == found


async def test_late_terminal_receipt_updates_same_step_and_hints_without_reviving_work(
    app_env, database
):
    env = app_env
    device, _ = await owner(env, database)
    principal = await env.identity.authenticate(device["token"])
    original = await seeded(env, database, retain_lease=True)
    key, run_id = str(uuid4()), str(uuid4())
    outcome = {
        "tool": "terminal_exec",
        "run_id": run_id,
        "ok": True,
        "pending": True,
        "status": "running",
        "side_effecting": True,
    }
    await original.repository.prepare_effect(
        original.lease,
        original.row["id"],
        key,
        "tool",
        outcome=outcome,
        invocation={"tool_id": "terminal_exec"},
    )
    await original.repository.record_effect(key, "accepted", {"outcome": outcome})
    async with database.immediate_session() as session:
        await session.execute(
            update(work).where(work.c.id == original.row["id"]).values(state="completed")
        )
    before = await env.files.views.works.get(principal, original.row["id"])
    stream = env.files.views.stream(principal, active=True)
    await anext(stream)
    try:
        await original.repository.resolve_run_effects(
            original.lease,
            original.row["id"],
            run_id,
            {
                "run_id": run_id,
                "status": "completed",
                "ok": True,
                "pending": False,
                "uncertain": False,
                "step_label": "browser",
            },
        )
        async with asyncio.timeout(3):
            while "event: work_steps" not in (await anext(stream)):
                pass
        after = await env.files.views.works.get(principal, original.row["id"])
        assert after["state"] == before["state"] == "completed"
        assert after["steps_token"] != before["steps_token"]
        step = (await env.files.views.works.steps(principal, original.row["id"], None, 100))[
            "steps"
        ][0]
        assert step["status"] == "succeeded"
        assert (step["category"], step["label"]) == ("browser", "使用瀏覽器")
    finally:
        await stream.aclose()
        await original.repository.release(original.lease)


async def test_wait_summary_keeps_all_conditions_and_only_public_run_kind(app_env, database):
    env = app_env
    device, person = await owner(env, database)
    original = await seeded(env, database, state="waiting_external")
    run_id = str(uuid4())
    async with database.immediate_session() as session:
        await session.execute(
            insert(effects).values(
                effect_key=str(uuid4()),
                work_id=original.row["id"],
                kind="tool",
                state="accepted",
                receipt_json=json.dumps(
                    {
                        "invocation": {"tool_id": "terminal_exec"},
                        "outcome": {"run_id": run_id, "ok": True, "pending": True},
                    }
                ),
                created=1,
                updated=1,
            )
        )
        await session.execute(
            insert(waits).values(
                id=str(uuid4()),
                work_id=original.row["id"],
                conversation_id=original.row["conversation_id"],
                generation=1,
                principal_kind="person",
                principal_id=person,
                call_key=str(uuid4()),
                request_json="{}",
                mode="all",
                conditions_json=json.dumps(
                    [
                        {"kind": "owned_run", "run_id": run_id},
                        {"kind": "time_due", "due": 30},
                        {"kind": "conversation"},
                        {
                            "kind": "plugin_event",
                            "plugin_id": "example",
                            "event_type": "finished",
                            "external_source": "private",
                        },
                    ]
                ),
                status="active",
                created=10,
                updated=10,
            )
        )
    response = (
        await env.client.get(
            f"/client/v1/works/{original.row['id']}/steps", headers=bearer(device["token"])
        )
    ).json()["data"]
    summary = response["wait"]
    assert summary["mode"] == "all" and len(summary["conditions"]) == 4
    assert summary["conditions"][0] == {"kind": "owned_run", "run_kind": "terminal"}
    assert run_id not in json.dumps(summary) and "private" not in json.dumps(summary)


async def test_active_home_and_sync_do_not_load_ended_history_and_list_reaches_oldest(
    app_env, database
):
    env = app_env
    device, _ = await owner(env, database)
    principal = await env.identity.authenticate(device["token"])
    active = await seeded(env, database, state="suspended")
    ended = [await seeded(env, database, state="completed") for _ in range(3)]
    async with database.immediate_session() as session:
        await session.execute(
            update(work).where(work.c.id == active.row["id"]).values(reason="owner_paused")
        )
        for index, original in enumerate(ended):
            await session.execute(
                update(work).where(work.c.id == original.row["id"]).values(updated=float(index + 1))
            )
    home = (
        await env.client.get("/client/v1/home?works=active", headers=bearer(device["token"]))
    ).json()["data"]
    assert [item["work_id"] for item in home["works"]] == [active.row["id"]]
    assert home["works"][0]["reason"] == "owner_paused"
    sync = await env.files.views.sync(principal, active=True)
    assert [item["work_id"] for item in sync["works"]] == [active.row["id"]]
    assert "steps_token" in sync["works"][0]
    assert len((await env.files.views.sync(principal))["works"]) == 4
    seen, before = [], None
    while True:
        params = {"group": "ended", "limit": 1, **({"before": before} if before else {})}
        page = (
            await env.client.get("/client/v1/works", params=params, headers=bearer(device["token"]))
        ).json()["data"]
        seen.extend(item["work_id"] for item in page["works"])
        before = page["next_before"]
        if before is None:
            break
    assert seen == [item.row["id"] for item in reversed(ended)]
    status = (
        await env.client.get("/client/v1/character/status", headers=bearer(device["token"]))
    ).json()["data"]
    assert (
        status["environment"]["state"] == "unavailable" and status["environment"]["control"] is None
    )
    assert status["mine"]["works"]["suspended"] == 1


async def test_new_query_indexes_are_used_and_rollback_never_publishes_steps(database):
    async with database.sessions() as session:
        steps = (
            await session.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT effect_key FROM runtime_work_effects "
                    "WHERE work_id='a' AND kind='tool' AND (created,effect_key)>(1,'b') "
                    "ORDER BY created,effect_key LIMIT 50"
                )
            )
        ).all()
        roots = (
            await session.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT id FROM runtime_work "
                    "WHERE parent_work_id IS NULL AND state='running' "
                    "ORDER BY updated DESC,id DESC LIMIT 50"
                )
            )
        ).all()
    assert "ix_runtime_work_tool_steps" in str(steps)
    assert "ix_runtime_work_query_state_updated" in str(roots)
    queue = database.commit_hints.subscribe(details=True)
    try:
        with pytest.raises(RuntimeError):
            async with database.immediate_session() as session:
                session.info.setdefault("work_step_hints", set()).add("not-committed")
                raise RuntimeError("rollback")
        assert queue.empty()
    finally:
        database.commit_hints.unsubscribe(queue)
