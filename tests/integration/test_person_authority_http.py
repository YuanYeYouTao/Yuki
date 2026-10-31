"""B-0 failures: paired != superuser, retired grants confer no authority,
and revoking Person authority immediately removes shared management access.

Real TCP HTTP, SQLite, files and device credentials; no model/provider calls.
"""

import asyncio
import hashlib
import io
import json
import os
import sys
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from PIL import Image


@pytest_asyncio.fixture
async def authority_http(tmp_path):
    state_file = tmp_path / "state.json"
    log_file = tmp_path / "server.log"
    with log_file.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.soul_reference_server",
            "--root",
            str(tmp_path / "server"),
            "--state-file",
            str(state_file),
            "--port",
            "0",
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(30):
                while not state_file.exists():
                    if process.returncode is not None:
                        pytest.fail(log_file.read_text(encoding="utf-8")[-6000:])
                    await asyncio.sleep(0.05)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            async with httpx.AsyncClient(base_url=state["base_url"] + "/") as client:
                yield client, state, tmp_path
                assert (await client.post("__test/shutdown")).status_code == 200
                assert await asyncio.wait_for(process.wait(), 10) == 0
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 10)


async def pair(client, code):
    response = await client.post(
        "client/v1/pair", json={"code": code, "device_label": "Authority HTTP"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return {"Authorization": "Bearer " + data["token"]}, data["device_id"]


async def first_sync(client, headers):
    async with client.stream("GET", "client/v1/stream", headers=headers) as stream:
        assert stream.status_code == 200
        async for line in stream.aiter_lines():
            if line.startswith("data:"):
                return json.loads(line.removeprefix("data:"))
    pytest.fail("SSE ended before its original sync")


async def test_current_person_superuser_owns_shared_management_not_retired_grants(authority_http):
    client, state, root = authority_http
    admin, first_device = await pair(client, state["owner_pairing_code"])
    ordinary, _ = await pair(client, state["nonowner_pairing_code"])
    second, second_device = await pair(
        client, (await client.post("__test/link-code")).json()["code"]
    )
    await client.post("__test/retired-owner")
    me = (await client.get("client/v1/me", headers=admin)).json()["data"]
    same_person = (await client.get("client/v1/me", headers=second)).json()["data"]
    visitor = (await client.get("client/v1/me", headers=ordinary)).json()["data"]
    assert me["is_superuser"] is True and same_person["is_superuser"] is True
    assert visitor["is_superuser"] is False
    assert me["person_id"] == same_person["person_id"] == state["owner_person_id"]
    assert first_device != second_device

    profile = (await client.get("client/v1/character", headers=ordinary)).json()["data"]
    assert "viewer" not in profile
    denied_id = str(uuid4())
    denied = await client.patch(
        "client/v1/character",
        headers=ordinary,
        json={"request_id": denied_id, "expected_revision": profile["revision"], "bio": "denied"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["problem"]["code"] == "forbidden"
    assert (await client.get("client/v1/character", headers=ordinary)).json()["data"] == profile
    request_id = str(uuid4())
    payload = {
        "request_id": request_id,
        "expected_revision": profile["revision"],
        "display_name": "Superuser managed Yuki",
    }
    changed = await client.patch("client/v1/character", headers=admin, json=payload)
    assert changed.status_code == 200, changed.text
    replay = await client.patch("client/v1/character", headers=second, json=payload)
    assert replay.status_code == 200 and replay.json()["data"]["replay"] is True
    assert replay.json()["data"]["id"] == profile["id"]
    assert replay.json()["data"]["revision"] == profile["revision"] + 1

    image = io.BytesIO()
    Image.new("RGB", (1024, 1024), "#336699").save(image, format="PNG")
    png = image.getvalue()
    avatar_id = str(uuid4())
    avatar_query = {
        "request_id": avatar_id,
        "expected_revision": profile["avatar_revision"],
        "sha256": hashlib.sha256(png).hexdigest(),
        "size": len(png),
    }
    avatar = await client.put(
        "client/v1/character/avatar", headers=admin, params=avatar_query, content=png
    )
    assert avatar.status_code == 200, avatar.text
    avatar_data = avatar.json()["data"]
    assert (
        await client.get(avatar_data["avatar"]["sizes"]["128"].removeprefix("/"))
    ).status_code == 200
    reset = await client.request(
        "DELETE",
        "client/v1/character/avatar",
        headers=second,
        json={"request_id": str(uuid4()), "expected_revision": avatar_data["avatar_revision"]},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"]["avatar"] is None

    soul = await client.get("client/v1/character/soul", headers=admin)
    assert soul.status_code == 200, soul.text
    original = soul.json()["data"]
    saved_id = str(uuid4())
    saved = await client.put(
        "client/v1/character/soul",
        headers=second,
        json={
            "request_id": saved_id,
            "expected_revision": original["revision"],
            "content": "B-0 SOUL\n",
        },
    )
    assert saved.status_code == 200, saved.text
    assert (root / "server/character/SOUL.md").read_text(encoding="utf-8") == "B-0 SOUL\n"
    assert (
        await client.post(
            "client/v1/character/soul/reload", headers=admin, json={"request_id": str(uuid4())}
        )
    ).status_code == 200
    assert "soul" in (await first_sync(client, admin))["character"]
    visitor_sync = (await first_sync(client, ordinary))["character"]
    assert "soul" not in visitor_sync and "is_owner" not in visitor_sync
    assert (
        await client.get("client/v1/automations?scope=mine", headers=ordinary)
    ).status_code == 200
    assert (await client.get("client/v1/automations?scope=self", headers=admin)).status_code == 200

    await client.post("__test/superuser", json={"person_id": me["person_id"], "enabled": False})
    assert (await client.get("client/v1/me", headers=admin)).json()["data"]["is_superuser"] is False
    denied_routes = []
    for headers in (admin, ordinary):
        for path in (
            "client/v1/character/soul",
            "client/v1/automations?scope=self",
            "client/v1/computer",
            "client/v1/computer/files?path=/",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code == 403, (path, response.text)
            denied_routes.append({"path": path, "status": response.status_code})
        ticket = await client.post("client/v1/computer/display-tickets", headers=headers)
        assert ticket.status_code == 403, ticket.text
        for action in ("wake", "sleep"):
            response = await client.post(
                "client/v1/computer/" + action, headers=headers, json={"request_id": str(uuid4())}
            )
            assert response.status_code == 403, response.text
    assert (root / "server/character/SOUL.md").read_text(encoding="utf-8") == "B-0 SOUL\n"
    assert (await client.get("client/v1/character", headers=ordinary)).json()["data"][
        "id"
    ] == profile["id"]
    (root / "authority-evidence.json").write_text(
        json.dumps(
            {
                "person_id": me["person_id"],
                "devices": [first_device, second_device],
                "character_id": profile["id"],
                "request_id": request_id,
                "avatar_request_id": avatar_id,
                "soul_request_id": saved_id,
                "denied": denied_routes,
                "retired_owner_is_superuser": visitor["is_superuser"],
                "demoted_is_superuser": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
