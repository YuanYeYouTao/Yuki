"""Event-proven group recovery outside ordinary group admission."""

from __future__ import annotations

from yuki.admin.control_resolution import ControlAccess
from yuki.admin.models import ControlAuditRef
from yuki.config import Settings
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.gateway.registry import (
    ConnectionResolution,
    GatewayConnectionRegistry,
    RegistryClosed,
)
from yuki.identity.routing import PresenceRouter, RouteSendError
from yuki.persistence.database import Database
from yuki.services.admin.group_recovery import GroupRecoveryService
from yuki.services.commands.group import parse_group_command
from yuki.services.policies import CommandName, parse_command


class NativeGroupRecovery:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        registry: GatewayConnectionRegistry,
        router: PresenceRouter,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._access = ControlAccess(database)
        self._service = GroupRecoveryService(database, registry, router)

    def _command(self, message: InboundMessage) -> tuple[CommandName | None, str]:
        if (
            message.platform in LOCAL_CLIENT_PLATFORMS
            or message.scope_type is not ScopeType.GROUP
            or not message.group_id
            or message.is_self_message
            or message.sender.is_bot
        ):
            return None, ""
        command, argument, _triggered = parse_command(message.text, self._settings.ai_prefix)
        return command, argument

    def is_enable_request(self, message: InboundMessage) -> bool:
        command, argument = self._command(message)
        parsed = parse_group_command(argument)
        return (
            command is CommandName.ADMIN
            and parsed is not None
            and (parsed[0].casefold(), parsed[1]) == ("current", "enable")
        )

    def _connection(self, bot: object, message: InboundMessage) -> ConnectionResolution:
        connection = self._registry.resolve_by_handle(bot)
        snapshot = connection.snapshot
        if (
            snapshot.platform != message.platform
            or not snapshot.presence_id
            or (message.bot_user_id and message.bot_user_id != snapshot.external_account_id)
            or message.sender.user_id == snapshot.external_account_id
            or self._registry.resolve_active(snapshot.presence_id).bot is not bot
        ):
            raise RegistryClosed("bot_handle_mismatch")
        return connection

    async def enable(self, bot: object, message: InboundMessage) -> str | None:
        if not self.is_enable_request(message):
            return None
        try:
            connection = self._connection(bot, message)
            principal = await self._access.recovery_principal(
                message.sender.user_id, platform=connection.snapshot.platform
            )
            target = await self._access.recovery_space_target(
                message.group_id or "", platform=connection.snapshot.platform
            )
            changed = await self._service.enable(
                self._access.context(principal, target),
                presence_id=connection.snapshot.presence_id or "",
                connection_id=connection.snapshot.connection_id,
                audit=ControlAuditRef(
                    user_id=message.sender.user_id,
                    trigger_message_id=message.message_id,
                    conversation_key=ConversationScope.group(
                        connection.snapshot.external_account_id,
                        message.group_id or "",
                        platform=connection.snapshot.platform,
                        presence_id=connection.snapshot.presence_id,
                        thread_id=message.thread_id,
                    ).key,
                    bot_user_id=connection.snapshot.external_account_id,
                ),
                event_type=message.event_type,
            )
        except RegistryClosed:
            return None
        except PermissionError:
            return "无法恢复：没有可用的超级管理员控制主体。"
        except RouteSendError as exc:
            if exc.category == "not_ingest":
                return None  # Another healthy ingest Presence owns this fanout event.
            if exc.category == "ambiguous":
                return "无法恢复：有多个可用账号且没有有效的接入账号，请先停用其他连接后重试。"
            if exc.category == "conflict":
                return (
                    "恢复未执行：路由或连接刚发生变化，请重新发送 /ai admin group current enable。"
                )
            return "无法恢复：当前群未配置、账号不可用或群成员验证失败。"
        if not changed:
            return "该启用请求已处理；如需再次恢复，请发送一条新的 /ai admin group current enable。"
        return "已启用当前群并恢复可用路由；会话和记忆保持不变。"

    async def hint(self, bot: object, message: InboundMessage, reason: str) -> str | None:
        command, _argument = self._command(message)
        if reason != "paused" or command is None:
            return None
        try:
            connection = self._connection(bot, message)
            await self._access.recovery_principal(
                message.sender.user_id, platform=connection.snapshot.platform
            )
        except (RegistryClosed, PermissionError):
            return None
        return (
            "当前群接入路由已暂停，命令未执行。请超级管理员在本群发送 "
            "/ai admin group current enable 恢复。"
        )
