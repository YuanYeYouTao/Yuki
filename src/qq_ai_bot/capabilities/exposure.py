"""Stable tool declarations and per-turn execution grants."""

from __future__ import annotations

from dataclasses import dataclass, field

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.models import CapabilityEffect
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.contracts import CapabilityExposureSnapshot, MemoryCapabilityView

CONDITIONAL_KERNEL_TOOLS = frozenset({"get_my_capabilities", "read_tool_artifact"})
SCHEMA_REVISION_CONFLICT = "capability_schema_revision_conflict"
NO_LONGER_AUTHORIZED = "capability_no_longer_authorized"


@dataclass(frozen=True, slots=True)
class DeclaredSchemaRecord:
    capability_id: str
    schema_fingerprint: str
    chat_tool: ChatTool


@dataclass(slots=True)
class DeclaredSchemaLedger:
    """Per-turn declared schemas vs currently callable ids."""

    registry_revision: str
    append_only: bool
    declared: dict[str, DeclaredSchemaRecord] = field(default_factory=dict)
    callable_ids: set[str] = field(default_factory=set)
    schema_token_total: int = 0
    conflict: str | None = None
    had_side_effect: bool = False

    def declare(
        self,
        entries: tuple[UnifiedToolCatalogEntry, ...],
        *,
        extra_tools: tuple[ChatTool, ...] = (),
        callable_ids: frozenset[str],
    ) -> str | None:
        """Merge stable schemas while changing grants only at the execution boundary."""

        if self.conflict is not None:
            return self.conflict
        if self.append_only:
            for entry in entries:
                tool = entry.descriptor.as_chat_tool()
                record = DeclaredSchemaRecord(
                    capability_id=entry.descriptor.model_name,
                    schema_fingerprint=entry.revision,
                    chat_tool=tool,
                )
                existing = self.declared.get(record.capability_id)
                if (
                    existing is not None
                    and existing.schema_fingerprint != record.schema_fingerprint
                ):
                    self.conflict = SCHEMA_REVISION_CONFLICT
                    return self.conflict
                if existing is None:
                    self.declared[record.capability_id] = record
                    self.schema_token_total += entry.estimated_schema_tokens
            for tool in extra_tools:
                if tool.name not in self.declared:
                    self.declared[tool.name] = DeclaredSchemaRecord(
                        capability_id=tool.name,
                        schema_fingerprint="kernel",
                        chat_tool=tool,
                    )
        else:
            self.declared = {
                entry.descriptor.model_name: DeclaredSchemaRecord(
                    capability_id=entry.descriptor.model_name,
                    schema_fingerprint=entry.revision,
                    chat_tool=entry.descriptor.as_chat_tool(),
                )
                for entry in entries
            }
            for tool in extra_tools:
                self.declared[tool.name] = DeclaredSchemaRecord(
                    capability_id=tool.name,
                    schema_fingerprint="kernel",
                    chat_tool=tool,
                )
            self.schema_token_total = sum(entry.estimated_schema_tokens for entry in entries)
        self.callable_ids = set(callable_ids)
        return None

    def declared_tools(self) -> tuple[ChatTool, ...]:
        return tuple(record.chat_tool for record in self.declared.values())

    def snapshot(self) -> CapabilityExposureSnapshot:
        revision = int(self.registry_revision[:8], 16) if self.registry_revision else 0
        return CapabilityExposureSnapshot(
            revision=revision,
            exposed_capability_ids=tuple(sorted(self.declared)),
            requestable_capability_ids=tuple(sorted(self.callable_ids)),
            schema_token_estimate=self.schema_token_total,
        )


@dataclass(frozen=True, slots=True)
class ExposurePlan:
    entries: tuple[UnifiedToolCatalogEntry, ...]
    callable_ids: frozenset[str]


def stable_exposure_plan(
    *,
    catalog: UnifiedToolCatalog,
    requestable_ids: frozenset[str],
    memory_view: MemoryCapabilityView | None,
) -> ExposurePlan:
    """Declare every admitted tool; only the execution grants may change."""

    entries = catalog.entries
    callable_ids = frozenset(
        entry.descriptor.model_name
        for entry in entries
        if entry.descriptor.model_name in requestable_ids
    )
    if memory_view is not None and memory_view.exclusive_namespace:
        callable_ids = _restrict_exclusive_write(entries, callable_ids, memory_view)
    return ExposurePlan(entries=entries, callable_ids=callable_ids)


def _restrict_exclusive_write(
    entries: tuple[UnifiedToolCatalogEntry, ...],
    requestable_ids: frozenset[str],
    memory_view: MemoryCapabilityView,
) -> frozenset[str]:
    allowed: set[str] = set()
    exclusive = memory_view.exclusive_namespace
    eager = set(memory_view.eager_namespaces)
    for entry in entries:
        name = entry.descriptor.model_name
        if name not in requestable_ids:
            continue
        namespace = entry.descriptor.namespace_id
        effect = entry.descriptor.effect
        if namespace == exclusive:
            allowed.add(name)
        elif namespace in eager and effect is not CapabilityEffect.WRITE_STATE:
            allowed.add(name)
        elif name in CONDITIONAL_KERNEL_TOOLS and effect is not CapabilityEffect.WRITE_STATE:
            allowed.add(name)
    return frozenset(allowed)
