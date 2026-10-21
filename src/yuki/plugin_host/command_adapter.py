"""Deterministic `/ai plugin` management and registered command dispatch."""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import cast

from pydantic import ValidationError

from yuki.admin.control_resolution import ControlAccess
from yuki.admin.models import RuntimeConfigSnapshot
from yuki.automation.models import TurnOrigin
from yuki.conversation.scope import plugin_conversation_key
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import InboundMessage
from yuki.plugin_host.direct_command_router import (
    DirectCommandMatch,
    DirectCommandRouter,
)
from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.plugin_host.manager import PluginManager
from yuki.services.agent_tools import ToolRuntime
from yuki_plugin_sdk.models import PermissionLevel
from yuki_plugin_sdk.registrar import CommandRegistration


class PluginCommandAdapter:
    def __init__(
        self,
        *,
        manager: PluginManager,
        registry: ExtensionRegistry,
        control: ControlAccess,
        invocation_scope: Callable[..., object] | None = None,
        direct_commands: DirectCommandRouter | None = None,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._control = control
        self._invocation_scope = invocation_scope
        self._direct_commands = direct_commands

    async def execute(
        self,
        *,
        message: InboundMessage,
        identity: ConversationScope,
        argument: str,
        runtime: RuntimeConfigSnapshot,
    ) -> str:
        parts = argument.strip().split(maxsplit=1)
        operation = parts[0].casefold() if parts else "list"
        remainder = parts[1].strip() if len(parts) == 2 else ""
        if operation == "run":
            return await self._run_command(message, identity, remainder, runtime)
        return (
            "插件管理使用 /ai admin plugin；注册命令使用 "
            "/ai plugin run <plugin_id> <command> [参数]。"
        )

    async def execute_direct(
        self,
        *,
        message: InboundMessage,
        identity: ConversationScope,
        match: DirectCommandMatch,
        runtime: RuntimeConfigSnapshot,
    ) -> str:
        """Execute one Host-owned direct binding through the normal command scope."""

        if not match.active:
            return f"插件直达命令暂不可用：{match.reason}。"
        return await self._execute_registered_command(
            message=message,
            identity=identity,
            plugin_id=match.plugin_id,
            command_name=match.command_name,
            raw_arguments=match.arguments,
            runtime=runtime,
            allow_alias=False,
        )

    async def _run_command(
        self,
        message: InboundMessage,
        identity: ConversationScope,
        remainder: str,
        runtime: RuntimeConfigSnapshot,
    ) -> str:
        parts = remainder.split(maxsplit=2)
        if len(parts) < 2:
            return "格式错误：/ai plugin run <plugin_id> <command> [参数]"
        plugin_id, command_name = parts[:2]
        raw_arguments = parts[2] if len(parts) == 3 else ""
        return await self._execute_registered_command(
            message=message,
            identity=identity,
            plugin_id=plugin_id,
            command_name=command_name,
            raw_arguments=raw_arguments,
            runtime=runtime,
            allow_alias=True,
        )

    async def _execute_registered_command(
        self,
        *,
        message: InboundMessage,
        identity: ConversationScope,
        plugin_id: str,
        command_name: str,
        raw_arguments: str,
        runtime: RuntimeConfigSnapshot,
        allow_alias: bool,
    ) -> str:
        if plugin_id not in self._manager.running_plugin_ids:
            return "插件当前未运行。"
        item = self._registry.get(f"{plugin_id}:{command_name}")
        if allow_alias and (item is None or item.kind is not ExtensionKind.COMMAND):
            item = self._registry.resolve_command_alias(command_name)
        if item is None or item.kind is not ExtensionKind.COMMAND or item.plugin_id != plugin_id:
            return "没有找到该插件命令。"
        registration = cast(CommandRegistration, item.registration)
        principal = await self._control.principal_for_person(message.person_id)
        privileged = "superuser" in principal.roles
        if not _level_allowed(
            registration.metadata.permission,
            privileged,
        ):
            return "权限不足：当前人物不能执行该插件命令。"
        try:
            payload = _parse_arguments(registration, raw_arguments)
            arguments = registration.argument_model.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            return f"插件命令参数错误：{type(exc).__name__}"
        tool_runtime = ToolRuntime(
            inbound=message,
            conversation_key=plugin_conversation_key(message, identity),
            trigger_message_id=message.message_id,
            trigger_event_id=message.source_event_id,
            actor_is_superuser=privileged,
            runtime_config=runtime,
            origin=TurnOrigin.USER_MESSAGE,
            tools_closed=True,
        )
        try:
            async with asyncio.timeout(registration.metadata.timeout_seconds):
                async with self._scope(item.plugin_id, tool_runtime):
                    result = await registration.handler(arguments)
        except TimeoutError:
            return "插件命令执行超时。"
        except Exception as exc:
            return f"插件命令执行失败：{type(exc).__name__}"
        if result.data.get("pending") is True:
            work_id = result.data.get("work_id")
            return f"任务已接纳，编号：{work_id}。" if work_id else "任务尚未取得有效编号。"
        return (
            result.text
            or result.detail
            or (json.dumps(result.data, ensure_ascii=False) if result.data else "插件命令已完成。")
        )

    @asynccontextmanager
    async def _scope(self, plugin_id: str, runtime: ToolRuntime):  # type: ignore[no-untyped-def]
        if self._invocation_scope is None:
            yield
            return
        scope = self._invocation_scope(plugin_id, runtime)
        enter = getattr(scope, "__aenter__", None)
        leave = getattr(scope, "__aexit__", None)
        if not callable(enter) or not callable(leave):
            raise RuntimeError("plugin invocation scope must be an async context manager")
        await enter()
        try:
            yield
        finally:
            await leave(None, None, None)


def _parse_arguments(registration: CommandRegistration, raw: str) -> object:
    if not raw:
        return {}
    stripped = raw.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    fields = registration.argument_model.model_fields
    if len(fields) == 1:
        return {next(iter(fields)): stripped}
    result: dict[str, object] = {}
    for token in shlex.split(stripped):
        if "=" not in token:
            raise ValueError("multi-field command arguments must use key=value")
        key, value = token.split("=", 1)
        result[key] = value
    return result


def _level_allowed(level: PermissionLevel, superuser: bool) -> bool:
    return level is PermissionLevel.USER or (level is PermissionLevel.SUPERUSER and superuser)


__all__ = ["PluginCommandAdapter"]
