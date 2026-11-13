"""Platform credentials: one webui-config document, hot apply through admin receipts.

The real container, admin HTTP API, Telegram/Feishu gateways and SQLite run against
offline platform fakes. Each scenario is a failure case written before the feature.
"""

import asyncio
import inspect
import json
import logging
import stat
import sys
import tomllib
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles
from tests.unit.test_admin_operator_access import operator_file
from tests.unit.test_sticker_library import LocalDirectoryBridge
from websockets.asyncio.server import serve

from yuki import cli
from yuki.admin_api.http import attach_admin_api
from yuki.application.modules.admin_api import build_admin_api
from yuki.client_api import snowluma_display
from yuki.container import ApplicationContainer
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.identity.db_models import AccountAssignmentModel, PresenceModel
from yuki.persistence.models import ChatEventModel

TOKEN_A = "80001:AAAA-original-token-secret"
TOKEN_B = "80001:BBBB-rotated-token-secret"
TOKEN_WRONG = "80001:WRONG-token-secret"
FEISHU_SECRET = "feishu-app-secret-value"
QQ_SECRET = "qq-official-app-secret-value"
SECRETS = (TOKEN_A, TOKEN_B, TOKEN_WRONG, FEISHU_SECRET, QQ_SECRET)
OPERATOR = "platform-operator-" + "c" * 40
CAPABILITIES = (
    "control.health.read",
    "control.operation.read",
    "control.config.file.content.read",
    "control.config.file.mutate",
)


class FakePlatforms:
    """Telegram Bot API and Feishu OpenAPI, keyed only by what each request carries."""

    def __init__(self, websocket_url: str) -> None:
        self.telegram_bots = {TOKEN_A: 80001, TOKEN_B: 80001}
        self.telegram_calls: list[tuple[str, str]] = []
        self.pending_updates: list[dict[str, object]] = []
        self.feishu_secrets = {"cli_feishu": FEISHU_SECRET}
        self.feishu_endpoint_rejects = False
        self.websocket_url = websocket_url

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.telegram.org":
            _, credential, method = request.url.path.split("/", 2)
            token = credential.removeprefix("bot")
            self.telegram_calls.append((token, method))
            if token not in self.telegram_bots:
                return httpx.Response(
                    401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
                )
            if method == "getMe":
                result: object = {
                    "id": self.telegram_bots[token],
                    "is_bot": True,
                    "username": "yuki_platform_test",
                }
            elif method == "getUpdates":
                await asyncio.sleep(0.02)
                result, self.pending_updates = self.pending_updates, []
            else:
                raise AssertionError(method)
            return httpx.Response(200, json={"ok": True, "result": result})
        if request.url.host in {"open.feishu.cn", "open.larksuite.com"}:
            body = json.loads(request.content) if request.content else {}
            if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
                if self.feishu_secrets.get(body.get("app_id")) != body.get("app_secret"):
                    return httpx.Response(200, json={"code": 10014, "msg": "invalid secret"})
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "t-offline", "expire": 7200}
                )
            if request.url.path == "/open-apis/bot/v3/info":
                return httpx.Response(
                    200, json={"code": 0, "bot": {"open_id": "ou_yuki", "app_name": "Yuki"}}
                )
            if request.url.path == "/callback/ws/endpoint":
                if self.feishu_endpoint_rejects:
                    return httpx.Response(200, json={"code": 1000040344, "msg": "denied"})
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"URL": self.websocket_url + "/?service_id=1", "ClientConfig": {}},
                    },
                )
        raise AssertionError(f"unexpected upstream {request.url.host}{request.url.path}")


async def eventually(check) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0

    async def reached() -> bool:
        result = check()
        return bool(await result if inspect.isawaitable(result) else result)

    while not await reached():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.02)


def command(revision: int, platform: str, **fields: object) -> dict[str, object]:
    return {
        "request_id": str(uuid4()),
        "expected_revision": revision,
        "platform": platform,
        **fields,
    }


