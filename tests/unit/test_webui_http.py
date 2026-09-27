"""Real ASGI, SQL and trusted credentials; no gateway or paid model calls."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import make_settings
from tests.unit import test_control_automation_history as automation_fixtures
from tests.unit import test_control_work_details as work_fixtures
from tests.unit.test_control_operator_access import operator_file

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.application.modules.control_plane import ControlPlaneBundle
from qq_ai_bot.control_plane.command_service import ControlCommandService
from qq_ai_bot.control_plane.query_service import ControlQueryService
from qq_ai_bot.control_plane.query_types import ControlQueryError
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
    writer = ControlCommandAdapter(database, settings=settings, runtime_config=runtime)
    workspace = WorkspaceStore(tmp_path / "workspace")
    bundle = ControlPlaneBundle(
        ControlOperatorAccess(database, path),
        ControlQueryService(
            ControlQueryAdapter(
                database, settings=settings, workspace=workspace, runtime_config=runtime
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
    metadata = store.write("note.md", b"<script>untrusted</script>")
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
    artifact = store.write("untrusted.html", b"<script>window.attack=1</script>")
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
