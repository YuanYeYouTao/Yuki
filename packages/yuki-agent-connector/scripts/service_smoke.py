"""Exercise one disposable user service against a loopback fake server.

No native instances, model calls or real server credentials are involved.
The temporary service is always removed; no existing installation is changed.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config
from yuki_agent_connector.service import UserService


async def main() -> None:
    connected = asyncio.Event()

    async def server(socket: Any) -> None:
        hello = json.loads(await socket.recv())
        assert hello["protocol_version"] == 4 and hello["instances"] == []
        connected.set()
        await socket.wait_closed()

    with tempfile.TemporaryDirectory(prefix="yuki-connector-service-") as temporary:
        directory = Path(temporary)
        config = directory / "config.json"
        async with serve(server, "127.0.0.1", 0) as listener:
            Config(
                f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/agents/v1/connect",
                "disposable-smoke-device",
                "disposable-smoke-token",
            ).save(config)
            service = UserService(config, directory)
            installed = False
            try:
                await asyncio.to_thread(service.install)
                installed = True
                await asyncio.to_thread(service.start)
                await asyncio.wait_for(connected.wait(), 30)
                state = await asyncio.to_thread(service.state)
                assert state["running"] is True, state
                print(
                    json.dumps(
                        {
                            "service": service.name,
                            "local_fake_server_connected": True,
                            "native_instances": 0,
                            "state": state,
                        }
                    )
                )
            finally:
                if installed:
                    await asyncio.to_thread(service.uninstall)


if __name__ == "__main__":
    asyncio.run(main())
