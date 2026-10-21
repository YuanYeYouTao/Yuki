"""Desktop's isolated subprocess fixture exercises original HTTP services."""

import asyncio
import json
import os
import sys
from uuid import uuid4

import httpx


async def test_soul_reference_process_uses_real_owners_files_and_personal_views(tmp_path):
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
                    return {"Authorization": "Bearer " + response.json()["data"]["token"]}

                owner = await pair(state["owner_pairing_code"])
                nonowner = await pair(state["nonowner_pairing_code"])
                second = await pair((await client.post("__test/link-code")).json()["code"])
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

                denied = await client.get("client/v1/character/soul", headers=nonowner)
                assert denied.status_code == 403, denied.text
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
                revoked = await client.post("__test/revoke-owner")
                assert revoked.status_code == 200
                assert (
                    await client.get("client/v1/character/soul", headers=owner)
                ).status_code == 403
                assert (
                    await client.get("client/v1/character/soul", headers=second)
                ).status_code == 403
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 10)
