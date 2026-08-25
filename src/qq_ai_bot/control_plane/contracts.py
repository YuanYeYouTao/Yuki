"""Stable control-plane DTO facade. Re-exports only; adds no new types."""

from __future__ import annotations

from qq_ai_bot.control_plane.capabilities import (
    CONTROL_CAPABILITY_DESCRIPTORS,
    CONTROL_CAPABILITY_IDS,
    DENIED_CAPABILITY_ID,
    CapabilityFamily,
    CapabilitySensitivity,
    CatalogCapabilityView,
    CatalogSourceKind,
    ControlCapabilityDescriptor,
    control_capability_descriptor,
    is_forbidden_control_capability,
    is_protocol_capability,
    normalize_capability_id,
    project_catalog_capabilities,
    project_source_capability_id,
)
from qq_ai_bot.control_plane.commands import ControlCommand, ControlResult
from qq_ai_bot.control_plane.decision import PolicyDecision, PolicyEffect, decide
from qq_ai_bot.control_plane.json_types import JsonObject, JsonScalar, JsonValue
from qq_ai_bot.control_plane.operations import OperationRef, OperationStatus, StateEpoch
from qq_ai_bot.control_plane.paging import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    Cursor,
    Page,
    PageRequest,
    paginate,
)
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.problems import FROZEN_PROBLEM_CODES, Problem, ProblemCode
from qq_ai_bot.domain.control import DecisionContext, DecisionPrincipal

__all__ = [
    "CONTROL_CAPABILITY_DESCRIPTORS",
    "CONTROL_CAPABILITY_IDS",
    "DEFAULT_PAGE_LIMIT",
    "DENIED_CAPABILITY_ID",
    "FROZEN_PROBLEM_CODES",
    "MAX_PAGE_LIMIT",
    "CapabilityFamily",
    "CapabilitySensitivity",
    "CatalogCapabilityView",
    "CatalogSourceKind",
    "ControlCapabilityDescriptor",
    "ControlCommand",
    "ControlPrincipal",
    "ControlResult",
    "Cursor",
    "DecisionContext",
    "DecisionPrincipal",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "OperationRef",
    "OperationStatus",
    "Page",
    "PageRequest",
    "PolicyDecision",
    "PolicyEffect",
    "PrincipalSource",
    "Problem",
    "ProblemCode",
    "StateEpoch",
    "control_capability_descriptor",
    "decide",
    "is_forbidden_control_capability",
    "is_protocol_capability",
    "normalize_capability_id",
    "paginate",
    "project_catalog_capabilities",
    "project_source_capability_id",
]
