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
from qq_ai_bot.capabilities.exposure import (
    NO_LONGER_AUTHORIZED,
    DeclaredSchemaLedger,
    ExposurePlan,
    stable_exposure_plan,
)
from qq_ai_bot.capabilities.models import CapabilityDescriptor
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from qq_ai_bot.capabilities.validation import (
    TOOL_INPUT_VALIDATION_FAILED,
    JsonSchemaCapabilityValidator,
)
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import CapabilityExposureSnapshot, MemoryCapabilityView


class TurnCapabilityRuntime:
    """Owns one turn's authorized catalog revision, exposure and callable set."""

    def __init__(
        self,
        *,
        registry: DescriptorRegistrySnapshot,
        authority: TurnAuthority,
        scene: TurnSceneFacts,
        memory_view: MemoryCapabilityView | None,
        policy_context: CapabilityPolicyContext,
        append_only: bool,
    ) -> None:
        self._registry = registry
        self._authority = authority
        self._scene = scene
        self._memory_view = memory_view
        self._policy_context = policy_context
        self._policy = CapabilityPolicyEngine()
        self._validator = JsonSchemaCapabilityValidator()
        self._quarantined: frozenset[str] = frozenset()
        self._authorized = self._project_authorized()
        self._ledger = DeclaredSchemaLedger(
            registry_revision=registry.revision,
            append_only=append_only,
        )
        self._plan: ExposurePlan | None = None
        self._restart_provider_chain = False
        self._exclusive_write = bool(memory_view and memory_view.exclusive_namespace)
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

        Exclusive write and locator-read escalations increment
        ``transition_revision``. The declaration stays fixed and only the
        callable set changes.
        """

        current_revision = (
            self._memory_view.transition_revision if self._memory_view is not None else None
        )
        next_revision = view.transition_revision if view is not None else None
        if current_revision == next_revision and (self._memory_view is None) == (view is None):
            return
        self._memory_view = view
        self._exclusive_write = bool(view is not None and view.exclusive_namespace)
        self._policy_context = replace(self._policy_context, memory_view=view)
        self._authorized = self._project_authorized()
        plan = stable_exposure_plan(
            catalog=self._authorized.catalog,
            requestable_ids=self._authorized.requestable_ids,
            memory_view=self._memory_view,
        )
        self._plan = plan
        self._apply_plan(plan)

    def exposure_snapshot(self) -> CapabilityExposureSnapshot:
        return self._ledger.snapshot()

    def callable_capability_ids(self) -> frozenset[str]:
        return frozenset(self._ledger.callable_ids)

    def definitions(self) -> tuple[ChatTool, ...]:
        return tuple(sorted(self._ledger.declared_tools(), key=lambda item: item.name))

    def initial_exposure(self) -> CapabilityExposureSnapshot:
        self._plan = stable_exposure_plan(
            catalog=self._authorized.catalog,
            requestable_ids=self._authorized.requestable_ids,
            memory_view=self._memory_view,
        )
        self._apply_plan(self._plan)
        return self._ledger.snapshot()

    async def prepare_initial_exposure(self) -> CapabilityExposureSnapshot:
        """Declare the complete stable catalog before the first model request."""

        return self.initial_exposure()

    def validate_call(self, name: str, arguments_json: str) -> tuple[bool, str | None]:
        if name not in self._ledger.declared:
            return False, "undeclared_tool"
        if name not in self._ledger.callable_ids:
            return False, NO_LONGER_AUTHORIZED
        result = self._validator.validate(name, arguments_json)
        if not result.ok:
            return False, result.error_category or TOOL_INPUT_VALIDATION_FAILED
        return True, None

    def mark_side_effect(self) -> None:
        self._ledger.had_side_effect = True

    def can_rebuild_provider_chain(self) -> bool:
        return not self._ledger.had_side_effect

    def rebuild_after_schema_conflict(self) -> bool:
        """Restart the declared set only when no side effect has committed."""

        if self._ledger.had_side_effect or self._plan is None:
            return False
        self._ledger = DeclaredSchemaLedger(
            registry_revision=self._registry.revision,
            append_only=self._ledger.append_only,
        )
        self._restart_provider_chain = True
        return self._apply_plan(self._plan) is None

    def consume_provider_chain_restart(self) -> bool:
        restart = self._restart_provider_chain
        self._restart_provider_chain = False
        return restart

    def descriptor(self, name: str) -> CapabilityDescriptor | None:
        entry = self._authorized.catalog.by_model_name(name)
        return None if entry is None else entry.descriptor

    def requested_exclusive_write(self) -> bool:
        return self._exclusive_write

    def _apply_plan(self, plan: ExposurePlan) -> str | None:
        return self._ledger.declare(
            plan.entries,
            callable_ids=plan.callable_ids,
        )

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
