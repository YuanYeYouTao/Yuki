"""Finite chat adapters over shared management operations and domain owners."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from yuki.admin.authority.command_types import ControlCommandError
from yuki.admin.authority.commands import ControlResult
from yuki.admin.authority.json_types import thaw_json_value
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.problems import ControlQueryError
from yuki.admin.control_resolution import ControlAccess
from yuki.admin.models import AdminActor
from yuki.admin.operations.base import authorize_current
from yuki.client_api.errors import ClientError
from yuki.domain.messages import InboundMessage
from yuki.gateway.media import PlatformMediaGateway
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.services.commands.emoji import EmojiCommands
from yuki.services.commands.group import GROUP_USAGE, parse_group_command
from yuki.services.commands.help import ADMIN_HELP
from yuki.services.commands.identity import IdentityAdminCommands
from yuki.services.commands.memory import MemoryCommands
from yuki.services.commands.models import ModelCommands
from yuki.services.commands.plugins import PluginAdminCommands
from yuki.services.commands.work import WorkAdminCommands
from yuki.services.config_commands import ConfigCommandHandler
from yuki.services.profile_commands import ProfileCommandHandler

if TYPE_CHECKING:
    from yuki.admin.operations import AdminOperations
    from yuki.admin.queries import AdminQueries


def render_result(value: Any) -> str:
    if isinstance(value, ControlResult):
        reference = value.operation
        operation = asdict(reference) if reference is not None else None
        if reference is not None and operation is not None:
            operation["created_at"] = reference.created_at.isoformat()
            operation["updated_at"] = reference.updated_at.isoformat()
        value = {
            "success": value.success,
            "resource_id": value.resource_id,
            "revision": value.revision,
            "effective_state": thaw_json_value(value.effective_state),
            "operation": operation,
        }
    return json.dumps(value, ensure_ascii=False, indent=2)


class AdminChatCommands:
    def __init__(
        self,
        *,
        operations: AdminOperations,
        queries: AdminQueries,
        control: ControlAccess,
        config: ConfigCommandHandler,
        profiles: ProfileCommandHandler,
        memory: MemoryCommands,
        emoji: EmojiCommands | None,
        models: ModelCommands,
    ) -> None:
        self.operations, self.queries = operations, queries
        self.identity = IdentityAdminCommands(operations, queries)
        self.plugins = PluginAdminCommands(operations, queries)
        self.works = WorkAdminCommands(operations, queries)
        self.control, self.config = control, config
        self.profiles, self.memory, self.emoji, self.models = profiles, memory, emoji, models

    async def execute(
        self,
        principal: ControlPrincipal,
        actor: AdminActor,
        message: InboundMessage,
        argument: str,
        gateway: PlatformMediaGateway | None,
    ) -> str:
        family, _, rest = argument.strip().partition(" ")
        family = family.casefold()
        try:
            if family in {"", "help"}:
                return ADMIN_HELP
            if family == "group":
                await authorize_current(self.control._database, principal, "control.group.mutate")
                parsed = parse_group_command(argument)
                if parsed is None:
                    return GROUP_USAGE
                target, action = parsed
                identity = message.space_id if target.casefold() == "current" else target
                if identity is None:
                    return "当前消息不在群聊中，请指定 Space ID。"
                if message.source_event_id is None:
                    raise ValueError("管理修改缺少原内部事件。")

                async def group_revision() -> int:
                    async with self.control._database.sessions() as session:
                        row = await session.get(CanonicalSpaceModel, identity)
                        if row is None:
                            raise ValueError("没有找到该 Space。")
                        return row.revision

                command = await self.operations.executor.prepare_chat_command(
                    principal,
                    message.source_event_id,
                    payload={"action": action, "enabled": action == "enable"},
                    revision=group_revision,
                )
                result = await self.operations.group.set_enabled(
                    principal, identity, command, enabled=action == "enable"
                )
                if not result.success:
                    return render_result(result)
                return f"已{'启用' if action == 'enable' else '停用'}群 {identity}。"
            if family == "config":
                await authorize_current(self.control._database, principal, "control.config.read")
                return await self.config.config(actor, rest, principal=principal)
            if family == "memory":
                return (await self.memory.memory(actor=actor, argument=rest, admin=True)).replace(
                    "/ai memory", "/ai admin memory"
                )
            if family == "preference":
                return (
                    await self.profiles.preference(actor=actor, argument=rest, admin=True)
                ).replace("/ai preference", "/ai admin preference")
            if family == "emoji":
                if self.emoji is None:
                    return "表情系统当前未启用。"
                return await self.emoji.execute(
                    actor=actor, message=message, argument=rest, gateway=gateway
                )
            if family == "person":
                return render_result(await self.identity.person(principal, message, rest))
            if family == "binding":
                return render_result(await self.identity.binding(principal, message, rest))
            if family == "presence":
                return render_result(await self.identity.presence(principal, message, rest))
            if family == "work":
                return render_result(await self.works.execute(principal, message, rest))
            if family == "plugin":
                return render_result(await self.plugins.execute(principal, message, rest))
            if family == "model":
                await authorize_current(
                    self.operations.executor.database, principal, "control.config.file.content.read"
                )
                if rest.strip().casefold() == "stats":
                    return await self.models.stats()
                if rest.strip().casefold() == "show":
                    return render_result(await self.operations.model.files.read())
                return "格式：/ai admin model show|stats；模型文件与密钥使用管理 CLI。"
            if family == "operation" and rest.strip():
                return render_result(await self.queries.operation(principal, rest.strip()))
            return ADMIN_HELP
        except (ControlCommandError, ControlQueryError) as exc:
            return f"操作未完成：{exc.problem.code.value}"
        except ClientError as exc:
            return f"操作未完成：{exc.code}"
        except (ValueError, LookupError, PermissionError) as exc:
            return f"操作未完成：{exc}"
