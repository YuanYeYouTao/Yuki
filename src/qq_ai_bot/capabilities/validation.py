"""Host-side JSON Schema validation before any provider binding (R3 §9)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.models import CapabilityDescriptor

TOOL_INPUT_VALIDATION_FAILED = "tool_input_validation_failed"
UNDECLARED_TOOL = "undeclared_tool"
SCHEMA_QUARANTINED = "capability_schema_quarantined"
_REMOTE_REF = re.compile(r"^https?://", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CapabilityValidationResult:
    """Outcome of validating one call's arguments against its schema."""

    ok: bool
    error_category: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.ok and self.error_category is None:
            raise ValueError("failed validation must carry an error category")


@dataclass(slots=True)
class JsonSchemaCapabilityValidator:
    """Compile-and-cache validators at catalog admission; never fetch $ref."""

    _validators: dict[str, Any] = field(default_factory=dict)
    _quarantined: set[str] = field(default_factory=set)

    def admit(self, entries: tuple[UnifiedToolCatalogEntry, ...]) -> tuple[str, ...]:
        """Compile schemas; return quarantined capability ids."""

        quarantined: list[str] = []
        for entry in entries:
            capability_id = entry.descriptor.model_name
            schema = _with_lifted_defs(entry.descriptor.input_schema)
            try:
                _assert_safe_schema(schema)
                validator_cls = validator_for(schema, default=Draft202012Validator)
                validator_cls.check_schema(schema)
                self._validators[capability_id] = validator_cls(schema)
                self._quarantined.discard(capability_id)
            except (SchemaError, ValueError):
                self._quarantined.add(capability_id)
                self._validators.pop(capability_id, None)
                quarantined.append(capability_id)
        return tuple(quarantined)

    def validate(self, capability_id: str, arguments_json: str) -> CapabilityValidationResult:
        if capability_id in self._quarantined:
            return CapabilityValidationResult(
                ok=False,
                error_category=SCHEMA_QUARANTINED,
                detail="tool schema is not safe to validate",
            )
        validator = self._validators.get(capability_id)
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

    def is_quarantined(self, capability_id: str) -> bool:
        return capability_id in self._quarantined


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


def _assert_safe_schema(schema: dict[str, object]) -> None:
    ref = schema.get("$ref")
    if isinstance(ref, str) and _REMOTE_REF.match(ref):
        raise ValueError("remote $ref is not allowed")
    if "$ref" in schema and isinstance(ref, str) and ref.startswith("#"):
        pass
    elif "$ref" in schema:
        raise ValueError("unsupported $ref")
    for key in ("items", "additionalProperties", "contains", "propertyNames", "not"):
        nested = schema.get(key)
        if isinstance(nested, dict):
            _assert_safe_schema(nested)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        nested = schema.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    _assert_safe_schema(item)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for item in properties.values():
            if isinstance(item, dict):
                _assert_safe_schema(item)
    defs = schema.get("$defs") or schema.get("definitions")
    if isinstance(defs, dict):
        for item in defs.values():
            if isinstance(item, dict):
                _assert_safe_schema(item)


def _with_lifted_defs(schema: dict[str, object]) -> dict[str, object]:
    """Copy nested ``$defs`` to the schema root so local ``#/$defs/...`` refs resolve."""

    lifted = dict(schema)
    collected: dict[str, object] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            nested = node.get("$defs") or node.get("definitions")
            if isinstance(nested, dict):
                for key, value in nested.items():
                    collected.setdefault(str(key), value)
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(lifted)
    if not collected:
        return lifted
    existing = lifted.get("$defs")
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in collected.items():
        merged.setdefault(key, value)
    lifted["$defs"] = merged
    return lifted


def domain_validate_descriptor(
    descriptor: CapabilityDescriptor, arguments: dict[str, Any]
) -> CapabilityValidationResult:
    del descriptor, arguments
    return CapabilityValidationResult(ok=True)
