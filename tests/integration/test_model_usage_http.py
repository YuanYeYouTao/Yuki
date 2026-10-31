"""Usage failures: wrong window/source, lost history, unknown cache, mixed snapshots,
and revoked management identity. Real TCP HTTP and original SQLite telemetry only.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event
from tests.integration.test_model_connection_http import model_http as model_http
from tests.integration.test_person_authority_http import authority_http as authority_http
from tests.integration.test_person_authority_http import pair
from tests.unit.test_admin_api import admin as admin

from yuki.model_runtime.db_models import ModelInvocationModel

END = datetime(2026, 11, 1, 4, tzinfo=UTC)
PARAMS = {"range": "today", "timezone": "Asia/Taipei", "end": END.isoformat()}


def invocation(**changes):
    return ModelInvocationModel(
        **{
            "task": "chat_agent",
            "profile_id": "flash",
            "provider": "original-provider",
            "model": "original-model",
            "success": True,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_prompt_tokens": 40,
            "latency_seconds": 2.0,
            "created_at": END - timedelta(hours=1),
            **changes,
        }
    )


def artifact(name, value):
    folder = Path("test-results/model-usage")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


async def test_usage_groups_original_sources_and_filters_one_snapshot(model_http, database):
    rows = [
        invocation(),
        invocation(profile_id="flash.legacy-agent", prompt_tokens=200, total_tokens=220),
        invocation(model="previous-model", task="memory_dream", total_tokens=75),
        invocation(provider="another-provider", profile_id="pro", task="planner"),
        invocation(task="web_search", profile_id="connection_opaque"),
        invocation(task="retired_or_unknown", profile_id="deleted-profile"),
    ]
    async with database.immediate_session() as writer:
        writer.add_all(rows)
    statements = []
    parameters = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "model_invocations" in statement.lower():
            statements.append(statement)
            parameters.append(_parameters)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        response = await model_http.client.get("/admin/v1/models/stats", params=PARAMS)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    artifact("original-source-model-groups", data)
    assert len(statements) == 1 and "GROUP BY" in statements[0]
    async with database.read_session() as reader:
        # EXPLAIN is metadata about the one observed query, never a second usage read.
        query_plan = await reader.run_sync(
            lambda session: (
                session.connection()
                .exec_driver_sql("EXPLAIN QUERY PLAN " + statements[0], parameters[0])
                .all()
            )
        )
    artifact("query-plan", {"sql": statements[0], "plan": [list(row) for row in query_plan]})
    assert data["summary"]["invocations"] == 6
    assert data["summary"]["total_tokens"] == 775
    same = next(
        group
        for group in data["groups"]
        if group["provider"] == "original-provider" and group["model"] == "original-model"
    )
    assert same["stats"]["invocations"] == 4
    assert {source["profile_id"] for source in same["sources"]} == {
        "flash",
        "flash.legacy-agent",
        "connection_opaque",
        "deleted-profile",
    }
    assert set(data["options"]["tasks"]) == {
        "chat_agent",
        "memory_dream",
        "planner",
        "web_search",
        "retired_or_unknown",
    }
    filtered = (
        await model_http.client.get(
            "/admin/v1/models/stats",
            params={
                **PARAMS,
                "provider": "another-provider",
                "model": "original-model",
                "group_by": "task",
            },
        )
    ).json()["data"]
    artifact("filtered-original-source", filtered)
    assert filtered["summary"]["invocations"] == 1
    assert filtered["groups"][0]["task"] == "planner"
    assert filtered["groups"][0]["sources"][0]["profile_id"] == "pro"
    assert filtered["options"] == data["options"]
    assert sum(group["stats"]["total_tokens"] for group in filtered["groups"]) == 120
    assert model_http.state["calls"] == []


@pytest.mark.parametrize(
    ("preset", "timezone", "end", "start"),
    [
        ("today", "Asia/Taipei", END, datetime(2026, 10, 31, 16, tzinfo=UTC)),
        ("24h", "Asia/Taipei", END, END - timedelta(hours=24)),
        ("7d", "UTC", END, END - timedelta(days=7)),
        (
            "today",
            "America/New_York",
            datetime(2026, 11, 2, 4, 30, tzinfo=UTC),
            datetime(2026, 11, 1, 4, tzinfo=UTC),
        ),
    ],
    ids=["taipei-today", "rolling-day", "rolling-week", "new-york-dst-day"],
)
async def test_usage_window_includes_start_and_excludes_end(
    model_http, database, preset, timezone, end, start
):
    async with database.immediate_session() as writer:
        writer.add_all(
            [
                invocation(created_at=start - timedelta(microseconds=1)),
                invocation(created_at=start),
                invocation(created_at=end - timedelta(microseconds=1)),
                invocation(created_at=end),
            ]
        )
    response = await model_http.client.get(
        "/admin/v1/models/stats",
        params={"range": preset, "timezone": timezone, "end": end.isoformat()},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    artifact(f"window-{preset}-{timezone.replace('/', '-')}", data)
    assert datetime.fromisoformat(data["range"]["start"]) == start
    assert datetime.fromisoformat(data["range"]["end"]) == end
    assert data["range"]["timezone"] == timezone
    assert data["summary"]["invocations"] == 2


async def test_usage_known_fields_and_cache_ratio_share_reported_samples(model_http, database):
    async with database.immediate_session() as writer:
        writer.add_all(
            [
                invocation(
                    prompt_tokens=100,
                    cached_prompt_tokens=40,
                    total_tokens=120,
                    cache_creation_input_tokens=10,
                    cache_creation_5m_input_tokens=3,
                    cache_creation_1h_input_tokens=7,
                ),
                invocation(prompt_tokens=900, cached_prompt_tokens=None, total_tokens=920),
                invocation(
                    prompt_tokens=None,
                    cached_prompt_tokens=30,
                    total_tokens=None,
                    completion_tokens=7,
                    success=False,
                ),
                invocation(prompt_tokens=0, cached_prompt_tokens=0, total_tokens=0),
                invocation(
                    task="memory_dream",
                    profile_id="cache-only",
                    prompt_tokens=0,
                    cached_prompt_tokens=5,
                    total_tokens=None,
                    completion_tokens=None,
                    cache_creation_input_tokens=12,
                ),
                invocation(
                    task="memory_dream",
                    prompt_tokens=10,
                    cached_prompt_tokens=2,
                    total_tokens=4,
                    physical_request_count=2,
                    unknown_usage_request_count=1,
                ),
            ]
        )
    response = await model_http.client.get("/admin/v1/models/stats", params=PARAMS)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    artifact("known-coverage-cache", data)
    summary = data["summary"]
    assert summary["invocations"] == 6 and summary["failures"] == 1
    assert summary["total_tokens"] == 1044
    assert summary["prompt_tokens"] == 1010 and summary["cached_prompt_tokens"] == 77
    assert summary["cache_creation_input_tokens"] == 22
    assert summary["cache_creation_5m_input_tokens"] == 3
    assert summary["cache_creation_1h_input_tokens"] == 7
    assert summary["coverage"]["cache_creation_input_tokens"] == 2
    assert summary["unknown_usage"] == 3
    assert summary["coverage"]["prompt_tokens"] == 5
    assert summary["coverage"]["cached_prompt_tokens"] == 5
    assert summary["coverage"]["total_tokens"] == 4
    assert summary["cache_read"] == {
        "reported_calls": 4,
        "prompt_tokens": 110,
        "cached_prompt_tokens": 47,
        "ratio": 47 / 110,
    }
    zero = (
        await model_http.client.get(
            "/admin/v1/models/stats",
            params={**PARAMS, "task": "memory_dream", "profile_id": "absent"},
        )
    ).json()["data"]
    assert zero["summary"]["invocations"] == 0
    assert zero["summary"]["cache_read"]["ratio"] is None
    assert zero["summary"]["coverage"]["prompt_tokens"] == 0
    assert zero["groups"] == []
    cache_only = (
        await model_http.client.get(
            "/admin/v1/models/stats", params={**PARAMS, "profile_id": "cache-only"}
        )
    ).json()["data"]["summary"]
    assert cache_only["cache_read"] == {
        "reported_calls": 1,
        "prompt_tokens": 0,
        "cached_prompt_tokens": 5,
        "ratio": None,
    }
    assert cache_only["cache_creation_input_tokens"] == 12


async def test_usage_all_retained_and_query_validation(model_http, database):
    async with database.immediate_session() as writer:
        writer.add_all(
            [invocation(created_at=END - timedelta(days=900)), invocation(created_at=END)]
        )
    response = await model_http.client.get(
        "/admin/v1/models/stats", params={**PARAMS, "range": "all"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["range"]["start"] is None
    assert data["summary"]["invocations"] == 1
    artifact("all-retained", data)
    script = """
