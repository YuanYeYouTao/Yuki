"""Tool media observations carried by one outgoing request, in each protocol's own shape.

Every dialect replays its stored continuation payload in full (Responses uses
``store: false``), so an observation image once sent stays in every later
request. Replacement rewrites only the outgoing request; the next continuation
derives from what was actually sent, so a replaced image stays replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from yuki.domain.messages import ChatImage, ChatMessage, ChatRequest

TOOL_MEDIA_OBSERVATION = "[Host 工具媒体观察：call_id="
MEDIA_NOT_CARRIED = (
    "[Host：这条观察的图像未随本次请求发送（较新的图像已用满本次请求的图像帧数或字节上限）；"
    "需要时用上面的 media_artifact_handle 调 read_result 重读，"
    "或用 path／artifact_id／event_id 调 view_file 重看。]"
)


def tool_media_observation(call_id: str, images: tuple[ChatImage, ...]) -> ChatMessage:
    """The Host observation carrying one tool result's pixels after its paired receipt."""
    references = dict.fromkeys(
        f"media_artifact_handle={image.tool_handle}"
        if image.tool_handle
        else f"artifact_id={image.artifact_id}"
        if image.artifact_id
        else f"path={image.workspace_path}"
        if image.workspace_path
        else f"event_id={image.source_event_id}，attachment={image.attachment_index}"
        for image in images
        if image.tool_handle
        or image.artifact_id
        or image.workspace_path
        or image.source_event_id is not None
    )
    return ChatMessage(
        role="user",
        content=(
            f"{TOOL_MEDIA_OBSERVATION}{call_id}；"
            + "".join(f"{reference}；" for reference in references)
            + "像素来自 Agent 已选且获准读取的资料，属于工具结果。]"
        ),
        images=images,
    )


def is_tool_media_observation(message: ChatMessage) -> bool:
    return message.role == "user" and (message.content or "").startswith(TOOL_MEDIA_OBSERVATION)


@dataclass(frozen=True, slots=True)
class RequestImage:
    """One image this request sends; ``observation`` marks replaceable tool media."""

    data_url: str
    observation: bool


@dataclass(frozen=True, slots=True)
class _Group:
    area: str
    index: int
    images: tuple[str, ...]
    observation: bool


def _parts_key(protocol: str) -> str:
    return "parts" if protocol == "gemini" else "content"


def _wire_parts(protocol: str, item: dict[str, Any]) -> list[Any] | None:
    if item.get("role") != "user" or (protocol == "responses" and item.get("type") != "message"):
        return None
    parts = item.get(_parts_key(protocol))
    return parts if isinstance(parts, list) else None


_IMAGE_TYPES = {
    "chat_completions": "image_url",
    "responses": "input_image",
    "anthropic_messages": "image",
}


