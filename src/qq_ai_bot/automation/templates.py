"""Non-evaluating template resolution for automation step arguments."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_REFERENCE = re.compile(r"^\$\{([a-z][a-z0-9_]{0,31})(\.[a-zA-Z0-9_]+)+\}$")
_BUILTIN = re.compile(r"^\$([a-z][a-z0-9_]{0,63})$")
_INLINE_REFERENCE = re.compile(r"\$\{([a-z][a-z0-9_]{0,31})(\.[a-zA-Z0-9_]+)+\}")
_ALLOWED_BUILTINS = frozenset(
    {
        "creator_user_id",
        "bot_user_id",
        "automation_id",
        "automation_run_id",
        "scheduled_for",
        "actual_started_at",
        "local_time",
        "current_group_id",
    }
)


class TemplateError(ValueError):
    """Raised when a template tries to escape the supported value graph."""


def referenced_steps(value: Any) -> frozenset[str]:
    references: set[str] = set()
    _collect(value, references)
    return frozenset(references)


def validate_templates(value: Any) -> None:
    """Reject unknown built-ins and malformed step-reference syntax at creation."""

    if isinstance(value, dict):
        for item in value.values():
            validate_templates(item)
        return
    if isinstance(value, list):
        for item in value:
            validate_templates(item)
        return
    if not isinstance(value, str):
        return
    builtin = _BUILTIN.fullmatch(value)
    if builtin and builtin.group(1) not in _ALLOWED_BUILTINS:
        raise TemplateError(f"未知内置变量：${builtin.group(1)}")
    if "${" in value:
        remainder = _INLINE_REFERENCE.sub("", value)
        if "${" in remainder:
            raise TemplateError("模板引用格式无效")


def resolve_templates(
    value: Any,
    *,
    builtins: Mapping[str, Any],
    step_outputs: Mapping[str, Any],
) -> Any:
    """Resolve only exact built-ins and dotted prior-step fields; never evaluate code."""

    if isinstance(value, dict):
        return {
            key: resolve_templates(item, builtins=builtins, step_outputs=step_outputs)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_templates(item, builtins=builtins, step_outputs=step_outputs) for item in value
        ]
    if not isinstance(value, str):
        return value
    builtin = _BUILTIN.fullmatch(value)
    if builtin:
        name = builtin.group(1)
        if name not in builtins:
            raise TemplateError(f"未知内置变量：${name}")
        return builtins[name]
    reference = _REFERENCE.fullmatch(value)
    if reference:
        return _resolve_reference(reference.group(0), step_outputs)
    if "${" not in value:
        return value
    rendered = _INLINE_REFERENCE.sub(
        lambda match: str(_resolve_reference(match.group(0), step_outputs)),
        value,
    )
    if "${" in rendered:
        raise TemplateError("模板引用格式无效")
    return rendered


def _resolve_reference(reference: str, step_outputs: Mapping[str, Any]) -> Any:
    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise TemplateError("模板引用格式无效")
    path = reference[2:-1].split(".")
    current: Any = step_outputs.get(path[0])
    if current is None and path[0] not in step_outputs:
        raise TemplateError(f"步骤 {path[0]} 尚无输出")
    for part in path[1:]:
        if not isinstance(current, Mapping) or part not in current:
            raise TemplateError(f"模板字段不存在：{reference}")
        current = current[part]
    if isinstance(current, dict | list):
        raise TemplateError("模板字段必须是标量，不能展开对象或数组")
    return current


def _collect(value: Any, references: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _collect(item, references)
    elif isinstance(value, list):
        for item in value:
            _collect(item, references)
    elif isinstance(value, str):
        for match in _INLINE_REFERENCE.finditer(value):
            references.add(match.group(1))
