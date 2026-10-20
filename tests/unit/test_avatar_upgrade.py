"""Real operator HTTP/CAS/receipts with lost replies and later user edits."""

import json
from uuid import uuid4

import httpx
import pytest
from tests.unit.test_admin_api import TOKEN
from tests.unit.test_character_avatar import admin as admin_fixture
from tests.unit.test_character_avatar import avatar_env as avatar_fixture
from tests.unit.test_character_avatar import png, upload

from yuki.admin_api.avatar_upgrade import initialize_existing_avatar

admin = admin_fixture
avatar_env = avatar_fixture


class LoseAcceptedReply(httpx.AsyncBaseTransport):
    def __init__(self, original, target):
        self.original = original
        self.target = target
        self.lost = False
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append((request.method, str(request.url)))
        response = await self.original.handle_async_request(request)
        if request.method == "POST" and request.url.path.endswith(self.target) and not self.lost:
            assert response.status_code == 200
            self.lost = True
            raise httpx.ReadError("accepted response lost", request=request)
        return response


async def profile(client):
    return (await client.get("/admin/v1/character")).json()["data"]


@pytest.mark.parametrize("target", ["/avatar/default", "/avatar"])
async def test_original_avatar_requests_resume_then_leave_later_user_choice_alone(
    avatar_env, tmp_path, target
):
    env = avatar_env
    image = tmp_path / "original.png"
    image.write_bytes(png())
    state = tmp_path / "upgrade.json"
    client = env.client
    client.headers["Authorization"] = "Bearer " + TOKEN
    transport = LoseAcceptedReply(client._transport, target)
    client._transport = transport
    original = (await profile(client))["id"]
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await initialize_existing_avatar(client, image, state, original)
    intended = json.loads(state.read_text())
    assert intended["phase"] == ("prepared" if target.endswith("default") else "default_saved")
    assert (await initialize_existing_avatar(client, image, state, original))[
        "status"
    ] == "initialized"
    completed = await profile(client)
    assert completed["avatar_revision"] == 2
    assert (
        completed["avatar"]["sha256"] == completed["default_avatar"]["sha256"] == intended["sha256"]
    )
    for key in ["default", "current"]:
        request = intended[key + "_request"]
        assert any(
            method == "GET" and url.endswith("/operations/" + request)
            for method, url in transport.calls
        )
    changed, _ = await upload(env, png("red"), 2)
    assert changed.status_code == 200
    image.unlink()  # Finished upgrades do not need to reread or reinstall the source asset.
    assert (await initialize_existing_avatar(client, image, state, original))[
        "status"
    ] == "already_initialized"
    assert (await profile(client))["avatar"] == changed.json()["data"]["avatar"]
    assert state.stat().st_mode & 0o777 == 0o600
    assert TOKEN not in state.read_text() and env.first["token"] not in state.read_text()


async def test_avatar_upgrade_rejects_wrong_character_and_existing_customization(
    avatar_env, tmp_path
):
    client = avatar_env.client
    client.headers["Authorization"] = "Bearer " + TOKEN
    image, state = tmp_path / "original.png", tmp_path / "upgrade.json"
    image.write_bytes(png())
    with pytest.raises(ValueError, match="different character"):
        await initialize_existing_avatar(client, image, state, str(uuid4()))
    assert not state.exists()
    original = (await profile(client))["id"]
    changed, _ = await upload(avatar_env, png("red"), 0)
    assert changed.status_code == 200
    with pytest.raises(ValueError, match="already changed"):
        await initialize_existing_avatar(client, image, state, original)
    assert not state.exists()
    assert (await profile(client))["default_avatar"] is None


async def test_user_edit_during_lost_default_reply_is_not_overwritten(avatar_env, tmp_path):
    client = avatar_env.client
    client.headers["Authorization"] = "Bearer " + TOKEN
    client._transport = LoseAcceptedReply(client._transport, "/avatar/default")
    image, state = tmp_path / "original.png", tmp_path / "upgrade.json"
    image.write_bytes(png())
    original = (await profile(client))["id"]
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await initialize_existing_avatar(client, image, state, original)
    custom, _ = await upload(avatar_env, png("red"), 1)
    assert custom.status_code == 200
    with pytest.raises(httpx.HTTPStatusError) as conflict:
        await initialize_existing_avatar(client, image, state, original)
    assert conflict.value.response.status_code == 409
    assert (await profile(client))["avatar"] == custom.json()["data"]["avatar"]
    assert (await profile(client))["avatar_revision"] == 2
    assert json.loads(state.read_text())["phase"] == "default_saved"


async def test_resume_rejects_changed_source_pixels(avatar_env, tmp_path):
    client = avatar_env.client
    client.headers["Authorization"] = "Bearer " + TOKEN
    client._transport = LoseAcceptedReply(client._transport, "/avatar/default")
    image, state = tmp_path / "original.png", tmp_path / "upgrade.json"
    image.write_bytes(png())
    original = (await profile(client))["id"]
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await initialize_existing_avatar(client, image, state, original)
    image.write_bytes(png("red"))
    with pytest.raises(ValueError, match="image changed"):
        await initialize_existing_avatar(client, image, state, original)
    assert (await profile(client))["avatar_revision"] == 1
