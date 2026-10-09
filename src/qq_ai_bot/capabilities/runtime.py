"""Per-turn authorized catalog, local exposure and call validation.

MainAgentContract owns the fixed model declaration. This runtime projects
local authority without growing that declaration.
"""

from __future__ import annotations

from dataclasses import replace

from qq_ai_bot.capabilities.catalog import (
    AuthorizedCatalogSnapshot,
    DescriptorRegistrySnapshot,
    UnifiedToolCatalog,
)
from qq_ai_bot.capabilities.exposure import NO_LONGER_AUTHORIZED
from qq_ai_bot.capabilities.models import CapabilityDescriptor
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from qq_ai_bot.capabilities.validation import (
    UNDECLARED_TOOL,
    CapabilityValidationResult,
    JsonSchemaCapabilityValidator,
)
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.contracts import MemoryCapabilityView


class TurnCapabilityRuntime:
    """Owns one turn's authorized catalog revision, exposure and callable set."""

    def __init__(
        self,
        *,
        registry: DescriptorRegistrySnapshot,
        policy_context: CapabilityPolicyContext,
    ) -> None:
        self._registry = registry
        self._policy_context = policy_context
        self._policy = CapabilityPolicyEngine()
        self._validator = JsonSchemaCapabilityValidator()
        self._quarantined: frozenset[str] = frozenset()
        self._authorized = self._project_authorized()
        # A freshly constructed runtime grants nothing until its exposure opens.
        self._opened = False
        self._quarantined = frozenset(self._validator.admit(self._authorized.catalog.entries))
        if self._quarantined:
            self._authorized = replace(
                self._authorized,
                requestable_ids=self._authorized.requestable_ids - self._quarantined,
            )

    @property
    def registry_revision(self) -> str:
        return self._registry.revision

    @property
    def authorized_catalog(self) -> UnifiedToolCatalog:
        return self._authorized.catalog

    def sync_memory_view(self, view: MemoryCapabilityView | None) -> None:
        """Re-project authority when the memory contract revision changes.

        The declaration stays fixed; a transition updates only execution grants.
        A transition also opens execution, even before ``initial_exposure``.
        """

        current = self._policy_context.memory_view
        current_revision = current.transition_revision if current is not None else None
        next_revision = view.transition_revision if view is not None else None
        if current_revision == next_revision and (current is None) == (view is None):
            return
        self._policy_context = replace(self._policy_context, memory_view=view)
        self._authorized = self._project_authorized()
        self._opened = True

    def callable_capability_ids(self) -> frozenset[str]:
        return self._authorized.requestable_ids if self._opened else frozenset()

    def definitions(self) -> tuple[ChatTool, ...]:
        return tuple(
            sorted(
                (entry.descriptor.as_chat_tool() for entry in self._authorized.catalog.entries),
                key=lambda item: item.name,
            )
        )

    def initial_exposure(self) -> None:
        """Open the authorized grants of the complete stable catalog."""

        self._opened = True

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
        return self._validator.validate(name, arguments_json)

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
        visible_names = frozenset(item.model_name for item in visible) - self._quarantined
        catalog = UnifiedToolCatalog(
            entries=entries,
            scopes=self._registry.catalog.scopes,
            revision=self._registry.revision,
        )
        return AuthorizedCatalogSnapshot(
            registry_revision=self._registry.revision,
            catalog=catalog,
            requestable_ids=visible_names,
        )


__all__ = [
    "TurnCapabilityRuntime",
]
