"""Desktop's isolated subprocess fixture exercises original HTTP services."""

import asyncio
import json
import os
import sys
from urllib.parse import urlparse
from uuid import uuid4

import httpx


async def test_desktop_reference_restarts_same_port_and_keeps_original_device_revoked(tmp_path):
    root, state_file = tmp_path / "server", tmp_path / "state.json"
    log_file = tmp_path / "server.log"
    process = None
    port = 0
    with log_file.open("wb") as log:

        async def start():
            nonlocal process, port
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.support.desktop_reference_server",
                "--root",
                str(root),
                "--state-file",
                str(state_file),
                "--port",
                str(port),
                "--base-path",
                "/role/restart",
                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            async with asyncio.timeout(30):
                async with httpx.AsyncClient() as probe:
                    while True:
                        if process.returncode is not None:
                            raise AssertionError(log_file.read_text(encoding="utf-8")[-2000:])
                        if state_file.exists():
                            state = json.loads(state_file.read_text(encoding="utf-8"))
                            try:
                                ready = await probe.get(state["base_url"] + "/healthz")
                            except httpx.ConnectError:
                                pass
                            else:
                                if ready.status_code == 200:
                                    port = urlparse(state["base_url"]).port
                                    return state
                        await asyncio.sleep(0.05)

        async def stop(client):
            # Keep an actual SSE connection open during server shutdown. This exercises
            # the listener's active-close/TIME_WAIT path, not just a vacant port bind.
            async with client.stream("GET", "client/v1/stream", headers=observer) as stream:
                assert stream.status_code == 200
                assert (await client.post("__test/shutdown")).status_code == 200
                assert await asyncio.wait_for(process.wait(), 10) == 0

        try:
            state = await start()
            async with httpx.AsyncClient(base_url=state["base_url"] + "/") as client:
                paired = await client.post(
                    "client/v1/pair",
                    json={"code": state["owner_pairing_code"], "device_label": "Restart"},
                )
                assert paired.status_code == 200
                owner_device = paired.json()["data"]["device_id"]
                owner = {"Authorization": "Bearer " + paired.json()["data"]["token"]}
                visitor = await client.post(
                    "client/v1/pair",
                    json={"code": state["nonowner_pairing_code"], "device_label": "Observer"},
                )
                assert visitor.status_code == 200
                observer = {"Authorization": "Bearer " + visitor.json()["data"]["token"]}
                me = (await client.get("client/v1/me", headers=owner)).json()["data"]
                profile = (await client.get("client/v1/character", headers=owner)).json()["data"]
                soul = (root / "character" / "SOUL.md").read_bytes()
                assert profile["viewer"]["is_owner"] is True
                await stop(client)

            for revoked in (False, True):
                restarted = await start()
                assert restarted["base_url"] == state["base_url"]
                assert restarted["owner_person_id"] == state["owner_person_id"]
                assert restarted["nonowner_person_id"] == state["nonowner_person_id"]
                async with httpx.AsyncClient(base_url=state["base_url"] + "/") as client:
                    original = await client.get("client/v1/me", headers=owner)
                    if revoked:
                        assert original.status_code == 401
                    else:
                        assert original.json()["data"] == me
                    current = (await client.get("client/v1/character", headers=observer)).json()[
                        "data"
                    ]
                    assert current["viewer"]["is_owner"] is True
                    assert current["id"] == profile["id"]
                    assert current["display_name"] == profile["display_name"]
                    assert (root / "character" / "SOUL.md").read_bytes() == soul
                    if not revoked:
                        assert (
                            await client.post(
                                "__test/revoke-device", json={"device_id": owner_device}
                            )
                        ).status_code == 200
                    else:
                        assert (
                            await client.get("client/v1/character/soul", headers=owner)
                        ).status_code == 401
                    await stop(client)
        finally:
            if process is not None:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), 10)


