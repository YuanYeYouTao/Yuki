"""OneBot image lookup and QQ SILK conversion at the transport boundary."""

import base64
import binascii
from typing import Any, Protocol

from yuki.gateway.media import MediaKind, MediaResolutionError, PlatformMediaFile


class OneBotMediaHandle(Protocol):
    async def call_api(self, api: str, **params: Any) -> Any: ...


async def resolve_media_file(
    handle: OneBotMediaHandle,
    file: str,
    kind: MediaKind,
    *,
    max_download_bytes: int,
) -> PlatformMediaFile:
    if kind not in {"image", "audio"}:
        raise MediaResolutionError("resource_unavailable", "该附件缺少平台可下载地址")
    action = "get_record" if kind == "audio" else "get_image"
    try:
        payload = await handle.call_api(
            action, **{"file": file, **({"out_format": "mp3"} if kind == "audio" else {})}
        )
    except Exception as exc:
        raise MediaResolutionError(f"{action}_failed", "平台媒体查询失败") from exc
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise MediaResolutionError("resource_unavailable", "平台媒体查询结果无效")
    choices = ("base64", "file", "url") if kind == "audio" else ("url", "file", "base64")
    for key in choices:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            continue
        if value.startswith(("http://", "https://")):
            return PlatformMediaFile(url=value)
        if key == "base64" and not value.startswith(("base64://", "data:")):
            value = "base64://" + value
        if not value.startswith(("base64://", "data:")):
            continue
        content_type = None
        if value.startswith("data:"):
            header, separator, encoded = value.partition(",")
            if not separator or ";base64" not in header:
                raise MediaResolutionError("invalid_base64", "平台媒体编码无效")
            content_type = header.removeprefix("data:").partition(";")[0]
        else:
            encoded = value.removeprefix("base64://")
        if len(encoded) > (max_download_bytes + 2) // 3 * 4:
            raise MediaResolutionError("too_large", "平台媒体超过下载预算")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MediaResolutionError("invalid_base64", "平台媒体编码无效") from exc
        if not content:
            raise MediaResolutionError("empty_media", "平台媒体为空")
        if len(content) > max_download_bytes:
            raise MediaResolutionError("too_large", "平台媒体超过下载预算")
        return PlatformMediaFile(content=content, content_type=content_type)
    # A gateway-container path is not a Bot filesystem source.
    raise MediaResolutionError("resource_unavailable", "平台媒体当前不可读取")


class OneBotMediaAdapter:
    def __init__(self, handle: OneBotMediaHandle) -> None:
        self._handle = handle

    async def resolve_media_file(
        self, file: str, kind: MediaKind, *, max_download_bytes: int
    ) -> PlatformMediaFile:
        return await resolve_media_file(
            self._handle, file, kind, max_download_bytes=max_download_bytes
        )
