"""IdentityAdminCommands chat parsing over shared application operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yuki.admin.authority.principal import ControlPrincipal
from yuki.domain.identity import PersonId
from yuki.domain.messages import InboundMessage
from yuki.services.commands.help import ADMIN_ACTIONS, admin_usage

if TYPE_CHECKING:
    from yuki.admin.operations import AdminOperations
    from yuki.admin.queries import AdminQueries


class IdentityAdminCommands:
    def __init__(self, operations: AdminOperations, queries: AdminQueries) -> None:
        self.operations, self.queries = operations, queries

    async def person(
        self, principal: ControlPrincipal, message: InboundMessage, argument: str
    ) -> Any:
        queries = self.queries
        action, _, rest = argument.strip().partition(" ")
        action, rest = action.casefold(), rest.strip()
        if action and action not in ADMIN_ACTIONS["person"]:
            raise ValueError(admin_usage("person"))
        if action in {"", "list"}:
            return await queries.persons(principal, after=rest)
        if action == "show":
            return await queries.person(principal, PersonId.parse(rest).text)
        identity, _, value = rest.partition(" ")
        if action != "create":
            identity = PersonId.parse(identity).text
        payload: dict[str, Any] = {}
        if action == "create":
            identity = "yuki"
            payload = {"display_name": rest} if rest else {}
        elif action == "rename":
            payload = {"display_name": value}
        elif action == "merge":
            payload = {"to_person": PersonId.parse(value).text}
        elif action not in {"enable", "disable", "grant", "revoke", "forget"}:
            raise ValueError(
                "人物操作：list|show|create|enable|disable|grant|revoke|rename|merge|forget"
            )

        async def revision() -> int:
            if action in {"create", "rename", "merge", "forget"}:
                return 0
            return int((await queries.person(principal, identity))["revision"])

        if action in {"grant", "revoke"}:
            payload = {"is_superuser": action == "grant"}
        if message.source_event_id is None:
            raise ValueError("管理修改缺少原内部事件。")
        command = await self.operations.executor.prepare_chat_command(
            principal,
            message.source_event_id,
            payload=payload,
            revision=revision,
        )
        if action == "create":
            return await self.operations.identity.create_person(principal, command)
        if action in {"enable", "disable"}:
            return await self.operations.identity.person_state(
                principal, identity, command, enabled=action == "enable"
            )
        if action in {"grant", "revoke"}:
            return await self.operations.identity.person_state(
                principal, identity, command, is_superuser=action == "grant"
            )
        if action in {"rename", "merge"}:
            return await self.operations.identity.change_person(
                principal, identity, command.request_id.text, **payload
            )
        if action == "forget":
            return await self.operations.identity.forget_person(
                principal, identity, command.request_id.text
            )
        raise ValueError("人物配对使用私聊 /ai link 或 operator CLI。")

    async def binding(
        self, principal: ControlPrincipal, message: InboundMessage, argument: str
    ) -> Any:
        queries = self.queries
        action, _, rest = argument.strip().partition(" ")
        action, rest = action.casefold(), rest.strip()
        if action and action not in ADMIN_ACTIONS["binding"]:
            raise ValueError(admin_usage("binding"))
        if action in {"", "list"}:
            return await queries.bindings(principal, person_id=rest or None)
        if action == "show":
            return await queries.binding(principal, rest)
        values = rest.split(maxsplit=3)
        if not values:
            raise ValueError(
                "请指定 Binding ID；attach 使用 <Person ID> <platform> <account> [名称]。"
            )
        identity = PersonId.parse(values[0]).text
        payload: dict[str, Any] = {}
        if action == "attach" and len(values) >= 3:
            payload = {
                "platform": values[1],
                "external_account_id": values[2],
                "display_name": values[3] if len(values) > 3 else "",
            }
        elif action == "transfer" and len(values) == 2:
            payload = {"to_person": PersonId.parse(values[1]).text}
        elif action not in {"enable", "disable"}:
            raise ValueError("账号操作：list [Person ID]|show|attach|enable|disable|transfer")

        async def revision() -> int:
            if action == "transfer":
                return 0
            row = (
                await queries.person(principal, identity)
                if action == "attach"
                else await queries.binding(principal, identity)
            )
            return int(row["revision"])

        if message.source_event_id is None:
            raise ValueError("管理修改缺少原内部事件。")
        command = await self.operations.executor.prepare_chat_command(
            principal,
            message.source_event_id,
            payload=payload,
            revision=revision,
        )
        if action == "attach":
            return await self.operations.identity.attach_binding(principal, identity, command)
        if action in {"enable", "disable"}:
            return await self.operations.identity.binding_state(
                principal, identity, command, enabled=action == "enable"
            )
        return await self.operations.identity.transfer_binding(
            principal, identity, payload["to_person"], command.request_id.text
        )

    async def presence(
        self, principal: ControlPrincipal, message: InboundMessage, argument: str
    ) -> Any:
        queries = self.queries
        action, _, identity = argument.strip().partition(" ")
        action, identity = action.casefold(), identity.strip()
        if action and action not in ADMIN_ACTIONS["presence"]:
            raise ValueError(admin_usage("presence"))
        if action in {"", "list"}:
            return await queries.presences(principal, after=identity)
        if action == "show":
            return await queries.presence(principal, identity)
        fields = (
            {"enabled": action == "enable"}
            if action in {"enable", "disable"}
            else {"ingest_eligible": action == "ingest-enable"}
        )
        if action not in {"enable", "disable", "ingest-enable", "ingest-disable"}:
            raise ValueError("Presence 操作：list|show|enable|disable|ingest-enable|ingest-disable")

        async def revision() -> int:
            return int((await queries.presence(principal, identity))["revision"])

        if message.source_event_id is None:
            raise ValueError("管理修改缺少原内部事件。")
        command = await self.operations.executor.prepare_chat_command(
            principal,
            message.source_event_id,
            payload=fields,
            revision=revision,
        )
        return await self.operations.identity.presence_state(principal, identity, command, **fields)
