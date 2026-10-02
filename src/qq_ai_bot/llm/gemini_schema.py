"""Project JSON Schema to the explicit Gemini Schema wire dialect.

The portable Schema subset is type/description/properties/required/items/enum/title.
Local validation retains the original schema, including extra-property, length and
numeric constraints. References are expanded without mutating that original.
See https://ai.google.dev/api/caching#Schema and the Google content.proto Schema.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from qq_ai_bot.llm.base import LLMUnsupportedFeatureError

_FIELDS = frozenset({"type", "description", "properties", "required", "items", "enum", "title"})
_COMPOSITION = frozenset({"anyOf", "oneOf", "allOf", "not", "if", "then", "else"})
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean"})


def response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert the wire view; reject references that cannot be finitely represented."""

    def resolve(ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise LLMUnsupportedFeatureError(
                "Gemini responseSchema requires local schema references"
            )
        value: Any = schema
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(value, dict) or token not in value:
                raise LLMUnsupportedFeatureError("Gemini responseSchema reference is unresolved")
            value = value[token]
        if not isinstance(value, dict):
            raise LLMUnsupportedFeatureError("Gemini responseSchema reference is not a schema")
        return value

    def convert(value: Any, references: tuple[str, ...] = ()) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise LLMUnsupportedFeatureError("Gemini responseSchema requires object schema nodes")
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str):
                raise LLMUnsupportedFeatureError("Gemini responseSchema reference is invalid")
            if ref in references or ref == "#":
                raise LLMUnsupportedFeatureError(
                    "Gemini responseSchema cannot expand recursive references"
                )
            if set(value) - {"$ref", "title", "description"}:
                raise LLMUnsupportedFeatureError(
                    "Gemini responseSchema does not support reference constraints"
                )
            resolved = convert(resolve(ref), (*references, ref))
            return {
                **resolved,
                **{key: value[key] for key in ("title", "description") if key in value},
            }
        if _COMPOSITION & value.keys():
            raise LLMUnsupportedFeatureError(
                "Gemini responseSchema does not support composed schemas"
            )
        kind = value.get("type")
        if kind is not None and (not isinstance(kind, str) or kind not in _TYPES):
            raise LLMUnsupportedFeatureError("Gemini responseSchema type is unsupported")
        result = {key: deepcopy(item) for key, item in value.items() if key in _FIELDS}
        if "properties" in value:
            properties = value["properties"]
            if not isinstance(properties, dict):
                raise LLMUnsupportedFeatureError("Gemini responseSchema properties are invalid")
            result["properties"] = {
                name: convert(child, references) for name, child in properties.items()
            }
        if "items" in value:
            result["items"] = convert(value["items"], references)
        return result

    return convert(schema)
