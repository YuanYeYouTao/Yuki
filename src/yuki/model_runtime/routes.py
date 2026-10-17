"""Validated task-to-profile routing."""

from __future__ import annotations

from yuki.model_runtime.models import ModelCapability, ModelProfile, ModelRoute, ModelTask
from yuki.model_runtime.profiles import ModelProfileCatalog


class ModelRouter:
    """Resolve one task without fallback or model-name guessing."""

    def __init__(self, catalog: ModelProfileCatalog) -> None:
        self._catalog = catalog

    @property
    def catalog(self) -> ModelProfileCatalog:
        return self._catalog

    def route(
        self,
        task: ModelTask,
        *,
        required_capabilities: frozenset[ModelCapability] = frozenset(),
    ) -> tuple[ModelRoute, ModelProfile]:
        route = self._catalog.routes[task]
        profile = self._catalog.profiles[route.profile_id]
        required = route.required_capabilities.union(required_capabilities)
        unavailable = required.difference(profile.capabilities)
        if unavailable:
            names = ", ".join(sorted(item.value for item in unavailable))
            raise ValueError(f"profile {profile.id} does not support: {names}")
        return route, profile
