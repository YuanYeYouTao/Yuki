"""PluginAdminCommands chat parsing over shared application operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yuki.admin.authority.principal import ControlPrincipal
from yuki.domain.messages import InboundMessage
from yuki.services.commands.help import ADMIN_ACTIONS, admin_usage

if TYPE_CHECKING:
    from yuki.admin.operations import AdminOperations
    from yuki.admin.queries import AdminQueries

import json


class PluginAdminCommands:
    def __init__(self, operations: AdminOperations, queries: AdminQueries) -> None:
        self.operations, self.queries = operations, queries

    async def execute(
        self, principal: ControlPrincipal, message: InboundMessage, argument: str
    ) -> Any:
        queries = self.queries
        action, _, rest = argument.strip().partition(" ")
        action = action.casefold()
        if action and action not in ADMIN_ACTIONS["plugin"]:
            raise ValueError(admin_usage("plugin"))
        identity, _, value = rest.strip().partition(" ")
        if action in {"", "list"}:
            return await queries.plugins_list(principal)
        if action in {"show", "permissions", "doctor"}:
            return await queries.plugin(principal, identity)
        if action == "configuration":
            return await queries.plugin_config(principal, identity, "global", None)
        if action == "configure":
            spec = {"scope_type": "global", "owner_id": None, "values": json.loads(value)}

            async def config_revision() -> int:
                return int(
                    (await queries.plugin_config(principal, identity, "global", None))["revision"]
                )

            if message.source_event_id is None:
                raise ValueError("管理修改缺少原内部事件。")
            command = await self.operations.executor.prepare_chat_command(
                principal,
                message.source_event_id,
                payload={"resource_id": identity, "action": "save", "spec": spec},
                revision=config_revision,
            )
            return await self.operations.plugin.configure_plugin(principal, identity, command, spec)
        if action not in {"discover", "approve", "enable", "disable"}:
            raise ValueError(
                "插件操作：list|show|permissions|discover|approve|enable|disable|doctor|configuration|configure"
            )
        target = "yuki" if action == "discover" else identity
        permissions = value.split() if action == "approve" else []
        payload: dict[str, Any] = {"resource_id": target, "action": action}
        if action == "approve":
            payload["spec"] = {"permissions": permissions}

        async def revision() -> int:
            return (
                0
                if action == "discover"
                else int((await queries.plugin(principal, identity))["revision"])
            )

        if message.source_event_id is None:
            raise ValueError("管理修改缺少原内部事件。")
        command = await self.operations.executor.prepare_chat_command(
            principal,
            message.source_event_id,
            payload=payload,
            revision=revision,
        )
        return await self.operations.plugin.plugin_action(
            principal, target, action, command, permissions
        )
