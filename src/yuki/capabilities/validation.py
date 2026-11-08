"""Host-side JSON Schema validation before any provider binding (R3 §9)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema._utils import find_additional_properties
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
            detail="工具声明无法校验，未执行。",
        )
    validator = validators.get(capability_id)
    if validator is None:
        return CapabilityValidationResult(
            ok=False,
            error_category=UNDECLARED_TOOL,
            detail="本轮没有声明这个工具，未执行。",
        )
    try:
        payload = json.loads(arguments_json)
    except json.JSONDecodeError:
        return CapabilityValidationResult(
            ok=False,
            error_category=TOOL_INPUT_VALIDATION_FAILED,
            detail="参数必须是 JSON 对象。",
        )
    if not isinstance(payload, dict):
        return CapabilityValidationResult(
            ok=False,
            error_category=TOOL_INPUT_VALIDATION_FAILED,
            detail="参数必须是 JSON 对象。",
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
                detail="参数不符合工具声明。",
            )
        raise
    return CapabilityValidationResult(ok=True)


def _validation_detail(error: ValidationError, *, depth: int = 0) -> str:
    """One readable sentence: which parameter, what is expected; never echo values."""
    location = ".".join(
        segment[:64] if isinstance(segment, str) else f"[{segment}]"
        for segment in error.absolute_path
    ).replace(".[", "[")[:256]
    category = str(error.validator)
    expected = error.validator_value

    def names(values: Iterable[object]) -> str:
        return "、".join(str(value)[:64] for value in list(values)[:8])

    if category == "required" and isinstance(expected, list):
        # Names declared in this schema, not the exception message, which may
        # contain arbitrary instance keys or values.
        instance = error.instance if isinstance(error.instance, dict) else {}
        requirement = "缺少必填参数 " + names(name for name in expected if name not in instance)
    elif category == "type":
        requirement = "类型应为 " + (
            names(expected) if isinstance(expected, list) else str(expected)
        )
    elif category == "enum" and isinstance(expected, list):
        requirement = "只能取 " + names(
            json.dumps(value, ensure_ascii=False)
            for value in expected
            if value is None
            or isinstance(value, (int, float, bool))
            or (isinstance(value, str) and len(value) <= 80)
        )
    elif category == "const":
        requirement = "只能取 " + json.dumps(expected, ensure_ascii=False)[:80]
    elif category in {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}:
        label = {
            "minimum": "不能小于",
            "maximum": "不能大于",
            "minLength": "长度不能少于",
            "maxLength": "长度不能超过",
            "minItems": "至少需要项数",
            "maxItems": "最多允许项数",
        }[category]
        requirement = f"{label} {expected}"
    elif category in {"additionalProperties", "unevaluatedProperties"}:
        fields = list(
            find_additional_properties(cast(Any, error.instance), cast(Any, error.schema))
        )
        requirement = "不接受参数 " + names(fields)
    elif category in {"anyOf", "oneOf"} and isinstance(expected, list):
        # Single-key objects (target, sources, wait conditions) list their keys.
        shapes = [
            names(branch.get("required") or branch.get("properties", {}))
            for branch in expected
            if isinstance(branch, dict)
        ]
        if shapes and all(shapes):
            requirement = "须且只能给出以下一种：" + "；".join(shapes[:8])
        elif depth < 2 and error.context:
            requirement = (
                "须符合其中一种形式（"
                + "；".join(
                    _validation_detail(child, depth=depth + 1) for child in error.context[:3]
                )
                + "）"
            )
        else:
            requirement = "须符合声明的其中一种形式"
    else:
        requirement = "不符合声明的约束"
    return (f"参数 {location}：{requirement}" if location else requirement)[:1024]
