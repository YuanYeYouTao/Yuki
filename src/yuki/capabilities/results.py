"""Uniform tool results and model-facing result budgeting."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any, Protocol

from yuki.capabilities.media import MediaResultText, result_images
from yuki.capabilities.models import CapabilityDescriptor, CapabilityEffect
from yuki.domain.messages import ChatImage
from yuki.tool_results.access import ArtifactAccess

if TYPE_CHECKING:
    from yuki.admin.models import RuntimeConfigSnapshot

_NO_PREVIEW = object()


def effective_result_character_budget(snapshot: RuntimeConfigSnapshot | None) -> int | None:
    """Resolve the existing token override and agent display budget in one place."""
    if snapshot is None:
        return None
    tooling = snapshot.tooling
    if tooling is not None and tooling.result_token_budget is not None:
        return tooling.result_token_budget * 4
    return snapshot.agent.tool_result_max_characters


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
    source_event_ids: tuple[int, ...] = ()
    source_privacy_generation: int | None = None
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
        payload = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in {"images", "source_event_ids", "source_privacy_generation"}
        }
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
        source_event_ids: tuple[int, ...] = (),
        source_privacy_generation: int | None = None,
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
        from yuki.runtime.effect_outcomes import current_result_capture

        capture = current_result_capture.get()
        if capture is not None:
            capture.observe_sources(result.source_event_ids, result.source_privacy_generation)
            if capture.source_event_ids:
                result = replace(
                    result,
                    source_event_ids=capture.source_event_ids,
                    source_privacy_generation=capture.source_privacy_generation,
                )
            capture.outcome = result
        media_handle = None
        archive_images = getattr(self._artifacts, "write_media_artifact", None)
        local_media = (
            result.provider_id == "core"
            and result.tool_name == "workspace_inspect"
            and isinstance(result.data, dict)
            and bool(result.data.get("device_id"))
        )
        if (
            result.ok
            and result.images
            and (result.provider_id not in {"core", "artifacts"} or local_media)
        ):
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
                    source_event_ids=result.source_event_ids,
                    source_privacy_generation=result.source_privacy_generation,
                )
            except (OSError, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc) != "tool_artifact_capacity":
                    raise
                # Optional presentation storage cannot reverse the original effect.
                payload["artifact_error"] = (
                    "tool_artifact_capacity"
                    if isinstance(exc, ValueError)
                    else "artifact_unavailable"
                )
            if capture is not None:
                capture.artifact_handle = artifact_id
        # Archiving research does not hide a result which already fits. Keep
        # its original evidence_state and the separately persisted source.
        if not item_overflow and not character_overflow:
            rendered = json.dumps(payload, ensure_ascii=False, default=str)
            if self._max_characters is None or len(rendered) <= self._max_characters:
                return BudgetedToolResult(
                    text=MediaResultText(rendered, result.images if result.ok else ()),
                    artifact_id=artifact_id or media_handle,
                )
        summary = _preview_result(
            payload,
            result=result,
            artifact_id=artifact_id,
            max_characters=self._max_characters,
            item_limit=self._item_limit,
            original_characters=len(text),
        )
        rendered = json.dumps(summary, ensure_ascii=False, default=str)
        return BudgetedToolResult(
            text=MediaResultText(rendered, result.images if result.ok else ()),
            artifact_id=artifact_id,
            truncated=True,
        )


def _preview_result(
    payload: dict[str, Any],
    *,
    result: ToolExecutionResult,
    artifact_id: str | None,
    max_characters: int | None,
    item_limit: int | None,
    original_characters: int,
) -> dict[str, Any]:
    from yuki.tool_results.json_length import (
        json_character_count,
        json_key_text,
        json_string_prefix,
    )

    base = {
        key: value
        for key, value in payload.items()
        if key not in {"data", "content", "metadata", "evidence_state", "memory_grounding_policy"}
    }
    base.update(
        _execution_envelope(result), truncated=True, original_characters=original_characters
    )
    if artifact_id is not None:
        base["artifact_handle"] = artifact_id
    progress = _workspace_progress(result)
    if progress:
        base["progress"] = progress
    root = payload.get("data", payload)
    base["logical_root"] = "data" if "data" in payload else "$"
    # Necessary execution facts are a soft target, just as on the original
    # minimal-result path. Spend only the remainder on optional prose/content.
    message = base.pop("public_message", None)
    budget = max_characters if max_characters is not None else original_characters
    if message is not None:
        fixed = json_character_count({**base, "public_message": ""})
        available = max(0, budget - (fixed or 0))
        base["public_message"] = message[: json_string_prefix(message, available)]
    fixed = json_character_count({**base, "data": {}}) or 0
    available = max(0, budget - fixed)
    cache: dict[int, tuple[object, int, bool]] = {}
    while True:
        value, pending = _preview_json(root, available, item_limit=item_limit, cache=cache)
        if value is _NO_PREVIEW:
            summary = {**base, "preview_error": "artifact_value_too_large", "preview_path": []}
        else:
            summary = {**base, "data": value}
        # An atomic array item may itself be larger than a page. Keep data's
        # array prefix whole, and show that item's actual partial value at its
        # explicit path instead of leaving only an empty list and a manifest.
        for path, _offset in pending:
            if not path or not isinstance(path[-1], int):
                continue
            focus: Any = root
            for part in path:
                focus = (
                    {json_key_text(key): child for key, child in focus.items()}[str(part)]
                    if isinstance(focus, dict)
                    else focus[part]
                )
            overhead = json_character_count({"path": path, "value": ""}) or 0
            remaining = available - (json_character_count(value) or 0) - overhead
            if remaining <= 0:
                break
            fragment, focus_pending = _preview_json(
                focus, remaining, item_limit=item_limit, cache=cache, path=tuple(path)
            )
            if fragment is not _NO_PREVIEW:
                summary["preview"] = {"path": path, "value": fragment}
            pending = [entry for entry in pending if entry[0] != path] + focus_pending
            break
        if artifact_id is not None and pending:
            summary["read_more"] = [
                {"handle": artifact_id, "operation": "get", "path": path, "offset": offset}
                for path, offset in pending
            ]
        size = json_character_count(summary) or 0
        if max_characters is None or size <= max_characters or available == 0:
            return summary
        # Metadata is measured with the actual page. This decreases the content
        # budget strictly, with no retry cap or second persistent page state.
        available = max(0, available - (size - max_characters))


def _preview_json(
    value: object,
    budget: int,
    *,
    item_limit: int | None,
    cache: dict[int, tuple[object, int, bool]],
    path: tuple[str | int, ...] = (),
) -> tuple[object, list[tuple[list[str | int], int]]]:
    from yuki.tool_results.json_length import (
        json_character_count,
        json_key_text,
        json_string_prefix,
    )

    size = json_character_count(value, limit=budget, cache=cache)
    item_overflow = item_limit is not None and _largest_collection(value) > item_limit
    if size is not None and size <= budget and not item_overflow:
        return value, []
    if isinstance(value, str):
        count = json_string_prefix(value, max(0, budget - 2))
        return value[:count], [(list(path), count)] if count < len(value) else []
    if isinstance(value, (list, tuple)):
        selected: list[object] = []
        used = 2
        for child in value:
            if item_limit is not None and len(selected) >= item_limit:
                break
            remaining = budget - used - (2 if selected else 0)
            child_size = json_character_count(child, limit=remaining, cache=cache)
            child_item_overflow = item_limit is not None and _largest_collection(child) > item_limit
            if child_size is None or child_size > remaining or child_item_overflow:
                break
            selected.append(child)
            used += child_size + (2 if len(selected) > 1 else 0)
        pending = [(list(path), len(selected))] if len(selected) < len(value) else []
        if not selected and value and isinstance(value[0], (dict, list, tuple, str)):
            pending.append(([*path, 0], 0))
        return selected, pending
    if isinstance(value, dict):
        # A public JSON object has encoded string keys. Preserve the same last
        # value as the archived JSON when two Python keys encode identically.
        mapping = {json_key_text(key): child for key, child in value.items()}
        selected_mapping: dict[str, object] = {}
        pending = []
        used = 2
        # Partial dictionaries use the reader's casefold order; full fast paths
        # above retain the original value and have no continuation position.
        for text_key in sorted(mapping, key=str.casefold):
            if item_limit is not None and len(selected_mapping) >= item_limit:
                break
            key_size = json_character_count(text_key) or 0
            added = key_size + 2 + (2 if selected_mapping else 0)
            remaining = budget - used - added
            if remaining < 1:
                break
            child, child_pending = _preview_json(
                mapping[text_key],
                remaining,
                item_limit=item_limit,
                cache=cache,
                path=(*path, text_key),
            )
            if child is _NO_PREVIEW:
                break
            child_size = json_character_count(child) or 0
            if child_size > remaining:
                break
            selected_mapping[text_key] = child
            used += added + child_size
            pending.extend(child_pending)
        if len(selected_mapping) < len(mapping):
            pending.append((list(path), len(selected_mapping)))
        return selected_mapping, pending
    if not isinstance(value, (int, float, bool)) and value is not None:
        return _preview_json(str(value), budget, item_limit=item_limit, cache=cache, path=path)
    return _NO_PREVIEW, [(list(path), 0)]


def artifact_page_size(value: object, limit: int | None = None) -> int | None:
    """Count the actual success or error envelope of the Artifact binding."""
    from yuki.tool_results.json_length import json_character_count

    if isinstance(value, dict) and isinstance(value.get("error_code"), str):
        data = {key: item for key, item in value.items() if key not in {"error_code", "detail"}}
        result = ToolExecutionResult(
            ok=False,
            data=data or None,
            error_code=value["error_code"],
            public_message=str(value.get("detail") or "Artifact 读取失败"),
            provider_id="artifacts",
            tool_name="read_tool_artifact",
        )
    else:
        result = ToolExecutionResult(
            ok=True,
            data=value,
            mutation_committed=False,
            provider_id="artifacts",
            tool_name="read_tool_artifact",
        )
    return json_character_count(result.model_payload(), limit=limit)


def artifact_page_fits(value: object, max_characters: int) -> bool:
    size = artifact_page_size(value, max_characters)
    return size is not None and size <= max_characters


def process_receipt(result: ToolExecutionResult) -> dict[str, Any]:
    """A successful status read does not mean the observed process succeeded."""
    if (
        result.provider_id != "core"
        or result.tool_name
        not in {"run_python", "get_code_run", "terminal_exec", "terminal_read", "external_agent"}
        or not isinstance(result.data, dict)
    ):
        return {}
    body = result.data
    if result.tool_name == "external_agent" and body.get("kind") not in {
        "external_agent",
        "acp_prompt",
    }:
        return {}
    if isinstance(body.get("completion"), dict):
        body = body["completion"]
    receipt = {
        key: body[key]
        for key in (
            "run_id",
            "peer_id",
            "device_id",
            "instance_id",
            "operation_id",
            "execution_id",
            "session_id",
            "status",
            "pending",
            "exit_code",
            "output_lost",
        )
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
    from yuki.sandbox.environment_tools import SANDBOX_TOOLS
    from yuki.workspace.tools import WORKSPACE_TOOLS

    if (
        result.provider_id not in {"core", "work_control"}
        or result.tool_name
        not in SANDBOX_TOOLS | WORKSPACE_TOOLS | {"external_agent", "task_control"}
        or not isinstance(result.data, dict)
    ):
        return {}
    data = result.data
    progress = {
        key: value
        for key in (
            "run_id",
            "peer_id",
            "device_id",
            "instance_id",
            "operation_id",
            "execution_id",
            "session_id",
            "permission_requests",
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
            "query_scope",
            "status_scope",
            "observed_at",
            "history_order",
            "history_items_view",
            "history_scope",
            "cursor_parameter",
            "history_available",
            "history_reason",
            "content_omitted",
            "cursor_direction",
            "source_scope",
        )
        if key in data
        and (value := data[key]) is not None
        and isinstance(value, (str, int, float, bool))
        and (not isinstance(value, str) or len(value) <= 512)
    }
    output = data.get("output", data.get("text"))
    for key in ("latest_turn", "selected_turn", "latest_message"):
        if isinstance(data.get(key), dict):
            progress[key] = {
                name: value[:1000] if isinstance(value, str) else value
                for name, value in data[key].items()
                if isinstance(value, (str, int, float, bool))
            }
            content = {
                name: value for name, value in data[key].items() if isinstance(value, (dict, list))
            }
            if isinstance(content.get("items"), list):
                actions = [
                    item
                    for item in content["items"]
                    if isinstance(item, dict)
                    and item.get("type") not in {"userMessage", "reasoning"}
                ]
                if data[key].get("items_order") == "asc":
                    actions.reverse()
                    progress[key]["content_order"] = "desc"
                else:
                    progress[key]["content_order"] = data[key].get("items_order", "native")
                # Keep the actual latest action visible ahead of its potentially
                # long arguments/output. Full original details remain archived.
                headers = ("id", "type", "status", "exitCode", "success", "command", "tool")
                content["items"] = [
                    {**{field: item[field] for field in headers if field in item}, **item}
                    for item in actions
                ]
            if content:
                preview = json.dumps(content, ensure_ascii=False, default=str)
                progress[key].update(
                    content_preview=preview[:1000], preview_truncated=len(preview) > 1000
                )
    if isinstance(data.get("work"), dict):
        work = data["work"]
        progress["work"] = {
            key: work[key]
            for key in (
                "work_id",
                "state",
                "progress_available",
                "progress_reason",
                "query_scope",
                "status_scope",
                "observed_at",
                "next_cursor",
                "history_order",
                "history_scope",
                "cursor_parameter",
            )
            if key in work
        }
        details = {key: work[key] for key in ("steps", "context_note") if key in work}
        if details:
            preview = json.dumps(details, ensure_ascii=False, default=str)
            progress["work"].update(
                content_preview=preview[:1200], preview_truncated=len(preview) > 1200
            )
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
        *(
            (key, body.get(key))
            for key in (
                "status",
                "run_id",
                "operation_id",
                "execution_id",
                "session_id",
                "event_id",
                "original_event_id",
                "snapshot_id",
                "artifact_id",
                "pending",
                "executed",
            )
        ),
    ):
        if value is not None:
            envelope[key] = value
    process = process_receipt(result)
    if process:
        envelope["process"] = process
    if result.tool_name == "send" and isinstance(result.data, dict):
        # These are the original Social receipts, not inferred from arguments
        # or the survival of message text. Long delivered/prepared bodies stay
        # in the immutable original while every part keeps its real identity.
        def delivery(item: dict[str, Any]) -> dict[str, Any]:
            facts = {
                key: item[key]
                for key in (
                    "operation_id",
                    "status",
                    "target",
                    "event_id",
                    "original_event_id",
                    "original_conversation_id",
                    "error_category",
                    "artifact_id",
                    "snapshot_id",
                    "uncertain",
                    "pending",
                    "text_planned",
                    "planned_messages",
                    "sent_messages",
                )
                if key in item
            }
            for field in ("file", "caption"):
                if isinstance(item.get(field), dict):
                    facts[field] = delivery(item[field])
            if isinstance(item.get("parts"), list):
                facts["parts"] = [delivery(part) for part in item["parts"]]
            return facts

        envelope["delivery"] = delivery(result.data)
    media = result.metadata or {}
    if "media_read" in media:
        envelope["media_read"] = media["media_read"]
    if "media_error" in media:
        envelope["media_error"] = media["media_error"]
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
                "read_memory",
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
                "read_memory",
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
    if result.mutation_committed is not None:
        return result.mutation_committed
    if not result.ok:
        return False
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