async def test_soul_reference_process_uses_paired_shared_access_files_and_personal_views(tmp_path):
    root, state_file = tmp_path / "server", tmp_path / "state.json"
    log_file = tmp_path / "server.log"
    with log_file.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.soul_reference_server",
            "--root",
            str(root),
            "--state-file",
            str(state_file),
            "--port",
            "0",
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(20):
                while not state_file.exists():
                    if process.returncode is not None:
                        raise AssertionError(log_file.read_text(encoding="utf-8")[-6000:])
                    await asyncio.sleep(0.05)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            assert state["base_url"].endswith("/role/test")
            async with httpx.AsyncClient(base_url=state["base_url"] + "/") as client:
                assert (await client.get("healthz")).json() == {"ok": True}
                meta = (await client.get("client/v1/meta")).json()["data"]
                assert meta["character"]["display_name"] == "初始角色"

                async def pair(code):
                    response = await client.post(
                        "client/v1/pair", json={"code": code, "device_label": "Desktop E2E"}
                    )
                    assert response.status_code == 200, response.text
                    return (
                        {"Authorization": "Bearer " + response.json()["data"]["token"]},
                        response.json()["data"]["device_id"],
                    )

                owner, owner_device = await pair(state["owner_pairing_code"])
                nonowner, _ = await pair(state["nonowner_pairing_code"])
                second, _ = await pair((await client.post("__test/link-code")).json()["code"])
                first_me = (await client.get("client/v1/me", headers=owner)).json()["data"]
                second_me = (await client.get("client/v1/me", headers=second)).json()["data"]
                assert first_me["person_id"] == second_me["person_id"]
                home = (await client.get("client/v1/home", headers=owner)).json()["data"]
                assert home["works"] == []
                events = await client.get(
                    f"client/v1/conversations/{home['main']['conversation_id']}/events",
                    headers=owner,
                )
                assert events.status_code == 200, events.text
                async with client.stream("GET", "client/v1/stream", headers=owner) as stream:
                    assert stream.status_code == 200
                    assert "text/event-stream" in stream.headers["content-type"]
                    async for line in stream.aiter_lines():
                        if line.startswith("data:"):
                            assert json.loads(line.removeprefix("data:"))
                            break
                missing_work = await client.get(f"client/v1/works/{uuid4()}", headers=owner)
                assert missing_work.status_code == 404

                profile = (await client.get("client/v1/character", headers=owner)).json()["data"]
                changed = await client.patch(
                    "client/v1/character",
                    headers=owner,
                    json={
                        "request_id": str(uuid4()),
                        "expected_revision": profile["revision"],
                        "display_name": "新名称",
                    },
                )
                assert changed.status_code == 200, changed.text
                renamed = (await client.get("client/v1/meta")).json()["data"]["character"]
                assert renamed["display_name"] == "新名称" and renamed["id"] == profile["id"]

                shared = await client.get("client/v1/character/soul", headers=nonowner)
                assert shared.status_code == 200, shared.text
                document = (await client.get("client/v1/character/soul", headers=owner)).json()[
                    "data"
                ]
                saved = await client.put(
                    "client/v1/character/soul",
                    headers=owner,
                    json={
                        "request_id": str(uuid4()),
                        "expected_revision": document["revision"],
                        "content": "",
                    },
                )
                assert saved.status_code == 200, saved.text
                assert (root / "character" / "SOUL.md").read_bytes() == b""
                reloaded = await client.post(
                    "client/v1/character/soul/reload",
                    headers=second,
                    json={"request_id": str(uuid4())},
                )
                assert reloaded.status_code == 200, reloaded.text
                revoked = await client.post(
                    "__test/revoke-device", json={"device_id": owner_device}
                )
                assert revoked.status_code == 200
                assert (
                    await client.get("client/v1/character/soul", headers=owner)
                ).status_code == 401
                assert (
                    await client.get("client/v1/character/soul", headers=second)
                ).status_code == 200
                assert (await client.post("__test/shutdown")).json() == {"ok": True}
                assert await asyncio.wait_for(process.wait(), 10) == 0
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 10)