@pytest.fixture
def platform_world(tmp_path, monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", OPERATOR)
    operators, _ = operator_file(tmp_path, capabilities=CAPABILITIES)
    upstream_holder: dict[str, FakePlatforms] = {}
    original = httpx.AsyncClient

    class Routed(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(upstream_holder["fake"]))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Routed)
    return SimpleNamespace(operators=operators, upstream=upstream_holder, original=original)


async def start_stack(database, tmp_path, world, platforms_file: Path, monkeypatch):
    settings = make_settings(
        database.url,
        platforms_file=platforms_file,
        control_operators_file=world.operators,
        model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
        onebot_access_token="",
        plugin_system_enabled=False,
        plugin_directory=tmp_path / "plugins",
        workspace_directory=tmp_path / "artifacts",
        conversation_media_cache_directory=tmp_path / "conversation-media",
        social_transfer_directory=tmp_path / "transfer",
        semantic_participation_state_path=tmp_path / "participation.sqlite3",
        memory_embedding_enabled=False,
        memory_embedding_worker_enabled=False,
        memory_self_reflection_enabled=False,
        memory_dream_enabled=False,
        memory_maintenance_enabled=False,
        conversation_proactive_wakeup_enabled=False,
        conversation_autonomous_enabled=False,
    )
    app = ApplicationContainer(settings, database=database)
    await app.character.initialize("Platform fixture")
    bridge = LocalDirectoryBridge(
        tmp_path / "manager-workspace",
        app.workspace_service.store,
        cache_root=app.conversation_media.root.parent,
    )

    async def manager_read_or_file(method, arguments, *, request_id, **_kwargs):
        if method == "list_code_completions":
            return {"events": [], "next_cursor": 0, "has_more": False}
        return await bridge.execute(method, arguments, request_id=request_id)

    monkeypatch.setattr(app.sandbox_client, "execute", manager_read_or_file)
    await app.start()
    http = FastAPI()
    attach_admin_api(http, lambda: build_admin_api(app))
    client = world.original(
        transport=httpx.ASGITransport(app=http),
        base_url="http://admin.test",
        headers={"Authorization": f"Bearer {OPERATOR}"},
    )
    return app, client


