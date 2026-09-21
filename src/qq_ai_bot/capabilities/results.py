"""Uniform tool results and model-facing result budgeting."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from qq_ai_bot.capabilities.models import CapabilityDescriptor, CapabilityEffect


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    ok: bool
    data: Any = None
    error: str | None = None
    public_message: str | None = None
    retryable: bool = False
    mutation_committed: bool | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Provider-neutral result returned by every ToolBinding."""

    ok: bool
    data: Any = None
    content: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None
    public_message: str | None = None
    retryable: bool = False
    mutation_committed: bool | None = None
    uncertain: bool = False
    finalize_after_commit: bool | None = None
    provider_id: str = ""
    tool_name: str = ""
    metadata: dict[str, Any] | None = None
    evidence_state: dict[str, Any] | None = None
    memory_grounding_policy: str | None = None

    def model_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        if not self.uncertain:
            payload.pop("uncertain")
        return {key: value for key, value in payload.items() if value not in (None, (), "")}


class ToolArtifactWriter(Protocol):
    def configure_retention(self, retention_seconds: int) -> None: ...

    async def write_artifact(
        self,
        *,
        provider_id: str,
        tool_name: str,
        content: str,
        media_type: str,
        retention_seconds: int | None = None,
    ) -> str: ...

    async def read(
        self,
        handle_id: str,
        *,
        operation: str = "text",
        path: tuple[str | int, ...] = (),
        offset: int = 0,
        limit: int = 8000,
        query: str = "",
        max_characters: int = 8000,
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class BudgetedToolResult:
    text: str
    artifact_id: str | None = None
    truncated: bool = False


class ToolResultBudgeter:
    """Bound all tool output without losing the complete value when artifacts are enabled."""

    def __init__(
        self,
        *,
        max_characters: int | None,
        item_limit: int | None = None,
        artifacts: ToolArtifactWriter | None = None,
        artifact_retention_seconds: int | None = None,
    ) -> None:
        if max_characters is not None and max_characters <= 0:
            raise ValueError("tool result budget must be positive or null")
        if item_limit is not None and item_limit <= 0:
            raise ValueError("tool result item limit must be positive or null")
        if artifact_retention_seconds is not None and artifact_retention_seconds <= 0:
            raise ValueError("artifact retention must be positive or null")
        self._max_characters = max_characters
        self._item_limit = item_limit
        self._artifacts = artifacts
        self._artifact_retention_seconds = artifact_retention_seconds

    async def render(self, result: ToolExecutionResult) -> BudgetedToolResult:
        payload = result.model_payload()
        text = json.dumps(payload, ensure_ascii=False, default=str)
        item_overflow = (
            self._item_limit is not None and _largest_collection(payload) > self._item_limit
        )
        character_overflow = self._max_characters is not None and len(text) > self._max_characters
        if not item_overflow and not character_overflow:
            return BudgetedToolResult(text=text)
        # The summary/artifact is not the original evidence payload. Never
        # advertise references to content which the following request cannot see.
        payload.pop("evidence_state", None)
        artifact_id: str | None = None
        recursive_artifact_read = (
            result.provider_id == "artifacts" and result.tool_name == "read_tool_artifact"
        )
        if self._artifacts is not None and not recursive_artifact_read:
            artifact_id = await self._artifacts.write_artifact(
                provider_id=result.provider_id,
                tool_name=result.tool_name,
                content=text,
                media_type="application/json",
                retention_seconds=self._artifact_retention_seconds,
            )
        important: dict[str, object] = {}
        if artifact_id:
            summary = _artifact_manifest(
                payload,
                result=result,
                artifact_id=artifact_id,
                original_characters=len(text),
            )
        else:
            summary = _bounded_payload(payload, item_limit=self._item_limit)
            important = _important_fields(payload)
            if important:
                summary["important_fields"] = important
            summary["truncated"] = True
            summary["original_characters"] = len(text)
        progress = _workspace_progress(result)
        if progress:
            summary["progress"] = progress
        rendered = json.dumps(summary, ensure_ascii=False, default=str)
        if self._max_characters is not None and len(rendered) > self._max_characters:
            minimal = {
                "ok": result.ok,
                "truncated": True,
                "original_characters": len(text),
                "artifact_handle": artifact_id,
                "public_message": result.public_message,
                "retryable": result.retryable,
                "mutation_committed": result.mutation_committed,
                "finalize_after_commit": result.finalize_after_commit,
                "mode": summary.get("mode"),
                "root_type": summary.get("root_type"),
                "available_operations": summary.get("available_operations"),
                "important_fields": (important or None) if not artifact_id else None,
                "progress": progress or None,
            }
            rendered = json.dumps(
                {key: value for key, value in minimal.items() if value is not None},
                ensure_ascii=False,
            )
        return BudgetedToolResult(
            text=rendered,
            artifact_id=artifact_id,
            truncated=True,
        )


def _workspace_progress(result: ToolExecutionResult) -> dict[str, Any]:
    """Keep execution receipts visible even when the full output becomes an artifact."""
    from qq_ai_bot.sandbox.environment_tools import SANDBOX_TOOLS
    from qq_ai_bot.workspace.tools import WORKSPACE_TOOLS

    if (
        result.provider_id != "core"
        or result.tool_name not in SANDBOX_TOOLS | WORKSPACE_TOOLS
        or not isinstance(result.data, dict)
    ):
        return {}
    data = result.data
    progress = {
        key: value
        for key in (
            "run_id",
            "status",
            "pending",
            "exit_code",
            "cursor",
            "next_cursor",
            "output_offset",
            "output_lost",
            "truncated",
            "path",
            "version",
            "artifact_id",
            "error",
            "retryable",
        )
        if key in data
        and (value := data[key]) is not None
        and isinstance(value, (str, int, float, bool))
        and (not isinstance(value, str) or len(value) <= 512)
    }
    output = data.get("output", data.get("text"))
    if isinstance(output, str):
        progress["output_preview"] = output[:1000]
        progress["preview_truncated"] = len(output) > 1000
    return progress


def normalize_legacy_result(
    value: object,
    *,
    provider_id: str,
    tool_name: str,
) -> ToolExecutionResult:
    """Convert old string/dict tool results into the kernel result contract."""

    payload: object = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return ToolExecutionResult(
                ok=True,
                data=value,
                provider_id=provider_id,
                tool_name=tool_name,
            )
    if isinstance(payload, dict):
        raw = dict(payload)
        ok = bool(raw.pop("ok", True))
        error = raw.pop("error_code", raw.pop("error", None))
        public = raw.pop("public_message", raw.pop("detail", None))
        committed_value = raw.pop("mutation_committed", None)
        committed = None if committed_value is None else bool(committed_value)
        finalize_value = raw.pop("finalize_after_commit", None)
        finalize_after_commit = None if finalize_value is None else bool(finalize_value)
        retryable = bool(raw.pop("retryable", False))
        uncertain = bool(raw.pop("uncertain", False))
        evidence = raw.pop("evidence_state", None)
        grounding = raw.pop("memory_grounding_policy", None)
        trusted_grounding = (
            grounding
            if provider_id == "core"
            and tool_name
            in {
                "get_person_memories",
                "get_group_memories",
                "get_self_memories",
                "get_memory_fact",
                "get_memory_evidence",
            }
            and isinstance(grounding, str)
            else None
        )
        trusted_evidence = (
            evidence
            if provider_id == "core"
            and tool_name
            in {
                "get_person_memories",
                "get_group_memories",
                "get_self_memories",
                "get_memory_fact",
                "get_memory_evidence",
                "web_search",
                "read_webpage",
            }
            and isinstance(evidence, dict)
            else None
        )
        data = raw.pop("data", raw if raw else None)
        return ToolExecutionResult(
            ok=ok,
            data=data,
            error_code=str(error) if error is not None else None,
            public_message=str(public) if public is not None else None,
            retryable=retryable,
            mutation_committed=None if uncertain else False if not ok else committed,
            uncertain=uncertain,
            finalize_after_commit=finalize_after_commit if ok else None,
            provider_id=provider_id,
            tool_name=tool_name,
            evidence_state=trusted_evidence,
            memory_grounding_policy=trusted_grounding,
        )
    return ToolExecutionResult(
        ok=True,
        data=payload,
        provider_id=provider_id,
        tool_name=tool_name,
    )


def resolve_mutation_commit(
    result: ToolExecutionResult,
    descriptor: CapabilityDescriptor,
) -> bool | None:
    """Resolve one provider-neutral commit state from result and capability effect."""

    if result.uncertain:
        return None
    if not result.ok:
        return False
    if result.mutation_committed is not None:
        return result.mutation_committed
    if descriptor.effect in {
        CapabilityEffect.READ_STATE,
        CapabilityEffect.EXTERNAL_READ,
    }:
        return False
    if descriptor.effect in {
        CapabilityEffect.WRITE_STATE,
        CapabilityEffect.PLATFORM_MUTATE,
        CapabilityEffect.PLATFORM_SEND,
    }:
        return True
    return False


def _largest_collection(value: object) -> int:
    if isinstance(value, dict):
        return max((len(value), *(_largest_collection(item) for item in value.values())))
    if isinstance(value, (list, tuple)):
        return max((len(value), *(_largest_collection(item) for item in value)))
    return 0


def _bounded_payload(value: object, *, item_limit: int | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"summary": str(value)[:1000]}
    limit = item_limit

    def bounded(item: object) -> object:
        if isinstance(item, list):
            selected = item if limit is None else item[:limit]
            return {
                "total_items": len(item),
                "items": [bounded(value) for value in selected],
            }
        if isinstance(item, tuple):
            return bounded(list(item))
        if isinstance(item, dict):
            pairs = list(item.items())
            selected_pairs = pairs if limit is None else pairs[:limit]
            return {str(key): bounded(child) for key, child in selected_pairs}
        if isinstance(item, str) and len(item) > 1000:
            return f"{item[:1000]}…"
        return item

    return {str(key): bounded(item) for key, item in value.items()}


def _artifact_manifest(
    payload: dict[str, Any],
    *,
    result: ToolExecutionResult,
    artifact_id: str,
    original_characters: int,
) -> dict[str, Any]:
    logical_root = payload.get("data", payload)
    root_type = _json_type(logical_root)
    manifest: dict[str, Any] = {
        "ok": result.ok,
        "retryable": result.retryable,
        "truncated": True,
        "artifact_handle": artifact_id,
        "mode": "text" if isinstance(logical_root, str) else "json",
        "logical_root": "data" if "data" in payload else "$",
        "root_type": root_type,
        "original_characters": original_characters,
    }
    if result.mutation_committed is not None:
        manifest["mutation_committed"] = result.mutation_committed
    if result.finalize_after_commit is not None:
        manifest["finalize_after_commit"] = result.finalize_after_commit
    if result.public_message:
        manifest["public_message"] = result.public_message
    if isinstance(logical_root, dict):
        keys = sorted((str(key) for key in logical_root), key=str.casefold)
        selected = keys[:24]
        manifest["total_children"] = len(keys)
        manifest["children"] = {
            key: _shape_label(logical_root[key]) for key in selected if key in logical_root
        }
        manifest["children_truncated"] = len(selected) < len(keys)
        manifest["available_operations"] = ["inspect", "get", "search"]
    elif isinstance(logical_root, list):
        manifest["total_children"] = len(logical_root)
        manifest["item_types"] = sorted({_json_type(item) for item in logical_root[:24]})
        manifest["available_operations"] = ["inspect", "get", "search"]
    else:
        manifest["available_operations"] = ["text"]
    return manifest


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _shape_label(value: object) -> str:
    kind = _json_type(value)
    if isinstance(value, (dict, list, str)):
        return f"{kind}[{len(value)}]"
    return kind


def _important_fields(value: object) -> dict[str, object]:
    """Project identifiers, status, errors, and URLs before lossy truncation."""

    important: dict[str, object] = {}

    def visit(item: object, path: tuple[str, ...]) -> None:
        if len(important) >= 64:
            return
        if isinstance(item, dict):
            for raw_key, child in item.items():
                key = str(raw_key)
                visit(child, (*path, key))
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item[:20]):
                visit(child, (*path, str(index)))
            return
        if not path:
            return
        key = path[-1].casefold()
        is_important = (
            "url" in key or key == "id" or key.endswith("id") or "status" in key or "error" in key
        )
        if not is_important:
            return
        field_path = ".".join(path)
        important[field_path] = item[:2000] if isinstance(item, str) else item

    visit(value, ())
    return important
