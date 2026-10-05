"""Convert MCP SDK content blocks into the unified kernel result."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import replace
from typing import Any

from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.messages import ChatImage
from qq_ai_bot.mcp.redaction import redact_sensitive_data, redact_sensitive_text
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.native_media import NativeMediaPreparer

_MAX_IMAGE_INPUT_BYTES = 20 * 1024 * 1024


def normalize_mcp_result(
    value: Any,
    *,
    server_id: str,
    tool_name: str,
    media_preparer: NativeMediaPreparer | None = None,
) -> ToolExecutionResult:
    media_preparer = media_preparer or NativeMediaPreparer(ImagePreprocessor())
    structured = getattr(value, "structuredContent", None)
    is_error = bool(getattr(value, "isError", False))
    content: list[dict[str, Any]] = []
    images: list[ChatImage] = []
    prepared_characters = 0
    public_error = ""
    upstream_error_code = ""
    retryable = False
    for item in getattr(value, "content", ()):
        if hasattr(item, "model_dump"):
            dumped = item.model_dump(mode="json", exclude_none=True)
            normalized = dict(dumped) if isinstance(dumped, dict) else {"value": dumped}
        else:
            normalized = {"value": str(item)}
        embedded = normalized.get("resource")
        image_resource = (
            normalized.get("type") == "resource"
            and isinstance(embedded, dict)
            and str(embedded.get("mimeType", "")).startswith("image/")
            and "blob" in embedded
        )
        if normalized.get("type") == "image" or image_resource:
            if is_error:
                content.append(
                    {"type": "image", "status": "unread", "error_code": "mcp_tool_error"}
                )
                continue
            # Pixel data is a private observation from this already authorized
            # tool call, never public JSON or a URL to fetch on the server's behalf.
            block, prepared = _prepare_image_block(
                {"data": embedded["blob"]}
                if image_resource and isinstance(embedded, dict)
                else normalized,
                max_frames=media_preparer.max_frames - len(images),
                max_characters=media_preparer.max_bytes - prepared_characters,
                preparer=media_preparer,
            )
            content.append(block)
            images.extend(prepared)
            prepared_characters += sum(len(image.data_url) for image in prepared)
            continue
        # MCP servers commonly mirror structuredContent into a large text block for
        # backwards-compatible clients. Keeping both can double the payload and make
        # the result budgeter discard the useful structured data. On successful
        # structured results, retain only non-text blocks such as images/resources.
        if structured is not None and not is_error and normalized.get("type") == "text":
            continue
        if is_error and normalized.get("type") == "text":
            parsed_error = _parse_structured_error(normalized.get("text"))
            if parsed_error is not None:
                upstream_error_code, public_error, retryable = parsed_error
        content.append(redact_sensitive_data(normalized))

    if structured is None and not is_error:
        promoted, content = _promote_single_json_text(content)
        if promoted is not None:
            structured = promoted
    content = _redact_text_blocks(content)
    structured = redact_sensitive_data(_strip_image_payloads(structured))
    public_error = redact_sensitive_text(public_error)
    return ToolExecutionResult(
        ok=not is_error,
        data=structured,
        content=tuple(content),
        images=tuple(images),
        error_code="mcp_tool_error" if is_error else None,
        public_message=(public_error or "MCP 工具返回错误") if is_error else None,
        retryable=retryable,
        mutation_committed=False if is_error else None,
        provider_id=f"mcp.{server_id}",
        tool_name=tool_name,
        metadata={
            "mcp_is_error": is_error,
            **({"mcp_error_code": upstream_error_code} if upstream_error_code else {}),
        },
    )


def _prepare_image_block(
    item: dict[str, Any],
    *,
    max_frames: int,
    max_characters: int,
    preparer: NativeMediaPreparer,
) -> tuple[dict[str, Any], tuple[ChatImage, ...]]:
    block: dict[str, Any] = {"type": "image", "status": "unread"}
    try:
        raw = item.get("data")
        if max_frames <= 0 or max_characters <= 0:
            raise ValueError("frame_budget")
        if not isinstance(raw, str) or len(raw) > ((_MAX_IMAGE_INPUT_BYTES + 2) // 3) * 4:
            raise ValueError("too_large")
        pixels = base64.b64decode(raw, validate=True)
        remaining_preparer = NativeMediaPreparer(
            preparer.preprocessor, max_bytes=max_characters, max_frames=max_frames
        )
        images = remaining_preparer.prepare_image(pixels, source="tool", max_frames=max_frames)
    except (ValueError, binascii.Error) as exc:
        # No raw decoder text: servers may put signed URLs or tokens in data.
        block["error_code"] = (
            str(exc) if str(exc) in {"too_large", "frame_budget"} else "invalid_image"
        )
        return block, ()
    except Exception as exc:
        block["error_code"] = getattr(exc, "code", "invalid_image")
        return block, ()
    block.update(status="prepared", image_count=len(images))
    return block, tuple(
        replace(image, content_hash=hashlib.sha256(image.data_url.encode()).hexdigest())
        for image in images
    )


def _strip_image_payloads(value: Any) -> Any:
    """Remove typed image mirrors without rewriting ordinary text/data values."""
    if isinstance(value, list):
        return [_strip_image_payloads(item) for item in value]
    if not isinstance(value, dict):
        return value
    is_image = value.get("type") == "image" or str(value.get("mimeType", "")).startswith("image/")
    return {
        key: _strip_image_payloads(item)
        for key, item in value.items()
        if not (is_image and key in {"data", "blob", "uri"})
    }


def _promote_single_json_text(
    content: list[dict[str, Any]],
) -> tuple[dict[str, Any] | list[Any] | None, list[dict[str, Any]]]:
    """Promote one complete JSON object/array text block to structured data."""

    text_indexes = [
        index
        for index, item in enumerate(content)
        if item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    if len(text_indexes) != 1:
        return None, content
    text_index = text_indexes[0]
    raw_text = str(content[text_index]["text"]).strip()
    try:
        decoded = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        content[text_index]["text"] = redact_sensitive_text(raw_text)
        return None, content
    if not isinstance(decoded, (dict, list)):
        content[text_index]["text"] = redact_sensitive_text(raw_text)
        return None, content
    remaining = content[:text_index] + content[text_index + 1 :]
    return redact_sensitive_data(decoded), remaining


def _redact_text_blocks(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for item in content:
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            item = {**item, "text": redact_sensitive_text(str(item["text"]))}
        redacted.append(item)
    return redacted


def _parse_structured_error(value: object) -> tuple[str, str, bool] | None:
    """Extract the bounded server error envelope used by MCP tool failures."""

    if not isinstance(value, str):
        return None
    opening = value.find("{")
    if opening < 0:
        return None
    try:
        payload = json.loads(value[opening:])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    message = redact_sensitive_text(" ".join(str(payload.get("message", "")).split())[:500])
    if not message:
        return None
    error_code = str(payload.get("error_code", "")).strip()[:64]
    retryable = payload.get("retryable") is True
    return error_code, message, retryable
