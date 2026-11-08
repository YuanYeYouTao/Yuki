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
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from yuki.capabilities.results import ToolExecutionResult
from yuki.domain.messages import ChatTool

_READ_EFFECTS = frozenset({CapabilityEffect.READ_STATE, CapabilityEffect.EXTERNAL_READ})


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
        aliases = tool.aliases
        use_when = tool.use_when
        tags = tool.tags
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
            schema_version=str(tool.schema_version),
            result_cacheable=tool.result_cacheable,
            tags=tuple(dict.fromkeys(tags)),
        )

    def _metadata(self, tool: ChatTool) -> tuple[str, CapabilityEffect, CapabilityRisk] | None:
        if self._source in {CapabilityTrustSource.CORE, CapabilityTrustSource.AUTOMATION}:
            # Host tools carry their policy on the declaration itself.
            if not tool.namespace or not tool.effect:
                return None
            effect = CapabilityEffect(tool.effect)
            return (
                tool.namespace,
                effect,
                CapabilityRisk.READ if effect in _READ_EFFECTS else CapabilityRisk.MUTATE,
            )
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
    ) -> None:
        self._provider_id = provider_id
        self._source = source
        self._definitions = definitions
        self._execute = execute
        self._plugin_read_only = plugin_read_only

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

        return replace(
            descriptor,
            provider_id=self._provider_id,
            provider_tool_name=tool.name,
            description=tool.description,
            compact_description=tool.description[:240],
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
