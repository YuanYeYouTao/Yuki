"""WorkAdminCommands chat parsing over shared application operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yuki.admin.authority.principal import ControlPrincipal
from yuki.domain.messages import InboundMessage
from yuki.services.commands.help import ADMIN_ACTIONS, admin_usage

if TYPE_CHECKING:
    from yuki.admin.operations import AdminOperations
    from yuki.admin.queries import AdminQueries


class WorkAdminCommands:
    def __init__(self, operations: AdminOperations, queries: AdminQueries) -> None:
        self.operations, self.queries = operations, queries

    async def execute(
        self, principal: ControlPrincipal, message: InboundMessage, argument: str
    ) -> Any:
        queries = self.queries
        action, _, identity = argument.strip().partition(" ")
        action, identity = action.casefold(), identity.strip()
        if action and action not in ADMIN_ACTIONS["work"]:
            raise ValueError(admin_usage("work"))
        if action in {"", "list"}:
            state, _, after = identity.partition(" ")
            return await queries.works(
                principal, state=None if state in {"", "all"} else state, after=after
            )
        if action == "show":
            return await queries.work(principal, identity)
        if action not in {"cancel", "resume"}:
            raise ValueError("Work 操作：list [state|all] [cursor]|show|cancel|resume")

        async def revision() -> int:
            return int((await queries.work(principal, identity))["revision"])

        if message.source_event_id is None:
            raise ValueError("管理修改缺少原内部事件。")
        command = await self.operations.executor.prepare_chat_command(
            principal,
            message.source_event_id,
            payload={"resource_id": identity, "action": action},
            revision=revision,
        )
        return await self.operations.work.work_action(principal, identity, action, command)
