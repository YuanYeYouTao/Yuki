"""Commands for capability discovery and reviewed runtime configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from yuki.admin.authority.commands import ControlResult
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.models import (
    AdminActor,
    ConfigApplyMode,
    ConfigChangeResult,
    EffectiveConfigValue,
)
from yuki.admin.permission_catalog import PermissionCatalogService
from yuki.domain.identity import PersonId
from yuki.domain.messages import InboundMessage
from yuki.services.admin.config_admin import ConfigAdminService
from yuki.services.commands.help import ADMIN_ACTIONS

if TYPE_CHECKING:
    from yuki.admin.operations import AdminOperations


class ConfigCommandHandler:
    """Render configuration commands using the shared runtime service."""

    def __init__(
        self,
        *,
        config_admin: ConfigAdminService,
        permission_catalog: PermissionCatalogService,
        operations: AdminOperations,
    ) -> None:
        self._operations = operations
        self._config_admin = config_admin
        self._permission_catalog = permission_catalog

    def capabilities(self, message: InboundMessage, argument: str, *, is_superuser: bool) -> str:
        category = argument.strip() or None
        report = self._permission_catalog.report_for_message(
            message, category=category, actor_is_superuser=is_superuser
        )
        return report.render_text()

    async def config(self, actor: AdminActor, argument: str, *, principal: ControlPrincipal) -> str:
        parts = argument.split()
        if not parts:
            return (
                "格式：/ai admin config list|get|set|unset|history|rollback ...\n"
                "群级后缀：group current；用户级后缀：user <Person ID>"
            )
        operation = parts.pop(0).casefold()
        if operation not in ADMIN_ACTIONS["config"]:
            return "可用操作：list、get、set、unset、history、rollback。"
        if operation == "list":
            category = parts[0] if parts else None
            if len(parts) > 1:
                return "格式：/ai admin config list [类别]"
            specs = self._config_admin.list_capabilities(category)
            if not specs:
                return "没有找到该类别的配置。"
            return "\n".join(
                (f"{spec.key} [{spec.apply_mode.value}] {'可修改' if spec.mutable else '受保护'}")
                for spec in specs
            )
        if operation == "get":
            if not parts:
                return "格式：/ai admin config get <key> [group current|user <Person ID>]"
            key = parts.pop(0)
            parsed_scope = self._parse_config_command_scope(parts, actor)
            if isinstance(parsed_scope, str):
                return parsed_scope
            scope_type, scope_id = parsed_scope
            try:
                value = await self._config_admin.get(
                    key,
                    user_id=scope_id if scope_type == "user" else None,
                    group_id=scope_id if scope_type == "group" else None,
                )
            except KeyError:
                return "未知配置键；使用 /ai admin config list 查看注册项。"
            return self._render_effective_config(value)
        if operation == "set":
            if len(parts) < 2:
                return "格式：/ai admin config set <key> <value> [group current|user <Person ID>]"
            key, raw_value = parts.pop(0), parts.pop(0)
            parsed_scope = self._parse_config_command_scope(parts, actor)
            if isinstance(parsed_scope, str):
                return parsed_scope
            scope_type, scope_id = parsed_scope
            if actor.trigger_event_id is None:
                raise ValueError("管理修改缺少原内部事件。")
            key = self._config_admin._runtime_config.registry.get(key).key

            async def revision() -> int:
                return await self._operations.config.revision(
                    principal, key, scope_type=scope_type, scope_id=scope_id
                )

            payload = {
                "action": "set",
                "key": key,
                "value": raw_value,
                "scope_type": scope_type,
                "scope_id": scope_id,
            }
            command = await self._operations.executor.prepare_chat_command(
                principal,
                actor.trigger_event_id,
                payload=payload,
                revision=revision,
            )
            result = await self._operations.config.set(
                principal,
                command,
                actor=actor,
                key=key,
                value=raw_value,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            return self._render_config_change(result)
        if operation == "unset":
            if not parts:
                return "格式：/ai admin config unset <key> [group current|user <Person ID>]"
            key = parts.pop(0)
            parsed_scope = self._parse_config_command_scope(parts, actor)
            if isinstance(parsed_scope, str):
                return parsed_scope
            scope_type, scope_id = parsed_scope
            if actor.trigger_event_id is None:
                raise ValueError("管理修改缺少原内部事件。")
            key = self._config_admin._runtime_config.registry.get(key).key

            async def unset_revision() -> int:
                return await self._operations.config.revision(
                    principal, key, scope_type=scope_type, scope_id=scope_id
                )

            payload = {
                "action": "unset",
                "key": key,
                "scope_type": scope_type,
                "scope_id": scope_id,
            }
            command = await self._operations.executor.prepare_chat_command(
                principal,
                actor.trigger_event_id,
                payload=payload,
                revision=unset_revision,
            )
            result = await self._operations.config.unset(
                principal,
                command,
                actor=actor,
                key=key,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            return self._render_config_change(result)
        if operation == "history":
            if len(parts) > 1:
                return "格式：/ai admin config history [key]"
            try:
                rows = await self._config_admin.history(
                    key=parts[0] if parts else None,
                    actor_user_id=actor.person_id,
                    limit=20,
                )
            except KeyError:
                return "未知配置键。"
            if not rows:
                return "暂无配置修改记录。"
            return "\n".join(
                (
                    f"{row.id}. {row.operation} {row.target_id} "
                    f"{'成功' if row.success else '失败'} "
                    f"{row.created_at:%Y-%m-%d %H:%M}"
                )
                for row in rows
            )
        if operation == "rollback":
            if len(parts) != 1 or not parts[0].isdigit():
                return "格式：/ai admin config rollback <change_id>"
            if actor.trigger_event_id is None:
                raise ValueError("管理修改缺少原内部事件。")
            change_id = int(parts[0])

            async def rollback_revision() -> int:
                return await self._operations.config.rollback_revision(principal, change_id)

            command = await self._operations.executor.prepare_chat_command(
                principal,
                actor.trigger_event_id,
                payload={"action": "rollback", "change_id": change_id},
                revision=rollback_revision,
            )
            result = await self._operations.config.rollback(
                principal,
                command,
                actor=actor,
                change_id=change_id,
            )
            return self._render_config_change(result)
        return "可用操作：list、get、set、unset、history、rollback。"

    @staticmethod
    def _parse_config_command_scope(
        parts: list[str],
        actor: AdminActor,
    ) -> tuple[str, str] | str:
        if not parts:
            return "global", ""
        if len(parts) == 2 and parts[0].casefold() == "group":
            if parts[1].casefold() != "current":
                return "群级作用域只接受 group current。"
            if actor.space_id is None:
                return "当前消息不在群聊中。"
            return "group", actor.space_id
        if len(parts) == 2 and parts[0].casefold() == "user":
            try:
                return "user", PersonId.parse(parts[1]).text
            except (TypeError, ValueError):
                return "目标 Person ID 格式错误。"
        return "作用域格式错误：使用 group current 或 user <Person ID>。"

    @staticmethod
    def _render_effective_config(value: EffectiveConfigValue) -> str:
        if value.configured is not None:
            rendered_value = "已配置" if value.configured else "未配置"
        else:
            rendered_value = str(value.value)
        return (
            f"{value.key} = {rendered_value}\n"
            f"来源：{value.source}\n"
            f"生效方式：{value.apply_mode.value}"
        )

    @staticmethod
    def _render_config_change(result: ConfigChangeResult | ControlResult) -> str:
        if isinstance(result, ControlResult):
            if not result.success:
                operation = result.operation.operation_id if result.operation is not None else ""
                return f"配置结果尚未确认；查询 /ai admin operation {operation}。"
            state = result.effective_state
            change = state["change"]
            assert isinstance(change, Mapping)
            apply_mode = change["apply_mode"]
            suffix = (
                "，需要重启 Bot 后生效"
                if apply_mode == ConfigApplyMode.RESTART_REQUIRED and change["pending_restart"]
                else (
                    "，只影响之后新建的记录或任务"
                    if apply_mode == ConfigApplyMode.FUTURE_ONLY
                    else (
                        "，有效值未变化，无需重启"
                        if apply_mode == ConfigApplyMode.RESTART_REQUIRED
                        else "，已立即生效"
                    )
                )
            )
            return (
                f"配置 {change['key']} 已处理{suffix}。"
                f" 变更编号：{change['change_id']}；revision：{result.revision}。"
            )
        if not result.success:
            return f"配置未修改：{result.detail or result.error_category or '未知错误'}"
        suffix = (
            "，需要重启 Bot 后生效"
            if (result.apply_mode is ConfigApplyMode.RESTART_REQUIRED and result.pending_restart)
            else (
                "，只影响之后新建的记录或任务"
                if result.apply_mode is ConfigApplyMode.FUTURE_ONLY
                else (
                    "，有效值未变化，无需重启"
                    if result.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                    else "，已立即生效"
                )
            )
        )
        return (
            f"已将 {result.key} 从 {result.before} 改为 {result.after}{suffix}。"
            f" 变更编号：{result.change_id}"
        )
