"""Provider registry and immutable unified tool catalog snapshots."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

from qq_ai_bot.capabilities.models import CapabilityDescriptor

_MODEL_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


def safe_model_tool_name(*parts: str, maximum: int = 64) -> str:
    """Build a provider-neutral model name with a stable collision suffix."""

    raw = "__".join(part.strip() for part in parts if part.strip())
    normalized = _MODEL_NAME.sub("_", raw).strip("_") or "tool"
    if len(normalized) <= maximum:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{normalized[: maximum - len(digest) - 2]}__{digest}"


class ToolProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def descriptors(self, context: Any) -> tuple[CapabilityDescriptor, ...]: ...

    async def refresh(self, *, force: bool = False) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class UnifiedToolCatalogEntry:
    descriptor: CapabilityDescriptor


@dataclass(frozen=True, slots=True)
class UnifiedToolCatalog:
    entries: tuple[UnifiedToolCatalogEntry, ...]

    def by_model_name(self, name: str) -> UnifiedToolCatalogEntry | None:
        return next((item for item in self.entries if item.descriptor.model_name == name), None)


@dataclass(frozen=True, slots=True)
class DescriptorRegistrySnapshot:
    """Context-free catalog before per-turn authority projection."""

    catalog: UnifiedToolCatalog

    def entry(self, capability_id: str) -> UnifiedToolCatalogEntry | None:
        return self.catalog.by_model_name(capability_id)


@dataclass(frozen=True, slots=True)
class AuthorizedCatalogSnapshot:
    """Per-turn execution grants over the complete catalog."""

    catalog: UnifiedToolCatalog
    requestable_ids: frozenset[str]
    hidden_ids: frozenset[str] = frozenset()

    def entry(self, capability_id: str) -> UnifiedToolCatalogEntry | None:
        if capability_id not in self.requestable_ids:
            return None
        return self.catalog.by_model_name(capability_id)


class ToolProviderRegistry:
    """Own provider lifecycle and reject ambiguous model-facing names centrally."""

    def __init__(self) -> None:
        self._providers: dict[str, ToolProvider] = {}

    def register(self, provider: ToolProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"duplicate tool provider: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def providers(self) -> tuple[ToolProvider, ...]:
        return tuple(self._providers.values())

    def provider(self, provider_id: str) -> ToolProvider | None:
        return self._providers.get(provider_id)

    def descriptor(self, model_name: str, context: Any) -> CapabilityDescriptor | None:
        entry = self.catalog(context).by_model_name(model_name)
        return entry.descriptor if entry is not None else None

    def binding(self, model_name: str, context: Any) -> Any:
        descriptor = self.descriptor(model_name, context)
        return descriptor.binding if descriptor is not None else None

    def catalog(self, context: Any) -> UnifiedToolCatalog:
        entries: list[UnifiedToolCatalogEntry] = []
        names: set[str] = set()
        canonical_names: set[str] = set()
        for provider in self._providers.values():
            for descriptor in provider.descriptors(context):
                if descriptor.model_name in names:
                    raise ValueError(f"duplicate model capability: {descriptor.model_name}")
                if descriptor.canonical_name in canonical_names:
                    raise ValueError(f"duplicate canonical capability: {descriptor.canonical_name}")
                names.add(descriptor.model_name)
                canonical_names.add(descriptor.canonical_name)
                entries.append(UnifiedToolCatalogEntry(descriptor=descriptor))
        entries.sort(key=lambda item: item.descriptor.model_name)
        return UnifiedToolCatalog(tuple(entries))

    async def refresh(self, *, force: bool = False) -> None:
        for provider in self._providers.values():
            await provider.refresh(force=force)

    async def close(self) -> None:
        for provider in reversed(self._providers.values()):
            await provider.close()
