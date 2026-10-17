"""Private media locations returned by the actual platform connection."""

from dataclasses import dataclass
from typing import Literal, Protocol

MediaKind = Literal["image", "audio", "video", "file"]


@dataclass(frozen=True, slots=True)
class PlatformMediaFile:
    content: bytes | None = None
    content_type: str | None = None
    url: str | None = None


class MediaResolutionError(RuntimeError):
    """A sanitized media error that never includes credentials or signed URLs."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


class PlatformMediaGateway(Protocol):
    async def resolve_media_file(
        self, file: str, kind: MediaKind, *, max_download_bytes: int
    ) -> PlatformMediaFile:
        """Resolve on this connection; credential-bearing URLs remain inside the adapter."""