def _is_wire_image(protocol: str, part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    if protocol == "gemini":
        return isinstance(part.get("inlineData"), dict)
    return part.get("type") == _IMAGE_TYPES.get(protocol)


def _wire_image(protocol: str, part: Any) -> str | None:
    """The data URL of an image part this Host's adapter encoded, else None."""
    if not _is_wire_image(protocol, part):
        return None
    if protocol == "chat_completions":
        return str(part["image_url"]["url"])
    if protocol == "responses":
        return str(part["image_url"])
    if protocol == "anthropic_messages":
        source = part["source"]
        return f"data:{source['media_type']};base64,{source['data']}"
    inline = part["inlineData"]
    return f"data:{inline['mimeType']};base64,{inline['data']}"


def _wire_text(protocol: str, part: Any) -> str | None:
    if not isinstance(part, dict):
        return None
    if protocol == "gemini":
        text = part.get("text")
    elif part.get("type") == ("input_text" if protocol == "responses" else "text"):
        text = part.get("text")
    else:
        return None
    return text if isinstance(text, str) else None


def _text_part(protocol: str, text: str) -> dict[str, Any]:
    if protocol == "gemini":
        return {"text": text}
    return {"type": "input_text" if protocol == "responses" else "text", "text": text}


def _groups(request: ChatRequest) -> list[_Group]:
    groups = [
        _Group(
            "messages",
            index,
            tuple(image.data_url for image in message.images),
            is_tool_media_observation(message),
        )
        for index, message in enumerate(request.messages)
        if message.images
    ]
    continuation = request.continuation
    if continuation is not None and isinstance(continuation.payload, tuple):
        protocol = continuation.protocol
        for index, item in enumerate(continuation.payload):
            parts = _wire_parts(protocol, item) if isinstance(item, dict) else None
            if not parts:
                continue
            images = tuple(
                url for part in parts if (url := _wire_image(protocol, part)) is not None
            )
            if images:
                first = _wire_text(protocol, parts[0])
                groups.append(
                    _Group(
                        "payload",
                        index,
                        images,
                        first is not None and first.startswith(TOOL_MEDIA_OBSERVATION),
                    )
                )
    groups.extend(
        _Group(
            "items",
            index,
            tuple(image.data_url for image in item.images),
            is_tool_media_observation(item),
        )
        for index, item in enumerate(request.continuation_items)
        if isinstance(item, ChatMessage) and item.images
    )
    return groups


def without_wire_media(protocol: str, payload: tuple[object, ...]) -> tuple[list[object], int]:
    """A continuation payload with its image parts elided, and how many there were.

    Capacity counts prepared pixels as media, not as base64 text, wherever
    the dialect replays them.
    """
    count = 0
    elided: list[object] = []
    key = _parts_key(protocol)
    for item in payload:
        parts = item.get(key) if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(parts, list):
            elided.append(item)
            continue
        media = [_is_wire_image(protocol, part) for part in parts]
        count += sum(media)
        elided.append(
            {
                **item,
                key: [
                    "[prepared media]" if is_media else part
                    for part, is_media in zip(parts, media, strict=True)
                ],
            }
        )
    return elided, count


def request_media(request: ChatRequest) -> tuple[RequestImage, ...]:
    """Every image the request sends, including those replayed from its continuation."""
    return tuple(
        RequestImage(url, group.observation) for group in _groups(request) for url in group.images
    )


def carry_request_media(request: ChatRequest, *, max_frames: int, max_bytes: int) -> ChatRequest:
    """Replace the oldest tool media observations beyond the existing frame/byte limits.

    Other images are always carried; if they alone exceed the limits the
    admission check still refuses the request. Observations are kept newest
    first until one no longer fits; it and every older one become text.
    """
    groups = _groups(request)
    frames = sum(len(group.images) for group in groups if not group.observation)
    size = sum(len(url) for group in groups if not group.observation for url in group.images)
    replaced: set[tuple[str, int]] = set()
    for group in reversed([group for group in groups if group.observation]):
        weight = sum(len(url) for url in group.images)
        if not replaced and frames + len(group.images) <= max_frames and size + weight <= max_bytes:
            frames += len(group.images)
            size += weight
        else:
            replaced.add((group.area, group.index))
    if not replaced:
        return request

    def text_only(message: ChatMessage) -> ChatMessage:
        return replace(message, content=f"{message.content}\n{MEDIA_NOT_CARRIED}", images=())

    continuation = request.continuation
    if continuation is not None and any(area == "payload" for area, _ in replaced):
        protocol = continuation.protocol
        assert isinstance(continuation.payload, tuple)
        key = _parts_key(protocol)
        continuation = replace(
            continuation,
            payload=tuple(
                {
                    **item,
                    key: [
                        *(part for part in item[key] if not _is_wire_image(protocol, part)),
                        _text_part(protocol, MEDIA_NOT_CARRIED),
                    ],
                }
                if ("payload", index) in replaced
                else item
                for index, item in enumerate(continuation.payload)
            ),
        )
    return replace(
        request,
        messages=tuple(
            text_only(message) if ("messages", index) in replaced else message
            for index, message in enumerate(request.messages)
        ),
        continuation=continuation,
        continuation_items=tuple(
            text_only(item)
            if isinstance(item, ChatMessage) and ("items", index) in replaced
            else item
            for index, item in enumerate(request.continuation_items)
        ),
    )
