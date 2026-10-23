"""Host-side JSON Schema validation before any provider binding (R3 §9)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for
from referencing import Registry

from yuki.capabilities.catalog import UnifiedToolCatalogEntry
from yuki.domain.messages import ChatTool

TOOL_INPUT_VALIDATION_FAILED = "tool_input_validation_failed"
UNDECLARED_TOOL = "undeclared_tool"
SCHEMA_QUARANTINED = "capability_schema_quarantined"


@dataclass(frozen=True, slots=True)
class CapabilityValidationResult:
    """Outcome of validating one call's arguments against its schema."""

    ok: bool
    error_category: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.ok and self.error_category is None:
            raise ValueError("failed validation must carry an error category")


@dataclass(frozen=True, slots=True)
class CapabilitySchemaView:
    """Compiled input schemas of one frozen contract, without execution grants."""

    validators: Mapping[str, Any]
    quarantined: frozenset[str]

    def validate(self, capability_id: str, arguments_json: str) -> CapabilityValidationResult:
        return _validate(self.validators, self.quarantined, capability_id, arguments_json)


@dataclass(slots=True)
class JsonSchemaCapabilityValidator:
    """Compile-and-cache validators at catalog admission; never fetch $ref."""

    _validators: dict[str, Any] = field(default_factory=dict)
    _quarantined: set[str] = field(default_factory=set)

    def admit(self, entries: tuple[UnifiedToolCatalogEntry, ...]) -> tuple[str, ...]:
        """Compile schemas; return quarantined capability ids."""

        return self._admit(
            (entry.descriptor.model_name, entry.descriptor.input_schema) for entry in entries
        )

    def admit_tools(self, tools: tuple[ChatTool, ...]) -> tuple[str, ...]:
        return self._admit((tool.name, tool.parameters) for tool in tools)

    def freeze(self) -> CapabilitySchemaView:
        return CapabilitySchemaView(
            MappingProxyType(dict(self._validators)), frozenset(self._quarantined)
        )

    def _admit(self, schemas: Iterable[tuple[str, Any]]) -> tuple[str, ...]:

        quarantined: list[str] = []
        for capability_id, schema in schemas:
            try:
                validator_cls = validator_for(schema, default=Draft202012Validator)
                validator_cls.check_schema(schema)
                self._validators[capability_id] = validator_cls(schema, registry=Registry())
                self._quarantined.discard(capability_id)
            except (SchemaError, ValueError):
                self._quarantined.add(capability_id)
                self._validators.pop(capability_id, None)
                quarantined.append(capability_id)
        return tuple(quarantined)

    def validate(self, capability_id: str, arguments_json: str) -> CapabilityValidationResult:
        return _validate(self._validators, self._quarantined, capability_id, arguments_json)


def _validate(
    validators: Mapping[str, Any],
    quarantined: set[str] | frozenset[str],
    capability_id: str,
    arguments_json: str,
) -> CapabilityValidationResult:
    if capability_id in quarantined:
        return CapabilityValidationResult(
            ok=False,
            error_category=SCHEMA_QUARANTINED,
            detail="tool schema is not safe to validate",
        )
    validator = validators.get(capability_id)
    if validator is None:
        return CapabilityValidationResult(
            ok=False,
            error_category=UNDECLARED_TOOL,
            detail="tool is not declared for this turn",
        )
    try:
        payload = json.loads(arguments_json)
    except json.JSONDecodeError:
        return CapabilityValidationResult(
            ok=False,
            error_category=TOOL_INPUT_VALIDATION_FAILED,
            detail="arguments must be a JSON object",
        )
    if not isinstance(payload, dict):
        return CapabilityValidationResult(
            ok=False,
            error_category=TOOL_INPUT_VALIDATION_FAILED,
            detail="arguments must be a JSON object",
        )
    try:
        validator.validate(payload)
    except ValidationError as exc:
        return CapabilityValidationResult(
            ok=False,
            error_category=TOOL_INPUT_VALIDATION_FAILED,
            detail=_validation_detail(exc),
        )
    except Exception as exc:
        module = getattr(type(exc), "__module__", "")
        if "jsonschema" in module or "referencing" in module:
            return CapabilityValidationResult(
                ok=False,
                error_category=TOOL_INPUT_VALIDATION_FAILED,
                detail="arguments do not match the declared schema",
            )
        raise
    return CapabilityValidationResult(ok=True)


def _validation_detail(error: ValidationError, *, depth: int = 0) -> str:
    """Describe the frozen schema only, never the invalid instance or its keys."""
    schema_path = tuple(error.absolute_schema_path)
    path: list[str] = []
    for index, segment in enumerate(schema_path):
        if segment == "properties" and index + 1 < len(schema_path):
            field_name = schema_path[index + 1]
            if isinstance(field_name, str):
                path.append(field_name[:64])
        elif segment in {"items", "prefixItems"}:
            path.append("[]")
        elif segment in {"additionalProperties", "patternProperties"}:
            path.append("*")
    location = ".".join(path)[:256] or "$"
    category = str(error.validator)
    expected = error.validator_value
    if category == "required" and isinstance(expected, list):
        # Check names declared in this schema, not the exception message, which
        # may contain arbitrary instance keys or values.
        instance = error.instance if isinstance(error.instance, dict) else {}
        missing = [name[:64] for name in expected if name not in instance][:8]
        requirement = "required fields: " + json.dumps(missing[:8], ensure_ascii=False)
    elif category == "type":
        requirement = "expected type: " + json.dumps(expected, ensure_ascii=False)
    elif category == "enum" and isinstance(expected, list):
        values = [
            value
            for value in expected[:8]
            if value is None
            or isinstance(value, (int, float, bool))
            or (isinstance(value, str) and len(value) <= 80)
        ]
        label = "allowed values" if len(values) == len(expected) else "allowed values (partial)"
        requirement = label + ": " + json.dumps(values, ensure_ascii=False)
    elif category in {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}:
        requirement = f"{category}: {expected}"
    elif category == "additionalProperties":
        requirement = "undeclared fields are not allowed"
    elif category in {"anyOf", "oneOf"} and depth < 2 and error.context:
        requirement = "declared alternatives: " + "; ".join(
            _validation_detail(child, depth=depth + 1) for child in error.context[:3]
        )
    else:
        requirement = "must satisfy the declared constraint"
    return f"{location}: {category}; {requirement}"[:1024]