import asyncio, json, sys
from yuki.persistence.database import Database
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.model_runtime.usage import ModelUsageQuery
async def main():
    database = Database(sys.argv[1])
    try:
        report = await ModelInvocationRepository(database).usage(
            ModelUsageQuery.model_validate_json(sys.argv[2]))
        print(report.model_dump_json())
    finally:
        await database.close()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        database.url,
        json.dumps({**PARAMS, "range": "all"}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    reopened = json.loads(stdout)
    artifact("new-process-retained", reopened)
    assert reopened == data
    cli = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import asyncio,sys; from tests.conftest import make_settings; "
        "from yuki.cli import _model_stats; asyncio.run(_model_stats(make_settings(sys.argv[1])))",
        database.url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await cli.communicate()
    assert cli.returncode == 0, stderr.decode()
    cli_stats = json.loads(stdout)
    assert cli_stats["all"]["invocations"] == cli_stats["chat_agent"]["invocations"] == 2
    assert cli_stats["memory_dream"]["invocations"] == 0
    artifact("cli-original-output", cli_stats)
    for params in (
        {},
        {**PARAMS, "range": "month"},
        {**PARAMS, "timezone": "Unknown/Nowhere"},
        {**PARAMS, "end": "2026-11-01T04:00:00"},
        {**PARAMS, "group_by": "profile"},
    ):
        rejected = await model_http.client.get("/admin/v1/models/stats", params=params)
        assert rejected.status_code == 422, rejected.text


async def test_usage_requires_current_person_and_device_authority(authority_http):
    client, state, _root = authority_http
    admin, device = await pair(client, state["owner_pairing_code"])
    ordinary, _ = await pair(client, state["nonowner_pairing_code"])
    route = "admin/v1/models/stats"
    allowed = await client.get(route, headers=admin, params=PARAMS)
    assert allowed.status_code == 200 and allowed.json()["data"]["groups"] == []
    assert (await client.get(route, headers=ordinary, params=PARAMS)).status_code == 403
    await client.post(
        "__test/superuser", json={"person_id": state["owner_person_id"], "enabled": False}
    )
    assert (await client.get(route, headers=admin, params=PARAMS)).status_code == 403
    await client.post(
        "__test/superuser", json={"person_id": state["owner_person_id"], "enabled": True}
    )
    await client.post("__test/revoke-device", json={"device_id": device})
    assert (await client.get(route, headers=admin, params=PARAMS)).status_code == 401
    artifact(
        "current-authority",
        {
            "allowed": 200,
            "ordinary": 403,
            "revoked_person": 403,
            "revoked_device": 401,
            "device_id": device,
        },
    )
