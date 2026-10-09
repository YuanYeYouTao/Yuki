"""Uniform tool results and model-facing result budgeting."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from typing import Any, Protocol

from qq_ai_bot.capabilities.media import MediaResultText, result_images
from qq_ai_bot.capabilities.models import CapabilityDescriptor, CapabilityEffect
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.tool_results.access import ArtifactAccess


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
    # True only when the owning domain verified, under current authority, that
    # this exact request's target state already holds. None means unknown.
    request_postcondition_satisfied: bool | None = None
    uncertain: bool = False
    finalize_after_commit: bool | None = None
    provider_id: str = ""
    tool_name: str = ""
    metadata: dict[str, Any] | None = None
    evidence_state: dict[str, Any] | None = None
    memory_grounding_policy: str | None = None
    images: tuple[ChatImage, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        for name in ("ok", "retryable", "uncertain"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        for name in (
            "mutation_committed",
            "finalize_after_commit",
            "request_postcondition_satisfied",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be a bool or None")

    def model_payload(self) -> dict[str, Any]:
        # Do not even copy pixels while building a public textual projection.
        payload = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "images"}
        if not self.uncertain:
            payload.pop("uncertain")
        if isinstance(self.data, dict) and isinstance(self.data.get("executed"), bool):
            # The coordinator consumes this same fact on short and archived
            # paths. A typed pre-dispatch refusal is not an executed tool.
            payload["executed"] = self.data["executed"]
        process = process_receipt(self)
        if process:
            payload["process"] = process
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
        access: ArtifactAccess | None = None,
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
        item_limit: int | None = None,
        access: ArtifactAccess | None = None,
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
        artifact_access: ArtifactAccess | None = None,
        artifact_access_resolver: Callable[[], ArtifactAccess] | None = None,
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
        self._artifact_access = artifact_access
        self._artifact_access_resolver = artifact_access_resolver

    async def render(self, result: ToolExecutionResult) -> BudgetedToolResult:
        from qq_ai_bot.runtime.effect_outcomes import current_result_capture

        capture = current_result_capture.get()
        if capture is not None:
            capture.outcome = result
            if self._artifacts is None:
                from qq_ai_bot.runtime.work_activation import current_work_control

                control = current_work_control.get()
                if control is not None:
                    self._artifacts = control.repository.database.work_result_store
        media_handle = None
        archive_images = getattr(self._artifacts, "write_media_artifact", None)
        if result.ok and result.images and result.provider_id not in {"core", "artifacts"}:
            try:
                if not callable(archive_images):
                    raise ValueError("media_artifact_unavailable")
                access = (
                    self._artifact_access_resolver()
                    if self._artifact_access_resolver
                    else self._artifact_access
                )
                media_handle = await archive_images(
                    provider_id=result.provider_id,
                    tool_name=result.tool_name,
                    images=result.images,
                    access=access,
                    retention_seconds=self._artifact_retention_seconds,
                )
            except Exception:
                # A screenshot/publication failure cannot erase an already
                # successful remote mutation. Report unread pixels separately.
                result = replace(
                    result,
                    images=(),
                    metadata={
                        **(result.metadata or {}),
                        "media_read": False,
                        "media_error": "media_artifact_unavailable",
                    },
                )
            else:
                result = replace(
                    result,
                    images=tuple(
                        replace(image, source="tool", tool_handle=media_handle)
                        for image in result.images
                    ),
                )
            if capture is not None:
                capture.outcome = result
                capture.artifact_handle = media_handle
        payload = result.model_payload()
        if media_handle:
            payload["media_artifact_handle"] = media_handle
        text = json.dumps(payload, ensure_ascii=False, default=str)
        artifact_page = (
            result.provider_id == "artifacts" and result.tool_name == "read_tool_artifact"
        )
        item_overflow = (
            not artifact_page
            and self._item_limit is not None
            and _largest_collection(result.data) > self._item_limit
        )
        character_overflow = self._max_characters is not None and len(text) > self._max_characters
        # External research is an immutable source, not permanent prompt
        # residency. Readers themselves remain paged model input and must not
        # recursively archive each page into another result.
        external_research = (
            (self._artifact_access is not None or self._artifact_access_resolver is not None)
            and self._artifacts is not None
            and result.tool_name in {"web_search", "read_webpage"}
            and result.data not in (None, {}, "")
        )
        if not item_overflow and not character_overflow and not external_research:
            return BudgetedToolResult(
                text=MediaResultText(text, result.images if result.ok else ()),
                artifact_id=media_handle,
            )
        # The summary/artifact is not the original evidence payload. Never
        # advertise references to content which the following request cannot see.
        payload.pop("evidence_state", None)
        artifact_id: str | None = None
        recursive_artifact_read = (
            result.provider_id == "artifacts" and result.tool_name == "read_tool_artifact"
        )
        if self._artifacts is not None and not recursive_artifact_read:
            # Short receipts need no archive identity; resolve it only before a write.
            access = (
                self._artifact_access_resolver()
                if self._artifact_access_resolver is not None
                else self._artifact_access
            )
            try:
                artifact_id = await self._artifacts.write_artifact(
                    provider_id=result.provider_id,
                    tool_name=result.tool_name,
                    content=text,
                    media_type="application/json",
                    retention_seconds=self._artifact_retention_seconds,
                    access=access,
                )
            except OSError:
                # Optional presentation storage cannot reverse the original effect.
                payload["artifact_error"] = "artifact_unavailable"
                payload["result_unavailable"] = True
            if capture is not None:
                capture.artifact_handle = artifact_id
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
            if payload.get("artifact_error"):
                summary["artifact_error"] = payload["artifact_error"]
                summary["result_unavailable"] = True
        progress = _workspace_progress(result)
        summary.update(_execution_envelope(result))
        if media_handle:
            summary["media_artifact_handle"] = media_handle
        if progress:
            summary["progress"] = progress
        file_page = (
            result.provider_id == "core"
            and result.tool_name == "workspace_read"
            and isinstance(result.data, dict)
            and isinstance(result.data.get("text"), str)
        )
        if file_page:
            # A directory/preview is not file text, and must not look like EOF.
            summary.pop("progress", None)
            summary["data"] = {
                **{
                    key: value
                    for key, value in progress.items()
                    if key not in {"output_preview", "preview_truncated"}
                },
                "read_state": "externalized",
                "text": None,
            }
        rendered = json.dumps(summary, ensure_ascii=False, default=str)
        if self._max_characters is not None and len(rendered) > self._max_characters:
            minimal = {
                "ok": result.ok,
                "truncated": True,
                "original_characters": len(text),
                "artifact_handle": artifact_id,
                "artifact_error": payload.get("artifact_error"),
                "result_unavailable": payload.get("result_unavailable"),
                "media_artifact_handle": media_handle,
                "public_message": result.public_message[:1000] if result.public_message else None,
                "retryable": result.retryable,
                "mutation_committed": result.mutation_committed,
                "finalize_after_commit": result.finalize_after_commit,
                "mode": summary.get("mode"),
                "root_type": summary.get("root_type"),
                "available_operations": summary.get("available_operations"),
                "important_fields": (important or None) if not artifact_id else None,
                "progress": (progress or None) if not file_page else None,
                **({"data": summary["data"]} if file_page else {}),
                **_execution_envelope(result),
            }
            rendered = json.dumps(
                {key: value for key, value in minimal.items() if value is not None},
                ensure_ascii=False,
            )
        return BudgetedToolResult(
            text=MediaResultText(rendered, result.images if result.ok else ()),
            artifact_id=artifact_id,
            truncated=True,
        )


def artifact_page_fits(value: object, max_characters: int) -> bool:
    """Size the exact final read-only envelope against its configured character budget."""
    try:
        payload = ToolExecutionResult(
            ok=True,
            data=value,
            mutation_committed=False,
            provider_id="artifacts",
            tool_name="read_tool_artifact",
        ).model_payload()
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        return len(rendered) <= max_characters
    except (RecursionError, ValueError):
        return False


def process_receipt(result: ToolExecutionResult) -> dict[str, Any]:
    """A successful status read does not mean the observed process succeeded."""
    if (
        result.provider_id != "core"
        or result.tool_name not in {"run_python", "get_code_run", "terminal_exec", "terminal_read"}
        or not isinstance(result.data, dict)
    ):
        return {}
    body = result.data
    if isinstance(body.get("completion"), dict):
        body = body["completion"]
    receipt = {
        key: body[key]
        for key in ("run_id", "status", "pending", "exit_code", "output_lost")
        if key in body and isinstance(body[key], (str, int, bool))
    }
    status = body.get("status")
    exit_code = body.get("exit_code")
    if result.uncertain or body.get("uncertain") or status in {"uncertain", "unknown"}:
        return receipt
    if status in {"failed", "cancelled"} or (
        isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0
    ):
        receipt["succeeded"] = False
    elif (
        status == "succeeded"
        and exit_code in (None, 0)
        and not body.get("pending")
        and not body.get("error")
    ):
        receipt["succeeded"] = True
    return receipt


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
            "size",
            "offset",
            "next_offset",
            "offset_unit",
            "eof",
            "read_state",
            "binary",
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
    process = process_receipt(result)
    if process:
        progress["process"] = process
    return progress


def _execution_envelope(result: ToolExecutionResult) -> dict[str, Any]:
    """Execution state survives every model projection, including minimal output."""
    body = result.data if isinstance(result.data, dict) else {}
    envelope: dict[str, Any] = {
        "ok": result.ok,
        "uncertain": result.uncertain or bool(body.get("uncertain")),
        "retryable": result.retryable,
    }
    for key, value in (
        ("error_code", result.error_code),
        ("mutation_committed", result.mutation_committed),
        ("finalize_after_commit", result.finalize_after_commit),
        *((key, body.get(key)) for key in ("status", "run_id", "pending", "executed")),
    ):
        if value is not None:
            envelope[key] = value
    process = process_receipt(result)
    if process:
        envelope["process"] = process
    return envelope


def normalize_legacy_result(
    value: object,
    *,
    provider_id: str,
    tool_name: str,
) -> ToolExecutionResult:
    """Decode persisted historical receipts; never use for live provider dispatch."""

    if not isinstance(value, (str, dict)):
        raise TypeError("historical receipt must be serialized text or an object")

    payload: object = value
    images = result_images(value)
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
        for key in ("ok", "mutation_committed", "finalize_after_commit", "retryable", "uncertain"):
            if key in raw and raw[key] is not None and type(raw[key]) is not bool:
                raise ValueError(f"invalid_historical_receipt_boolean:{key}")
        ok = raw.pop("ok", True)
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
                "search_memory",
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
                "search_memory",
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
            images=images if ok else (),
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
