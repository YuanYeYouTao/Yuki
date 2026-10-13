"""Real ASGI, SQL and trusted credentials; no gateway or paid model calls."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import make_settings
from tests.support import control_automation_history_helpers as automation_fixtures
from tests.support import control_memory_query_helpers as memory_fixtures
from tests.support import control_reflection_query_helpers as reflection_fixtures
from tests.support import control_work_details_helpers as work_fixtures
from tests.support.social_identity_cases import social_env
from tests.support.workspace_snapshots import snapshot_bytes
from tests.unit.test_control_operator_access import operator_file

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.application.modules.control_plane import ControlPlaneBundle
from qq_ai_bot.control_plane.command_service import ControlCommandService
from qq_ai_bot.control_plane.query_service import ControlQueryService
from qq_ai_bot.control_plane.query_types import ControlQueryError, DownloadView
from qq_ai_bot.domain.identity import PersonId
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.webui.http import attach_webui
from qq_ai_bot.webui.sessions import BrowserSessions
from qq_ai_bot.workspace.store import WorkspaceStore

ORIGIN = "http://127.0.0.1:18765"
SECRET = "webui-fixture-" + "a" * 48
detailed_work = work_fixtures.detailed_work
histories = automation_fixtures.histories
memory_scene = memory_fixtures.memory_scene
reflection_scene = reflection_fixtures.reflection_scene


@pytest.fixture
async def web(database, tmp_path, monkeypatch):
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", SECRET)
    path, _ = operator_file(
        tmp_path,
        capabilities=(
            "control.system.read",
            "control.operation.read",
            "identity.person.read",
            "identity.person.disable",
            "control.workspace.metadata.read",
            "control.workspace.content.read",
            "control.chat.metadata.read",
            "control.execution.metadata.read",
            "control.work.mutate",
            "control.automation.read",
            "control.memory.metadata.read",
            "control.config.read",
            "control.config.mutate",
        ),
    )
    settings = make_settings(
        database.url,
        webui_enabled=True,
        webui_origin=ORIGIN,
        control_operators_file=path,
        webui_max_body_bytes=1024,
    )
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    from qq_ai_bot.workspace.service import WorkspaceService

    workspace = WorkspaceStore(tmp_path / "workspace")
    workspace_service = WorkspaceService(workspace)
    writer = ControlCommandAdapter(
        database, settings=settings, runtime_config=runtime, workspace_service=workspace_service
    )
    bundle = ControlPlaneBundle(
        ControlOperatorAccess(database, path),
        ControlQueryService(
            ControlQueryAdapter(
                database,
                settings=settings,
                workspace=workspace,
                runtime_config=runtime,
                workspace_service=workspace_service,
            )
        ),
        ControlCommandService(writer),
        writer.recover_interrupted_controls,
    )
    app = FastAPI()

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    attach_webui(app, settings, lambda: bundle)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        yield client, bundle, workspace


async def signed_in(client):
    response = await client.post(
        "/api/control/login", json={"credential": SECRET}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    session = await client.get("/api/control/session")
    assert session.status_code == 200
    assert session.json()["content_access"]["chat"] is False
    return {"Origin": ORIGIN, "X-Yuki-CSRF": session.json()["csrf"]}


async def test_live_execution_http_validates_scope_and_serializes_event_turns(
    web, database, tmp_path
):
    client, _, _ = web
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        from sqlalchemy import select

        from qq_ai_bot.persistence.models import ChatEventModel

        event = await session.scalar(select(ChatEventModel))
        assert event is not None
    headers = await signed_in(client)
    live = await client.post(
        "/api/control/queries/read_conversation_execution",
        headers=headers,
        json={"conversation_id": env.context.conversation_id},
    )
    assert live.status_code == 200, live.text
    assert live.json()["data"]["fields"]["conversation_id"] == env.context.conversation_id
    turns = await client.post(
        "/api/control/queries/list_event_turns",
        headers=headers,
        json={
            "conversation_id": env.context.conversation_id,
            "event_id": event.id,
            "direction": "inbound",
            "page": {"limit": 20, "number": 1},
        },
    )
    assert turns.status_code == 200 and turns.json()["data"]["items"] == []
    from qq_ai_bot.execution_trace.recorder import TraceRecorder, trace_span

    async with trace_span(
        "turn",
        {},
        recorder=TraceRecorder(database),
        conversation_id=env.context.conversation_id,
        source_event_id=event.id,
    ):
        pass
    linked = await client.post(
        "/api/control/queries/list_event_turns",
        headers=headers,
        json={
            "conversation_id": env.context.conversation_id,
            "event_id": event.id,
            "direction": "inbound",
        },
    )
    assert linked.status_code == 200
    fields = linked.json()["data"]["items"][0]["fields"]
    assert fields["original_conversation_id"] == env.context.conversation_id
    assert fields["trace_status"] == "completed"
    invalid = (
        (
            "read_conversation_execution",
            {"conversation_id": env.context.conversation_id, "extra": 1},
        ),
        (
            "list_event_turns",
            {
                "conversation_id": env.context.conversation_id,
                "event_id": True,
                "direction": "inbound",
            },
        ),
        (
            "list_event_turns",
            {
                "conversation_id": env.context.conversation_id,
                "event_id": event.id,
                "direction": "sideways",
            },
        ),
    )
    for method, payload in invalid:
        response = await client.post(
            f"/api/control/queries/{method}", headers=headers, json=payload
        )
        assert response.status_code == 400


async def test_reflection_http_queries(web, reflection_scene):
    client, _, _ = web
    env, runs, _ = reflection_scene
    headers = await signed_in(client)
    response = await client.post(
        "/api/control/queries/read_self_reflection_health", headers=headers, json={}
    )
    assert response.status_code == 200 and "last_24h" in response.json()["data"]["fields"]
    response = await client.post(
        "/api/control/queries/list_self_reflection_history",
        headers=headers,
        json={"section": "runs", "scope": {"space_id": env.space}},
    )
    assert response.status_code == 200 and len(response.json()["data"]["items"]) == 2
    assert "checkpoint_json" not in response.text
    response = await client.post(
        "/api/control/queries/list_self_reflection_history",
        headers=headers,
        json={"section": "requests", "scope": {"run_id": runs[0]}},
    )
    assert (
        response.status_code == 200
        and response.json()["data"]["items"][0]["fields"]["output_tokens"] is None
    )
    for payload in (
        {"section": "checkpoint"},
        {"section": "states", "scope": {"run_id": runs[0]}},
        {"section": "runs", "scope": {"run_id": True}},
        {"section": "runs", "scope": {"person_id": "10001"}},
        {"section": "runs", "scope": []},
    ):
        response = await client.post(
            "/api/control/queries/list_self_reflection_history", headers=headers, json=payload
        )
        assert response.status_code == 400


async def test_memory_scope_http_metadata_and_detail(web, memory_scene):
    client, _, _ = web
    env, ids, source, _ = memory_scene
    headers = await signed_in(client)
    response = await client.post(
        "/api/control/queries/list_memory_facts",
        headers=headers,
        json={"scope": {"person_id": env.person}, "page": {"limit": 30}},
    )
    assert response.status_code == 200
    page = response.json()["data"]
    assert len(page["items"]) == 30 and page["items"][0]["content"] is None
    response = await client.post(
        "/api/control/queries/list_memory_facts",
        headers=headers,
        json={
            "scope": {"person_id": env.person},
            "page": {"limit": 30, "cursor": page["next_cursor"]},
        },
    )
    assert len(response.json()["data"]["items"]) == 3
    numbered = await client.post(
        "/api/control/queries/list_memory_facts",
        headers=headers,
        json={"scope": {"person_id": env.person}, "page": {"limit": 10, "number": 4}},
    )
    assert numbered.status_code == 200
    assert numbered.json()["data"]["total"] == 33
    assert numbered.json()["data"]["number"] == 4
    assert len(numbered.json()["data"]["items"]) == 3
    invalid_page = await client.post(
        "/api/control/queries/list_memory_facts",
        headers=headers,
        json={"scope": {"person_id": env.person}, "page": {"limit": 10, "number": 0}},
    )
    assert invalid_page.status_code == 400
    response = await client.post(
        "/api/control/queries/read_memory_fact", headers=headers, json={"fact_id": ids[0]}
    )
    assert response.status_code == 200
    assert "content" not in response.json()["data"]["fields"]
    response = await client.post(
        "/api/control/queries/list_memory_evidence",
        headers=headers,
        json={"scope": {"fact_id": ids[0], "event_id": source}},
    )
    assert response.status_code == 200
    assert len(response.json()["data"]["items"]) == 1
    assert response.json()["data"]["items"][0]["excerpt"] is None
    for payload in (
        {"scope": {"person_id": "10001"}},
        {"scope": {"platform_message_id": "inbound"}},
        {"scope": {"event_id": True}},
        {"scope": {"event_id": 1, "tool_receipt_id": 1}},
    ):
        response = await client.post(
            "/api/control/queries/list_memory_facts", headers=headers, json=payload
        )
        assert response.status_code == 400
    response = await client.post(
        "/api/control/queries/read_memory_fact", headers=headers, json={"fact_id": 2**63 - 1}
    )
    assert response.status_code == 404


async def test_work_history_and_mutation_use_original_command_receipt(web, detailed_work):
    client, _, _ = web
    identity, _ = detailed_work
    headers = await signed_in(client)
    response = await client.post(
        "/api/control/queries/list_work_history",
        json={"work_id": identity, "section": "inputs", "page": {"limit": 20}},
        headers=headers,
    )
    assert response.status_code == 200
    page = response.json()["data"]
    assert len(page["items"]) == 20 and page["next_cursor"]
    response = await client.post(
        "/api/control/queries/list_work_history",
        json={
            "work_id": identity,
            "section": "inputs",
            "page": {"limit": 20, "cursor": page["next_cursor"]},
        },
        headers=headers,
    )
    assert len(response.json()["data"]["items"]) == 5
    request_id = str(uuid4())
    command = {
        "request_id": request_id,
        "expected_revision": 1,
        "payload": {"resource_id": identity, "action": "cancel"},
    }
    headers["X-Request-ID"] = request_id
    first = await client.post("/api/control/commands/mutate_work", json=command, headers=headers)
    assert (
        first.status_code == 200
        and first.json()["data"]["effective_state"]["status"] == "cancelled"
    )
    assert (
        await client.post("/api/control/commands/mutate_work", json=command, headers=headers)
    ).json() == first.json()
    response = await client.post(
        "/api/control/queries/list_work_history",
        json={"work_id": identity, "section": "source_json"},
        headers=headers,
    )
    assert response.status_code == 400


async def test_automation_history_http_does_not_require_script_content(web, histories):
    client, _, _ = web
    owner, _other, run, foreign = histories
    headers = await signed_in(client)
    for method, args in (
        ("list_automation_runs", {"automation_id": owner}),
        ("list_automation_steps", {"automation_id": owner, "run_id": run}),
    ):
        response = await client.post(
            f"/api/control/queries/{method}", json={**args, "page": {"limit": 10}}, headers=headers
        )
        assert response.status_code == 200
        assert (
            len(response.json()["data"]["items"]) == 10 and response.json()["data"]["next_cursor"]
        )
    response = await client.post(
        "/api/control/queries/list_automation_steps",
        json={"automation_id": owner, "run_id": foreign},
        headers=headers,
    )
    assert response.status_code == 404
    response = await client.post(
        "/api/control/queries/read_automation", json={"automation_id": owner}, headers=headers
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_auth_csrf_origin_and_existing_health_are_separate(web):
    client, _, _ = web
    assert (await client.get("/api/control/session")).status_code == 401
    assert (
        await client.get("/healthz", headers={"Origin": "https://elsewhere.test"})
    ).status_code == 200
    for headers in (
        {},
        {"Origin": "https://evil.test"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
    ):
        response = await client.post(
            "/api/control/login", json={"credential": SECRET}, headers=headers
        )
        assert response.status_code == 403
    headers = await signed_in(client)
    for invalid in (
        {"Origin": ORIGIN},
        {**headers, "Origin": "https://evil.test"},
        {**headers, "X-Yuki-CSRF": "bad"},
    ):
        assert (
            await client.post("/api/control/queries/read_system", json={}, headers=invalid)
        ).status_code == 403
    response = await client.post("/api/control/queries/read_system", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["version"]
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert SECRET not in response.text
    assert (await client.post("/api/control/logout", json={}, headers=headers)).status_code == 200
    assert (await client.get("/api/control/session")).status_code == 401


@pytest.mark.asyncio
async def test_token_rotation_invalidates_existing_session(web, monkeypatch):
    client, _, _ = web
    headers = await signed_in(client)
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", "rotated-" + "b" * 50)
    assert (await client.get("/api/control/session")).status_code == 401
    assert (
        await client.post("/api/control/queries/read_system", json={}, headers=headers)
    ).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,body,status",
    [
        ("list_chat_events", {}, 400),
        ("list_chat_events", {"conversation_id": "platform:123"}, 400),
        ("list_chat_events", {"conversation_id": str(uuid4()), "include_content": True}, 403),
        ("read_execution_trace", {"entry_id": 1}, 403),
        ("read_system", {"principal": "superuser"}, 400),
        ("read_system", {"page": {"limit": 101}}, 400),
        ("database.sql", {}, 404),
        ("execute_tool", {}, 404),
        ("list_chat_events", {"conversation_id": str(uuid4()), "history": {"offset": 1}}, 400),
    ],
)
async def test_http_keeps_permissions_and_strict_contracts(web, method, body, status):
    client, _, _ = web
    headers = await signed_in(client)
    response = await client.post(f"/api/control/queries/{method}", json=body, headers=headers)
    assert response.status_code == status
    assert response.json()["problem"]["code"]


@pytest.mark.asyncio
async def test_body_limit_bad_json_and_unknown_fields_are_controlled(web):
    client, _, _ = web
    headers = {**await signed_in(client), "Content-Type": "application/json"}
    for value in ('{"x":"' + "a" * 1024 + '"}', "{", "[]", "null"):
        response = await client.post(
            "/api/control/queries/read_system", content=value, headers=headers
        )
        assert response.status_code == 400
        assert response.json()["problem"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_real_mutation_uses_original_target_revision_and_receipt(web, database):
    client, _, _ = web
    headers = await signed_in(client)
    person = PersonId.new()
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=person.text,
                enabled=True,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    revision_query = await client.post(
        "/api/control/queries/list_persons", json={}, headers=headers
    )
    revision = revision_query.json()["data"]["items"][0]["revision"]
    request_id = str(uuid4())
    envelope = {
        "request_id": request_id,
        "expected_revision": revision,
        "payload": {},
        "target": {"kind": "person", "id": person.text},
    }
    headers["X-Request-ID"] = request_id
    first = await client.post(
        "/api/control/commands/disable_person", json=envelope, headers=headers
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["success"]
    repeated = await client.post(
        "/api/control/commands/disable_person", json=envelope, headers=headers
    )
    assert repeated.json() == first.json()
    receipt = await client.post(
        "/api/control/queries/read_operation", json={"request_id": request_id}, headers=headers
    )
    assert receipt.status_code == 200 and receipt.json()["data"]["status"] == "succeeded"
    for invalid in (
        {**envelope, "principal": "root"},
        {**envelope, "target": {"kind": "person", "id": "12345"}},
        {**envelope, "request_id": str(uuid4())},
    ):
        response = await client.post(
            "/api/control/commands/disable_person", json=invalid, headers=headers
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_workspace_only_uses_canonical_artifact_ids(web):
    client, _, store = web
    headers = await signed_in(client)
    metadata = snapshot_bytes(store, "note.md", b"<script>untrusted</script>")
    result = await client.post(
        "/api/control/queries/read_workspace",
        json={"artifact_id": metadata["artifact_id"]},
        headers=headers,
    )
    assert result.status_code == 200
    assert result.json()["data"]["fields"]["text"] == "<script>untrusted</script>"
    assert str(store.root) not in result.text
    for invalid in ("../config/system_prompt.md", "C:/secrets", str(uuid4())):
        result = await client.post(
            "/api/control/queries/read_workspace", json={"artifact_id": invalid}, headers=headers
        )
        assert result.status_code in {400, 404}


@pytest.mark.asyncio
async def test_sessions_expire_and_restart_drops_them(web, monkeypatch):
    _, bundle, _ = web
    sessions = BrowserSessions(bundle.access, lifetime=300)
    token, session = await sessions.login(SECRET, "fixture")
    assert SECRET not in repr(session)
    await sessions.resolve(token)
    with pytest.raises(ControlQueryError):
        await BrowserSessions(bundle.access, lifetime=300).resolve(token)
    monkeypatch.setattr("qq_ai_bot.webui.sessions.time.monotonic", lambda: session.expires + 1)
    with pytest.raises(ControlQueryError):
        await sessions.resolve(token)


def test_disabled_webui_has_no_routes():
    app = FastAPI()
    attach_webui(
        app,
        make_settings("sqlite+aiosqlite:///:memory:"),
        lambda: (_ for _ in ()).throw(AssertionError("not called")),
    )
    assert not any("control" in route.path or route.path == "/ui" for route in app.routes)


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.example",
        "https://example.test/path",
        "https://u:p@example.test",
        "javascript:alert(1)",
        "https://example.test:70000",
        "https://example.test:bad",
        "https://example.test:0",
        "https://example.test\\other",
        "https://example.test\n",
    ],
)
def test_public_origin_requires_https_without_path_or_credentials(origin):
    with pytest.raises(ValueError):
        make_settings("sqlite+aiosqlite:///:memory:", webui_origin=origin)


def test_origin_matches_browser_normalization():
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:", webui_origin="https://Example.TEST:443/"
    )
    assert settings.webui_origin == "https://example.test"


@pytest.mark.asyncio
async def test_https_session_cookie_has_secure_host_prefix_and_logout_revokes_it(web):
    _, bundle, _ = web
    origin = "https://example.test"
    app = FastAPI()
    attach_webui(
        app,
        make_settings("sqlite+aiosqlite:///:memory:", webui_enabled=True, webui_origin=origin),
        lambda: bundle,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        response = await client.post(
            "/api/control/login", json={"credential": SECRET}, headers={"Origin": origin}
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert (
            cookie.startswith("__Host-yuki_control=")
            and "; Secure" in cookie
            and "; Path=/" in cookie
        )
        token = client.cookies["__Host-yuki_control"]
        session = (await client.get("/api/control/session")).json()
        assert (
            await client.post(
                "/api/control/logout",
                json={},
                headers={"Origin": origin, "X-Yuki-CSRF": session["csrf"]},
            )
        ).status_code == 200
        client.cookies.set("__Host-yuki_control", token)
        assert (await client.get("/api/control/session")).status_code == 401


@pytest.mark.asyncio
async def test_login_limits_do_not_create_sessions_for_invalid_credentials(web, monkeypatch):
    _, bundle, _ = web
    sessions = BrowserSessions(bundle.access, lifetime=300)
    for _ in range(10):
        with pytest.raises(ControlQueryError):
            await sessions.login("wrong", "same-peer")
    assert not sessions.sessions
    with pytest.raises(ControlQueryError):
        await sessions.login(SECRET, "same-peer")
    last_attempt = sessions.attempts["same-peer"][-1]
    monkeypatch.setattr("qq_ai_bot.webui.sessions.time.monotonic", lambda: last_attempt + 61)
    token, _ = await sessions.login(SECRET, "same-peer")
    await sessions.resolve(token)


@pytest.mark.asyncio
async def test_file_download_never_executes_workspace_html_and_static_assets_are_packaged(web):
    client, _, store = web
    artifact = snapshot_bytes(store, "untrusted.html", b"<script>window.attack=1</script>")
    path = f"/api/control/files/workspace/{artifact['artifact_id']}"
    assert (await client.get(path)).status_code == 401
    await signed_in(client)
    response = await client.get(path)
    assert response.status_code == 200 and response.content == b"<script>window.attack=1</script>"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    for asset in (
        "index.html",
        "static/theme-tokens.css",
        "static/dashboard.css",
        "CRESCENT-GROVE-LICENSE.txt",
    ):
        response = await client.get(f"/ui/{asset}")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_avatar_cache_is_private_and_bound_to_login_cookie(web, monkeypatch):
    client, bundle, _ = web
    owner = str(uuid4())
    path = f"/api/control/files/avatar/person/{owner}"
    assert (await client.get(path)).status_code == 401

    async def avatar(_context, kind, owner_id):
        assert kind == "person" and owner_id == owner
        return DownloadView("avatar", b"\x89PNG\r\n\x1a\nportrait", "image/png")

    monkeypatch.setattr(bundle.queries, "download_avatar", avatar)
    await signed_in(client)
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.headers["vary"] == "Cookie"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("kind", ["rebuild", "dream"])
async def test_operation_pages_read_original_status_without_parsing_private_run_json(
    database, kind
):
    from sqlalchemy import insert
    from tests.support.projection_sql_counts import capture_sql

    from qq_ai_bot.control_plane.operations import OperationKind, OperationStatus
    from qq_ai_bot.control_plane.paging import PageRequest
    from qq_ai_bot.memory.dream.db_models import MemoryDreamRunModel
    from qq_ai_bot.persistence.models import MemoryRebuildRunModel

    now = datetime.now(UTC)
    public_ids = [str(uuid4()) for _ in range(3)]
    model = MemoryRebuildRunModel if kind == "rebuild" else MemoryDreamRunModel
    statuses = (
        ("planned", "commit_paused", "failed")
        if kind == "rebuild"
        else ("running", "partial_failed", "rolled_back")
    )
    private = (
        {
            "selection_json": "private selection not used by operation view",
            "selection_hash": "a" * 64,
            "snapshot_max_event_id": 0,
            "extraction_fingerprint": "b" * 64,
            "plan_statistics_json": "private statistics not used by operation view",
        }
        if kind == "rebuild"
        else {
            "mode": "full",
            "snapshot_max_fact_id": 0,
            "statistics_json": "private statistics not used by operation view",
        }
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(model),
            [
                {
                    **private,
                    "public_id": public_id,
                    "status": statuses[index],
                    "snapshot_created_at": now,
                    "created_at": now + timedelta(seconds=index),
                    "updated_at": now + timedelta(seconds=index),
                    "error_category": "private error details!",
                }
                for index, public_id in enumerate(public_ids)
            ],
        )
    queries = ControlQueryAdapter(database)
    operation_kind = OperationKind(kind)
    with capture_sql(database) as statements:
        first = await queries.list_operations(PageRequest(limit=2), kind=operation_kind)
        tail = await queries.list_operations(
            PageRequest(limit=2, cursor=first.next_cursor), kind=operation_kind
        )
        numbered = await queries.list_operations(
            PageRequest(limit=2, number=1), kind=operation_kind
        )
        all_items = (*first.items, *tail.items)
        individual = [await queries.read_operation(item.operation_id) for item in all_items]
    assert [item.operation_id for item in all_items] == [
        f"{kind}:{public_id}" for public_id in public_ids
    ]
    assert individual == list(all_items)
    assert tail.next_cursor is None and numbered.total == 3 and numbered.number == 1
    assert numbered.items[0] == all_items[-1]
    for item in all_items:
        assert item.error_category == (
            "operation_failed" if item.status is OperationStatus.FAILED else None
        )
    assert not any(
        column in sql
        for sql, _ in statements
        for column in ("selection_json", "statistics_json", "plan_statistics_json")
    )


async def test_config_spec_page_preserves_schema_and_secret_configured_fields(database):
    from sqlalchemy import insert
    from tests.support.projection_sql_counts import capture_sql

    from qq_ai_bot.admin.config_registry import ConfigRegistry
    from qq_ai_bot.control_plane.paging import PageRequest
    from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel

    settings = make_settings(
        database.url, llm_api_key="private-provider-secret", onebot_access_token=""
    )
    queries = ControlQueryAdapter(database, settings=settings)
    keys = ("context.compaction_window_tokens", "llm.api_key", "onebot.access_token")
    specs = [queries._registry.get(key) for key in keys]
    queries._registry = ConfigRegistry(specs)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(RuntimeConfigOverrideModel),
            [
                {
                    "config_key": key,
                    "scope_type": "global",
                    "value_json": "private-unused-value" * 1000,
                    "value_type": "integer",
                    "apply_mode": "hot",
                    "version": 1,
                    "created_at": now,
                    "updated_at": now,
                    "updated_by": "fixture",
                }
                for key in (keys[0], "zz.retired.setting")
            ],
        )
    with capture_sql(database) as statements:
        items = []
        request = PageRequest(limit=1)
        while True:
            page = await queries.list_config_specs(request)
            items.extend(page.items)
            if page.next_cursor is None:
                break
            request = PageRequest(limit=1, cursor=page.next_cursor)
        numbered = await queries.list_config_specs(PageRequest(limit=1, number=2))
    assert [item.key for item in items] == list(keys)
    assert [item.configured for item in items] == [True, True, False]
    assert numbered.items == (items[1],) and numbered.total == 3 and numbered.number == 2
    for item, spec in zip(items, specs, strict=True):
        assert item.minimum == spec.minimum and item.maximum == spec.maximum
        assert item.choices == spec.choices
        assert item.allowed_scopes == tuple(scope.value for scope in spec.allowed_scopes)
        assert item.sensitive == spec.sensitive and item.apply_mode == spec.apply_mode.value
    assert "private-provider-secret" not in repr(items)
    assert not any("value_json" in sql for sql, _ in statements)


async def test_group_config_inheritance_create_conflict_edit_unset_uses_real_http_writer(
    web, database, tmp_path
):
    from tests.unit.test_participation_flags import override

    client, _, _ = web
    env = await social_env(database, tmp_path)
    key = "conversation.auto_reply_enabled"
    await override(database, None, key, False, version=5)
    headers = await signed_in(client)

    async def read():
        response = await client.post(
            "/api/control/queries/list_effective_configs",
            json={"scope": {"space_id": env.space}, "page": {"limit": 100}},
            headers=headers,
        )
        assert response.status_code == 200
        return next(row for row in response.json()["data"]["items"] if row["key"] == key)

    async def command(method, revision, value=None):
        identity = str(uuid4())
        payload = {"key": key, "scope_type": "group", "scope_id": env.space}
        if method == "set_config":
            payload["value"] = value
        return await client.post(
            f"/api/control/commands/{method}",
            json={"request_id": identity, "expected_revision": revision, "payload": payload},
            headers={**headers, "X-Request-ID": identity},
        )

    inherited = await read()
    assert inherited["scope_type"] == "global" and inherited["version"] == 5
    assert inherited["value"] is False
    created = await command("set_config", 0, True)
    assert created.status_code == 200 and created.json()["data"]["success"]
    local = await read()
    assert local["scope_type"] == "group" and local["version"] == 1 and local["value"] is True
    conflict = await command("set_config", 0, False)
    assert conflict.json()["problem"]["code"] == "version_conflict"
    assert await read() == local
    edited = await command("set_config", local["version"], False)
    assert edited.status_code == 200 and edited.json()["data"]["success"]
    local = await read()
    assert local["version"] == 2 and local["value"] is False
    deleted = await command("unset_config", local["version"])
    assert deleted.status_code == 200 and deleted.json()["data"]["success"]
    assert await read() == inherited
