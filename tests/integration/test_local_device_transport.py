"""Real Connector, TLS WSS, HTTP, shell processes, durable receipts and byte transfer."""

import asyncio
import json
import shlex
import sys
from contextlib import asynccontextmanager

from tests.integration.test_external_agent_bridge import bridge as bridge
from websockets.asyncio.client import connect
from yuki_agent_connector import connector as connector_module
from yuki_agent_connector.config import Config
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal

from yuki.sandbox.client import SandboxClient


def python_command(script):
    if sys.platform == "win32":
        return "& " + " ".join(
            "'" + str(value).replace("'", "''") + "'" for value in (sys.executable, script)
        )
    return " ".join(shlex.quote(str(value)) for value in (sys.executable, script))


@asynccontextmanager
async def actual_connector(bridge, tmp_path, monkeypatch):
    def tls_connect(url, **kwargs):
        return connect(url, ssl=bridge.ssl, proxy=None, **kwargs)

    monkeypatch.setattr(connector_module, "connect", tls_connect)
    journal = Journal(
        tmp_path / "isolated-connector" / "journal.sqlite3", namespace=bridge.device["device_id"]
    )
    connector = Connector(
        Config(
            bridge.websocket_url,
            bridge.device["device_id"],
            bridge.device["token"],
            instances=[],
            full_access=True,
        ),
        journal,
    )
    running = asyncio.create_task(connector.run())
    try:
        async with asyncio.timeout(8):
            while not bridge.service.connections.online(bridge.device["device_id"]):
                if running.done():
                    await running
                await asyncio.sleep(0.01)
        yield connector
    finally:
        socket = connector.stream
        running.cancel()
        if socket is not None:
            await socket.close()
        await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 8)
        async with asyncio.timeout(8):
            while bridge.service.connections.online(bridge.device["device_id"]):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        journal.close()


async def test_real_connector_terminal_http_incremental_completion_and_cancel(
    bridge, tmp_path, monkeypatch
):
    async with actual_connector(bridge, tmp_path, monkeypatch) as connector:
        device_id = bridge.device["device_id"]
        source = {
            **bridge.source,
            "conversation_id": bridge.env.context.conversation_id,
            "parent_execution_id": "isolated-integration",
        }
        client = SandboxClient(tmp_path / "unused-sandbox", tasks=bridge.service.tasks)
        client.local_devices = bridge.service.local_devices
        script = tmp_path / "incremental.py"
        script.write_text(
            "import time\nprint('first-雪', flush=True)\n"
            "time.sleep(1)\nprint('second-雪', flush=True)\n",
            encoding="utf-8",
        )
        result = await client.execute(
            "terminal_exec",
            {"device_id": device_id, "command": python_command(script), "cwd": str(tmp_path)},
            request_id="real-local-shell",
            source=source,
        )
        run_id = result["run_id"]
        assert result["pending"] is True
        first = ""
        cursor = 0
        async with asyncio.timeout(12):
            while "first-雪" not in first:
                response = await bridge.http.get(
                    f"/agents/v1/terminals/{run_id}",
                    params={"cursor": cursor},
                    headers=bridge.headers,
                )
                assert response.status_code == 200, response.text
                page = response.json()
                first += page["output"]
                cursor = page["next_cursor"]
                await asyncio.sleep(0.01)
        assert page["pending"] is True and "second-雪" not in first
        output = first
        async with asyncio.timeout(12):
            while True:
                response = await bridge.http.get(
                    f"/agents/v1/terminals/{run_id}",
                    params={"cursor": cursor},
                    headers=bridge.headers,
                )
                assert response.status_code == 200, response.text
                page = response.json()
                output += page["output"]
                cursor = page["next_cursor"]
                if page["pending"] is False and "second-雪" in output:
                    break
                await asyncio.sleep(0.01)
        assert output.count("first-雪") == output.count("second-雪") == 1
        assert page["status"] == "succeeded" and page["exit_code"] == 0
        listing = await bridge.http.get(
            "/agents/v1/terminals", params={"device_id": device_id}, headers=bridge.headers
        )
        assert listing.status_code == 200, listing.text
        assert [item["run_id"] for item in listing.json()["items"]] == [run_id]
        async with asyncio.timeout(8):
            while connector.journal.next_event() is not None:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        stored = await bridge.service.tasks.get("real-local-shell")
        assert (
            stored.status == "completed" and json.loads(stored.completion_json)["run_id"] == run_id
        )
        replay = await client.execute(
            "terminal_exec",
            {"device_id": device_id, "command": python_command(script), "cwd": str(tmp_path)},
            request_id="real-local-shell",
            source=source,
        )
        assert replay["status"] == "succeeded"
        assert (
            connector.journal.db.execute(
                "SELECT COUNT(*) FROM operations WHERE json_extract(command,'$.action')='exec'"
            ).fetchone()[0]
            == 1
        )
        long = tmp_path / "long.py"
        long.write_text(
            "import time\nprint('cancel-target', flush=True)\ntime.sleep(30)\n", encoding="utf-8"
        )
        active = await client.execute(
            "terminal_exec",
            {"device_id": device_id, "command": python_command(long), "cwd": str(tmp_path)},
            request_id="cancel-original-shell",
            source=source,
        )
        cancelled = await bridge.http.post(
            f"/agents/v1/terminals/{active['run_id']}/control",
            json={"request_id": "cancel-once", "action": "cancel"},
            headers=bridge.headers,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        receipt = await bridge.http.get(
            f"/agents/v1/terminals/{active['run_id']}/controls/cancel-once", headers=bridge.headers
        )
        assert receipt.status_code == 200, receipt.text
        assert (
            receipt.json()["status"] == "cancelled"
            and receipt.json()["request_id"] == "cancel-once"
        )
        assert (
            connector.journal.db.execute(
                "SELECT COUNT(*) FROM operations WHERE json_extract(command,'$.action')='cancel'"
            ).fetchone()[0]
            == 1
        )


async def test_real_connector_file_selection_transfers_original_bytes(
    bridge, tmp_path, monkeypatch
):
    async with actual_connector(bridge, tmp_path, monkeypatch) as connector:
        data = b"actual-local-file\x00\xff" + "雪".encode()
        selected = tmp_path / "selected.bin"
        selected.write_bytes(data)
        async with bridge.service.local_devices.download(
            bridge.device["device_id"], bridge.env.person, str(selected), request_id="read-selected"
        ) as (path, metadata):
            assert path.read_bytes() == data and metadata["size"] == len(data)
        assert not path.exists()
        assert not connector.files.sources
