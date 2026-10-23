"""Per-turn authorized catalog and validation against one frozen contract.

MainAgentContract owns the fixed model declaration. This runtime projects
local authority without growing that declaration.
"""

from __future__ import annotations

from dataclasses import replace

from yuki.capabilities.catalog import (
    AuthorizedCatalogSnapshot,
    DescriptorRegistrySnapshot,
    UnifiedToolCatalog,
)
from yuki.capabilities.exposure import NO_LONGER_AUTHORIZED
from yuki.capabilities.models import CapabilityDescriptor
from yuki.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from yuki.capabilities.validation import (
    UNDECLARED_TOOL,
    CapabilitySchemaView,
    CapabilityValidationResult,
)
from yuki.runtime.contracts import MemoryCapabilityView


class TurnCapabilityRuntime:
    """Project one turn's current grants over compiled contract schemas."""

    def __init__(
        self,
        *,
        registry: DescriptorRegistrySnapshot,
        policy_context: CapabilityPolicyContext,
        schemas: CapabilitySchemaView,
    ) -> None:
        self._registry = registry
        self._policy_context = policy_context
        self._policy = CapabilityPolicyEngine()
        self._schemas = schemas
        self._authorized = self._project_authorized()

    @property
    def authorized_catalog(self) -> UnifiedToolCatalog:
        return self._authorized.catalog

    def sync_memory_view(self, view: MemoryCapabilityView | None) -> None:
        """Re-project authority when the memory contract revision changes.

        The declaration stays fixed; a transition updates only execution grants.
        """

        current = self._policy_context.memory_view
        current_revision = current.transition_revision if current is not None else None
        next_revision = view.transition_revision if view is not None else None
        if current_revision == next_revision and (current is None) == (view is None):
            return
        self._policy_context = replace(self._policy_context, memory_view=view)
        self._authorized = self._project_authorized()

    def callable_capability_ids(self) -> frozenset[str]:
        return self._authorized.requestable_ids

    def validate_call(self, name: str, arguments_json: str) -> tuple[bool, str | None]:
        """Boolean admission view retained for policy probes, without losing execution detail."""
        result = self.validate_call_result(name, arguments_json)
        return result.ok, result.error_category

    def validate_call_result(self, name: str, arguments_json: str) -> CapabilityValidationResult:
        if self._authorized.catalog.by_model_name(name) is None:
            return CapabilityValidationResult(False, UNDECLARED_TOOL, "tool is not declared")
        if name not in self.callable_capability_ids():
            return CapabilityValidationResult(
                False, NO_LONGER_AUTHORIZED, "current authority does not allow this tool"
            )
        return self._schemas.validate(name, arguments_json)

    def descriptor(self, name: str) -> CapabilityDescriptor | None:
        entry = self._authorized.catalog.by_model_name(name)
        return None if entry is None else entry.descriptor

    def _project_authorized(self) -> AuthorizedCatalogSnapshot:
        entries = tuple(
            entry
            for entry in self._registry.catalog.entries
            if not (entry.descriptor.provider_metadata or {}).get("synthetic")
        )
        visible = self._policy.visible(
            tuple(entry.descriptor for entry in entries),
            self._policy_context,
        )
        visible_names = frozenset(item.model_name for item in visible) & frozenset(
            self._schemas.validators
        )
        catalog = UnifiedToolCatalog(
            entries=entries,
        )
        return AuthorizedCatalogSnapshot(
            catalog=catalog,
            requestable_ids=visible_names,
        )


__all__ = [
    "TurnCapabilityRuntime",
]
