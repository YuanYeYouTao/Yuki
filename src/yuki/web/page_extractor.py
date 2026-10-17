"""Public page extraction without a search provider or model request."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from html.parser import HTMLParser
from urllib.parse import urlsplit

from yuki.services.media_resolver import MediaResolutionError, MediaResolver
from yuki.vision.models import MediaReference
from yuki.web.base import WebSearchError, normalize_public_url
from yuki.web.bridge_state import BridgeState
from yuki.web.http_transport import create_web_http_client
from yuki.web.models import WebSearchResponse, WebSearchSource

logger = logging.getLogger(__name__)


class PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


class DirectPageExtractor:
    def __init__(
        self, *, state: BridgeState, timeout_seconds: float, media: MediaResolver | None = None
    ) -> None:
        self.state = state
        self._client = (
            create_web_http_client(timeout_seconds=timeout_seconds) if media is None else None
        )
        self.media = (
            media
            if media is not None
            else MediaResolver(
                max_download_bytes=2 * 1024 * 1024,
                timeout_seconds=timeout_seconds,
                allow_private_urls=False,
                client=self._client,
            )
        )

    async def _extract(self, url: str, query: str) -> WebSearchSource:
        normalized = normalize_public_url(url)
        try:
            page = await self.media.resolve(MediaReference(url=normalized))
        except MediaResolutionError as exc:
            cause: BaseException = exc
            while cause.__cause__ is not None:
                cause = cause.__cause__
            logger.warning(
                "web_page_download_failed code=%s cause=%s",
                exc.code,
                type(cause).__name__,
            )
            raise WebSearchError("extract_failed", "网页下载失败") from exc
        except OSError as exc:
            logger.warning("web_page_download_failed cause=%s", type(exc).__name__)
            raise WebSearchError("extract_failed", "网页下载失败") from exc
        content_type = (page.content_type or "").split(";")[0].casefold()
        if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
            raise WebSearchError("unsupported_page", "仅支持 HTML 或纯文本网页")
        text = page.content.decode("utf-8", errors="replace")
        if content_type != "text/plain":
            parser = PageText()
            parser.feed(text)
            text = "\n".join(parser.parts)
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        content = "\n".join(lines)
        if not content:
            raise WebSearchError("empty_page", "网页正文为空")
        return WebSearchSource(
            hashlib.sha256(f"{normalized}\x1f{query}".encode()).hexdigest()[:24],
            urlsplit(normalized).hostname or "",
            normalized,
            urlsplit(normalized).hostname or "",
            content,
            content,
            provider="direct_http",
        )

    async def extract(self, url: str, query: str) -> WebSearchSource:
        normalized = normalize_public_url(url)
        key = hashlib.sha256(f"direct-page-v2:{normalized}:{query}".encode()).hexdigest()
        cached = await asyncio.to_thread(self.state.access, key)
        if isinstance(cached, WebSearchResponse) and cached.provider == "direct_http":
            return cached.sources[0]
        source = await self._extract(normalized, query)
        await asyncio.to_thread(
            self.state.access,
            key,
            WebSearchResponse(query, (source,), None, 0, provider=source.provider),
        )
        return source

    async def close(self) -> None:
        try:
            await self.media.close()
        finally:
            if self._client is not None:
                await self._client.aclose()
