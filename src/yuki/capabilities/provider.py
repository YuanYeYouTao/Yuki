"""Adapters that export existing ChatTool definitions as common descriptors."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from yuki.capabilities.binding import InProcessToolBinding
from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityExposure,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from yuki.capabilities.results import ToolExecutionResult
from yuki.domain.messages import ChatTool
from yuki.sandbox.environment_tools import READ_TOOLS, SANDBOX_TOOLS
from yuki.workspace.tools import WORKSPACE_READ_TOOLS, WORKSPACE_TOOLS

_RESIDENT_YUKI_TOOLS = (
    frozenset(
        {
            "find_contacts",
            "send_message",
            "local_device",
            "get_group_members",
            "read_conversation_history",
            "read_personal_entry",
            "recall_own_message",
            "inspect_conversation_attachment",
            "save_conversation_attachment_to_workspace",
        }
    )
    | SANDBOX_TOOLS
    | WORKSPACE_TOOLS
)
_CORE_METADATA: dict[str, tuple[str, CapabilityEffect, CapabilityRisk]] = {
    **{
        name: (
            "sandbox.read" if name in READ_TOOLS else "sandbox.run",
            CapabilityEffect.READ_STATE if name in READ_TOOLS else CapabilityEffect.WRITE_STATE,
            CapabilityRisk.READ if name in READ_TOOLS else CapabilityRisk.MUTATE,
        )
        for name in SANDBOX_TOOLS
    },
    **{
        name: (
            "workspace.read" if name in WORKSPACE_READ_TOOLS else "workspace.write",
            CapabilityEffect.READ_STATE
            if name in WORKSPACE_READ_TOOLS
            else CapabilityEffect.WRITE_STATE,
            CapabilityRisk.READ if name in WORKSPACE_READ_TOOLS else CapabilityRisk.MUTATE,
        )
        for name in WORKSPACE_TOOLS
    },
    "find_contacts": ("social.contacts", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "read_personal_entry": ("social.entry.read", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "local_device": ("device.local", CapabilityEffect.WRITE_STATE, CapabilityRisk.MUTATE),
    "send_message": ("social.send", CapabilityEffect.PLATFORM_SEND, CapabilityRisk.MUTATE),
    "get_group_members": ("social.members", CapabilityEffect.EXTERNAL_READ, CapabilityRisk.READ),
    "read_conversation_history": (
        "social.history",
        CapabilityEffect.EXTERNAL_READ,
        CapabilityRisk.READ,
    ),
    "recall_own_message": (
        "social.recall",
        CapabilityEffect.PLATFORM_MUTATE,
        CapabilityRisk.MUTATE,
    ),
    "get_my_capabilities": (
        "kernel.authority.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "read_tool_artifact": (
        "kernel.artifact.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "query_chat_history": ("memory.history", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "read_memory": ("memory.read", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "search_memory": (
        "memory.search",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "memory_change": (
        "memory.state.write",
        CapabilityEffect.WRITE_STATE,
        CapabilityRisk.MUTATE,
    ),
    "web_search": ("web.search", CapabilityEffect.EXTERNAL_READ, CapabilityRisk.READ),
    "read_webpage": ("web.read", CapabilityEffect.EXTERNAL_READ, CapabilityRisk.READ),
}

_CORE_USE_WHEN: dict[str, tuple[str, ...]] = {
    "query_chat_history": ("聊天原话", "近期消息", "以前聊过", "附近消息"),
    "read_memory": ("记忆详情", "记忆证据"),
    "get_my_capabilities": ("我能改什么", "权限范围", "有哪些设置"),
    "search_memory": ("记得他", "人物记忆", "群记忆", "自我记忆", "你的经历"),
    "memory_change": ("记住", "忘记", "纠正记忆", "保存记忆"),
    "web_search": ("搜索", "联网", "查资料", "最新新闻", "搜下", "上网"),
    "read_webpage": ("打开网页", "阅读链接", "看这个URL"),
    "send_message": ("发消息", "回复", "表情", "引用这条"),
    "read_tool_artifact": ("读取工具结果", "artifact"),
}


_AUTOMATION_READ = frozenset({"automation_query"})

_CORE_SEARCH_TAGS: dict[str, tuple[str, ...]] = {
    "query_chat_history": ("聊天记录", "原文", "前后几条", "以前聊过"),
    "read_memory": ("事实详情", "证据摘要"),
    "search_memory": (
        "人物记忆",
        "群友记忆",
        "某人",
        "关于他",
        "关于她",
        "偏好",
        "记得",
        "自我记忆",
        "你的经历",
        "你的偏好",
        "群记忆",
        "群整体",
        "群里的情况",
    ),
    "memory_change": (
        "记住",
        "保存记忆",
        "纠正记忆",
        "修改记忆",
        "忘记",
        "撤销",
        "恢复记忆",
    ),
    "web_search": (
        "搜索",
        "联网",
        "网上查",
        "最新",
        "新闻",
        "查资料",
        "查询资料",
        "搜下",
        "搜搜",
        "上网",
    ),
    "read_webpage": ("网页", "链接", "URL", "打开网页", "读取页面", "看这个链接"),
    "send_message": ("发消息", "回复", "表情", "来张图"),
}


class CapabilityProvider(Protocol):
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]: ...


class ChatToolCapabilityProvider:
    """Attach policy metadata to existing domain-owned tool definitions."""

    def __init__(
        self,
        tools: tuple[ChatTool, ...],
        *,
        source: CapabilityTrustSource,
        plugin_read_only: Callable[[str], bool] | None = None,
    ) -> None:
        self._tools = tools
        self._source = source
        self._plugin_read_only = plugin_read_only

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        result: list[CapabilityDescriptor] = []
        for tool in self._tools:
            descriptor = self._descriptor(tool)
            if descriptor is not None:
                result.append(descriptor)
        return tuple(result)

    def _descriptor(self, tool: ChatTool) -> CapabilityDescriptor | None:
        metadata = self._metadata(tool)
        if metadata is None:
            return None
        namespace, effect, risk = metadata
        permissions = frozenset({"superuser"}) if namespace.startswith("qq.") else frozenset()
        aliases = tool.aliases or _CORE_SEARCH_TAGS.get(tool.name, ())
        use_when = tool.use_when or _CORE_USE_WHEN.get(tool.name, ())
        tags = tool.tags or (namespace.split(".")[0], self._source.value)
        return CapabilityDescriptor(
            canonical_name=f"{self._source.value}.{tool.name}",
            model_name=tool.name,
            group=namespace.split(".")[0],
            namespace=namespace,
            aliases=tuple(dict.fromkeys(aliases)),
            use_when=tuple(dict.fromkeys(use_when)),
            input_schema=tool.parameters,
            output_schema={"type": "object"},
            effect=effect,
            risk=risk,
            trust_source=self._source,
            required_permissions=permissions,
            uses_external_data=effect is CapabilityEffect.EXTERNAL_READ,
            cancellable=effect in {CapabilityEffect.READ_STATE, CapabilityEffect.EXTERNAL_READ},
            idempotency=(
                CapabilityIdempotency.IDEMPOTENT
                if risk is CapabilityRisk.READ
                else CapabilityIdempotency.CONDITIONAL
            ),
            exposure=(
                CapabilityExposure.DIRECT_ALWAYS
                if self._source is CapabilityTrustSource.CORE and tool.name in _RESIDENT_YUKI_TOOLS
                else CapabilityExposure.PLANNED
            ),
            schema_version=str(tool.schema_version),
            result_cacheable=tool.result_cacheable,
            tags=tuple(dict.fromkeys(tags)),
        )

    def _metadata(self, tool: ChatTool) -> tuple[str, CapabilityEffect, CapabilityRisk] | None:
        if self._source is CapabilityTrustSource.CORE:
            mapped = _CORE_METADATA.get(tool.name)
            if mapped is None:
                return None
            return mapped
        if self._source is CapabilityTrustSource.AUTOMATION:
            if tool.name == "time_get_current":
                return "kernel.time", CapabilityEffect.READ_STATE, CapabilityRisk.READ
            if tool.name in _AUTOMATION_READ:
                return "automation.read", CapabilityEffect.READ_STATE, CapabilityRisk.READ
            return "automation.write", CapabilityEffect.WRITE_STATE, CapabilityRisk.MUTATE
        namespace = tool.namespace.strip() or f"plugin.{tool.name}"
        read_only = bool(self._plugin_read_only and self._plugin_read_only(tool.name))
        return (
            namespace,
            CapabilityEffect.READ_STATE if read_only else CapabilityEffect.WRITE_STATE,
            CapabilityRisk.READ if read_only else CapabilityRisk.MUTATE,
        )


ToolExecutor = Callable[[str, str, Any], Awaitable[ToolExecutionResult]]
DefinitionFactory = Callable[[Any], tuple[ChatTool, ...]]


class InProcessToolProvider:
    """Expose an existing domain service through provider-neutral bindings."""

    def __init__(
        self,
        *,
        provider_id: str,
        source: CapabilityTrustSource,
        definitions: DefinitionFactory,
        execute: ToolExecutor,
        plugin_read_only: Callable[[str], bool] | None = None,
        bot_aliases: tuple[str, ...] = ("Yuki", "yuki", "由纪"),
    ) -> None:
        self._provider_id = provider_id
        self._source = source
        self._definitions = definitions
        self._execute = execute
        self._plugin_read_only = plugin_read_only
        self._bot_aliases = bot_aliases

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def descriptors(self, context: Any) -> tuple[CapabilityDescriptor, ...]:
        tools = self._definitions(context)
        provider = ChatToolCapabilityProvider(
            tools,
            source=self._source,
            plugin_read_only=self._plugin_read_only,
        )
        by_name = {item.model_name: item for item in provider.descriptors()}
        return tuple(
            self._bound(by_name[tool.name], tool) for tool in tools if tool.name in by_name
        )

    def _bound(
        self,
        descriptor: CapabilityDescriptor,
        tool: ChatTool,
    ) -> CapabilityDescriptor:
        async def invoke(
            arguments: dict[str, object],
            context: ToolInvocationContext,
        ) -> ToolExecutionResult:
            token = current_invocation.set(context)
            try:
                return await self._execute(
                    tool.name, json.dumps(arguments, ensure_ascii=False), context.runtime
                )
            finally:
                current_invocation.reset(token)

        search_tags = (
            _CORE_SEARCH_TAGS.get(tool.name, ())
            if self._source is CapabilityTrustSource.CORE
            else tool.tags
        )
        if tool.name == "search_memory":
            search_tags = (
                *search_tags,
                *(f"{alias}记忆" for alias in self._bot_aliases if alias.strip()),
            )
        return replace(
            descriptor,
            provider_id=self._provider_id,
            provider_tool_name=tool.name,
            description=tool.description,
            compact_description=tool.description[:240],
            aliases=tuple(dict.fromkeys((*descriptor.aliases, *search_tags))),
            tags=tuple(
                dict.fromkeys(
                    (
                        descriptor.namespace_id,
                        self._source.value,
                        *descriptor.tags,
                        *search_tags,
                    )
                )
            ),
            binding=InProcessToolBinding(
                provider_id=self._provider_id,
                tool_name=tool.name,
                handler=invoke,
            ),
            parallel_safe=descriptor.risk is CapabilityRisk.READ,
        )

    async def refresh(self, *, force: bool = False) -> None:
        del force

    async def close(self) -> None:
        return None
