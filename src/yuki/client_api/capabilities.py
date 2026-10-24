"""Public descriptions from approved plugins and the live Manager manifest."""

import asyncio
from typing import Any
from uuid import uuid4

from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.plugin_host.manager import PluginManager
from yuki.sandbox.client import SandboxClient


class ClientCapabilities:
    def __init__(
        self, identity: ClientIdentityService, plugins: PluginManager, sandbox: SandboxClient
    ) -> None:
        self.identity, self.plugins, self.sandbox = identity, plugins, sandbox

    async def get(self, principal: ClientPrincipal) -> dict[str, Any]:
        async with self.identity.database.read_session() as session:
            await self.identity.require_principal(session, principal)
        plugins, status = await asyncio.gather(
            self.plugins.public_capabilities(),
            self.sandbox.execute("environment_status", {}, request_id=str(uuid4())),
        )
        async with self.identity.database.read_session() as session:
            await self.identity.require_principal(session, principal)
        return {
            "plugins": plugins,
            "environment": {
                "tools": [
                    {"name": tool["name"], "version": tool["version"]}
                    for tool in status.get("tools", ())
                ],
                "desktop": status.get("desktop") is True,
            },
        }