@pytest.fixture
async def websocket_url():
    async def hold(connection):
        await connection.wait_closed()

    async with serve(hold, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


class Transcript:
    """Everything a reader, log or file shows; secrets must appear in none of it."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def keep(self, response: httpx.Response) -> httpx.Response:
        self.texts.append(response.text)
        return response

    async def assert_clean(self, caplog, database, platforms_file: Path) -> None:
        document = await asyncio.to_thread(platforms_file.read_text, "utf-8")
        exposed = [*self.texts, caplog.text, document]
        async with database.read_session() as session:
            rows = (await session.execute(select(ControlCommandReceiptModel))).scalars().all()
            exposed.extend(json.dumps(row.__dict__, default=str) for row in rows)
        for secret in SECRETS:
            assert all(secret not in text for text in exposed), "secret exposed"


async def platforms(client, transcript) -> dict:
    response = transcript.keep(await client.get("/admin/v1/platforms"))
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def apply(client, transcript, body) -> httpx.Response:
    return transcript.keep(await client.post("/admin/v1/platforms/apply", json=body))


async def rows(database, model) -> list[tuple]:
    async with database.read_session() as session:
        found = (await session.execute(select(model))).scalars().all()
        return sorted(
            tuple((column.key, getattr(row, column.key)) for column in model.__table__.columns)
            for row in found
        )


async def test_upgrade_migration_keeps_env_and_process_credentials_online_and_is_idempotent(
    database, tmp_path, platform_world, websocket_url, monkeypatch, caplog
):
    """Failure 11: old .env/process credentials must reach the document and stay online."""
    caplog.set_level(logging.DEBUG)
    platform_world.upstream["fake"] = upstream = FakePlatforms(websocket_url)
    deployment = tmp_path / "deploy"
    (deployment / "webui-config").mkdir(parents=True)
    (deployment / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN_A}\nTELEGRAM_POLL_INTERVAL_SECONDS=1\n"
        "FEISHU_APP_ID=cli_feishu\nFEISHU_APP_SECRET=\n",
        encoding="utf-8",
    )
    # Compose inlined the secret into the container; it is absent from .env.
    monkeypatch.setenv("FEISHU_APP_SECRET", FEISHU_SECRET)
    monkeypatch.delenv("FEISHU_DOMAIN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    migrate = ["yuki", "setup", "platforms-migrate", "--deployment-root", str(deployment)]
    monkeypatch.setattr(sys, "argv", migrate)
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    document = deployment / "webui-config/platforms.toml"
    secrets = deployment / "webui-config/platforms.secrets.json"
    parsed = tomllib.loads(document.read_text("utf-8"))
    assert parsed["telegram"]["enabled"] is True
    assert parsed["feishu"] == {"enabled": True, "app_id": "cli_feishu"}
    assert "lark" not in parsed
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    before = (document.read_bytes(), secrets.read_bytes())

    (deployment / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN_B}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    assert (document.read_bytes(), secrets.read_bytes()) == before

    app, client = await start_stack(database, tmp_path, platform_world, document, monkeypatch)
    transcript = Transcript()
    try:
        await eventually(
            lambda: (
                app.gateway_registry.has_any_active(platform="telegram")
                and app.gateway_registry.has_any_active(platform="feishu")
            )
        )
        assert (TOKEN_A, "getUpdates") in upstream.telegram_calls
        view = await platforms(client, transcript)
        telegram, feishu = view["platforms"]["telegram"], view["platforms"]["feishu"]
        assert telegram["configured"] and telegram["enabled"]
        assert telegram["saved_secrets"] == ["bot_token"]
        assert telegram["apply_mode"] == "hot"
        assert telegram["health"]["running"] is True
        assert telegram["health"]["accounts"][0]["external_account_id"] == "80001"
        assert feishu["settings"] == {"app_id": "cli_feishu"}
        assert feishu["saved_secrets"] == ["app_secret"]
        assert view["platforms"]["lark"]["configured"] is False
    finally:
        await asyncio.wait_for(app.close(), 10)
        await client.aclose()
    await transcript.assert_clean(caplog, database, document)


async def test_wrong_token_keeps_old_connection_and_same_account_rotation_has_one_instance(
    database, tmp_path, platform_world, websocket_url, monkeypatch, caplog
):
    """Failures 4, 5, 9 and 11: fresh writable save; bad token; same-bot rotation."""
    caplog.set_level(logging.DEBUG)
    platform_world.upstream["fake"] = upstream = FakePlatforms(websocket_url)
    folder = tmp_path / "webui-config"
    folder.mkdir()
    document = folder / "platforms.toml"
    app, client = await start_stack(database, tmp_path, platform_world, document, monkeypatch)
    transcript = Transcript()
    registry = app.gateway_registry
    try:
        view = await platforms(client, transcript)
        assert view["revision"] == 0 and view["writable_directory"] is True
        assert view["platforms"]["telegram"]["configured"] is False
        saved = await apply(
            client,
            transcript,
            command(0, "telegram", enabled=True, secrets={"bot_token": TOKEN_A}),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["effective_state"]["status"] == "applied"
        await eventually(lambda: registry.has_any_active(platform="telegram"))
        original_presences = registry.connected_presence_ids("telegram")
        revision = (await platforms(client, transcript))["revision"]
        secret_bytes = (folder / "platforms.secrets.json").read_bytes()

        rejected = await apply(
            client,
            transcript,
            command(revision, "telegram", enabled=True, secrets={"bot_token": TOKEN_WRONG}),
        )
        assert rejected.status_code == 409
        problem = rejected.json()["problem"]
        assert problem["code"] == "precondition_failed"
        assert problem["details"]["error_category"] == "telegram_rejected"
        assert registry.connected_presence_ids("telegram") == original_presences
        polled = upstream.telegram_calls.count((TOKEN_A, "getUpdates"))
        await eventually(lambda: upstream.telegram_calls.count((TOKEN_A, "getUpdates")) > polled)
        after = await platforms(client, transcript)
        assert after["revision"] == revision
        assert after["platforms"]["telegram"]["health"]["healthy"] is True
        assert (folder / "platforms.secrets.json").read_bytes() == secret_bytes

        rotated = await apply(
            client,
            transcript,
            command(revision, "telegram", enabled=True, secrets={"bot_token": TOKEN_B}),
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["data"]["effective_state"]["status"] == "applied"
        await eventually(lambda: (TOKEN_B, "getUpdates") in upstream.telegram_calls)
        await eventually(lambda: registry.has_any_active(platform="telegram"))
        old_polls = upstream.telegram_calls.count((TOKEN_A, "getUpdates"))
        await asyncio.sleep(0.2)
        assert upstream.telegram_calls.count((TOKEN_A, "getUpdates")) == old_polls
        assert registry.connected_presence_ids("telegram") == original_presences
        health = (await platforms(client, transcript))["platforms"]["telegram"]["health"]
        assert health["healthy"] is True and health["last_error_category"] is None
        assert "GatewayConnectionConflict" not in caplog.text
        rows_ = {row["name"]: row for row in (await client.get("/admin/v1/health")).json()["data"]}
        assert "last_error_category" in rows_["telegram"]
    finally:
        await asyncio.wait_for(app.close(), 10)
        await client.aclose()
    await transcript.assert_clean(caplog, database, document)


async def test_feishu_permission_rejection_recovers_by_applying_again(
    database, tmp_path, platform_world, websocket_url, monkeypatch, caplog
):
    """Failure 6: a stopped PermissionError socket restarts on a later apply."""
    caplog.set_level(logging.DEBUG)
    platform_world.upstream["fake"] = upstream = FakePlatforms(websocket_url)
    upstream.feishu_endpoint_rejects = True
    folder = tmp_path / "webui-config"
    folder.mkdir()
    document = folder / "platforms.toml"
    app, client = await start_stack(database, tmp_path, platform_world, document, monkeypatch)
    transcript = Transcript()
    try:
        partial = await apply(
            client, transcript, command(0, "lark", enabled=False, settings={"app_id": "cli_x"})
        )
        assert partial.status_code == 422
        assert partial.json()["problem"]["code"] == "validation_error"
        orphan = await apply(
            client, transcript, command(0, "lark", enabled=False, secrets={"app_secret": "x" * 8})
        )
        assert orphan.status_code == 422
        saved = await apply(
            client,
            transcript,
            command(
                0,
                "feishu",
                enabled=True,
                settings={"app_id": "cli_feishu"},
                secrets={"app_secret": FEISHU_SECRET},
            ),
        )
        assert saved.status_code == 200, saved.text

        async def stopped() -> bool:
            health = (await platforms(client, transcript))["platforms"]["feishu"]["health"]
            return health["running"] is False and health["last_error_category"] == "PermissionError"

        await eventually(stopped)
        upstream.feishu_endpoint_rejects = False
        revision = (await platforms(client, transcript))["revision"]
        again = await apply(client, transcript, command(revision, "feishu", enabled=True))
        assert again.status_code == 200, again.text
        assert again.json()["data"]["effective_state"]["status"] == "applied"
        await eventually(lambda: app.gateway_registry.has_any_active(platform="feishu"))
        health = (await platforms(client, transcript))["platforms"]["feishu"]["health"]
        assert health["running"] is True and health["healthy"] is True
    finally:
        await asyncio.wait_for(app.close(), 10)
        await client.aclose()
    await transcript.assert_clean(caplog, database, document)


async def test_disable_stops_ingress_and_keeps_history_presence_and_receipts(
    database, tmp_path, platform_world, websocket_url, monkeypatch, caplog
):
    """Failure 7: after disabling nothing is fetched or admitted; facts are not rewritten."""
    caplog.set_level(logging.DEBUG)
    platform_world.upstream["fake"] = upstream = FakePlatforms(websocket_url)
    folder = tmp_path / "webui-config"
    folder.mkdir()
    document = folder / "platforms.toml"
    app, client = await start_stack(database, tmp_path, platform_world, document, monkeypatch)
    transcript = Transcript()
    try:
        saved = await apply(
            client, transcript, command(0, "telegram", enabled=True, secrets={"bot_token": TOKEN_A})
        )
        assert saved.status_code == 200, saved.text
        await eventually(lambda: app.gateway_registry.has_any_active(platform="telegram"))
        presences = await rows(database, PresenceModel)
        assignments = await rows(database, AccountAssignmentModel)
        events = await rows(database, ChatEventModel)
        receipts = await rows(database, ControlCommandReceiptModel)
        revision = (await platforms(client, transcript))["revision"]

        disabled = await apply(client, transcript, command(revision, "telegram", enabled=False))
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["data"]["effective_state"]["status"] == "applied"
        assert not app.gateway_registry.has_any_active(platform="telegram")
        polls = len(upstream.telegram_calls)
        upstream.pending_updates.append(
            {
                "update_id": 7,
                "message": {
                    "message_id": 7,
                    "date": 1792070000,
                    "from": {"id": 42, "first_name": "P", "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "text": "after disable",
                },
            }
        )
        await asyncio.sleep(0.3)
        assert len(upstream.telegram_calls) == polls
        assert await rows(database, ChatEventModel) == events
        assert await rows(database, PresenceModel) == presences
        assert await rows(database, AccountAssignmentModel) == assignments
        after = await rows(database, ControlCommandReceiptModel)
        assert [row for row in after if row in receipts] == receipts
        view = (await platforms(client, transcript))["platforms"]["telegram"]
        assert view["configured"] is True and view["enabled"] is False
        assert view["saved_secrets"] == ["bot_token"]
        assert view["health"]["enabled"] is False and view["health"]["running"] is False
    finally:
        await asyncio.wait_for(app.close(), 10)
        await client.aclose()
    await transcript.assert_clean(caplog, database, document)


async def test_stale_revision_is_rejected_and_restart_only_platforms_say_so(
    database, tmp_path, platform_world, websocket_url, monkeypatch, caplog
):
    """Failures 8, 9 and 10: no lost update; QQ official waits for restart; QQ not savable."""
    caplog.set_level(logging.DEBUG)
    platform_world.upstream["fake"] = FakePlatforms(websocket_url)
    folder = tmp_path / "webui-config"
    folder.mkdir()
    document = folder / "platforms.toml"
    app, client = await start_stack(database, tmp_path, platform_world, document, monkeypatch)
    transcript = Transcript()
    try:
        official = await apply(
            client,
            transcript,
            command(
                0,
                "qq_official",
                enabled=True,
                settings={"app_id": "102000001", "sandbox": True},
                secrets={"app_secret": QQ_SECRET},
            ),
        )
        assert official.status_code == 200, official.text
        assert official.json()["data"]["effective_state"]["status"] == "saved_pending_restart"
        stale = await apply(
            client, transcript, command(0, "telegram", enabled=True, secrets={"bot_token": TOKEN_A})
        )
        assert stale.status_code == 409
        assert stale.json()["problem"]["code"] == "version_conflict"
        assert "telegram" not in tomllib.loads(document.read_text("utf-8"))
        view = await platforms(client, transcript)
        assert view["platforms"]["telegram"]["configured"] is False
        qq_official = view["platforms"]["qq_official"]
        assert qq_official["configured"] and qq_official["apply_mode"] == "restart"
        assert qq_official["settings"] == {"app_id": "102000001", "sandbox": True}
        assert qq_official["saved_secrets"] == ["app_secret"]
        qq = view["platforms"]["qq"]
        assert qq["apply_mode"] == "restart" and qq["savable"] is False
        assert qq["login_available"] is snowluma_display.configured()
        assert "connected" in qq["health"]
        refused = await apply(client, transcript, command(view["revision"], "qq", enabled=True))
        assert refused.status_code == 422
    finally:
        await asyncio.wait_for(app.close(), 10)
        await client.aclose()
    await transcript.assert_clean(caplog, database, document)
