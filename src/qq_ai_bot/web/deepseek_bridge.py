"""Removable Anthropic search adapter. Main-agent Responses history never enters here."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, replace
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.media_resolver import MediaResolutionError, MediaResolver
from qq_ai_bot.vision.models import MediaReference
from qq_ai_bot.web.base import WebSearchError, WebSearchProvider, normalize_public_url
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource

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


class DeepSeekSearchBridge:
    name = "deepseek_anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        state_path: Path,
        fallback: WebSearchProvider | None = None,
        timeout_seconds: float = 20,
        extract_max_results: int = 3,
        client: httpx.AsyncClient | None = None,
        media: MediaResolver | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeek search credentials are required")
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self.headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        self.media = media or MediaResolver(
            max_download_bytes=2 * 1024 * 1024,
            timeout_seconds=timeout_seconds,
            allow_private_urls=False,
        )
        self.state = BridgeState(state_path)
        self.fallback = fallback
        self.extract_max_results = extract_max_results
        # Collapse concurrent repeated queries without retaining per-query locks.
        self.slot = asyncio.Lock()

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        query = " ".join(request.query.split())
        if not 1 <= len(query) <= 400:
            raise WebSearchError("invalid_query", "搜索词须为 1–400 字符")
        key = hashlib.sha256(
            json.dumps(asdict(request), sort_keys=True, default=str).encode()
        ).hexdigest()
        async with self.slot:
            cached = await asyncio.to_thread(self.state.access, key)
            if isinstance(cached, WebSearchResponse):
                return cached
            try:
                # Date filters need provider support; instructions are not equivalent.
                if request.time_range or request.start_date or request.end_date:
                    raise WebSearchError("unsupported_filter", "日期筛选需要 Tavily 后端")
                result = await self._search(replace(request, query=query))
            except WebSearchError as exc:
                if self.fallback is None:
                    raise
                logger.info("deepseek_search_fallback category=%s", exc.code)
                result = await self.fallback.search(request)
            await asyncio.to_thread(self.state.access, key, result)
            return result

    async def _search(self, request: WebSearchRequest) -> WebSearchResponse:
        started = time.monotonic()
        constraints = {k: v for k, v in asdict(request).items() if v is not None}
        payload = {
            "model": "deepseek-flash",
            "max_tokens": 4096,
            "system": (
                "你是检索服务。只搜索提供的问题，遵守主题限制。"
                "必须调用 web_search，最多两次；不要猜测来源，最后用一句话结束。"
            ),
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(constraints, ensure_ascii=False, default=str),
                }
            ],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
            "tool_choice": {"type": "auto"},
        }
        control = current_work_control.get()
        if control is not None:
            await control.validate()
            await control.reserve_request(auxiliary=True)
        try:
            # No retries: a timeout may already have consumed search/model credits.
            async with self.client.stream(
                "POST",
                "https://api.deepseek.com/anthropic/v1/messages",
                headers=self.headers,
                json=payload,
            ) as response:
                if response.status_code != 200:
                    raise WebSearchError("search_unavailable", "DeepSeek 搜索暂不可用")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise WebSearchError("search_response_too_large", "搜索响应超过上限")
            value = json.loads(raw)
        except (httpx.HTTPError, ValueError) as exc:
            raise WebSearchError("search_failed", "DeepSeek 搜索请求失败") from exc
        if not isinstance(value, dict) or not isinstance(value.get("content"), list):
            raise WebSearchError("invalid_response", "搜索响应格式无效")
        blocks = value["content"]
        calls = {
            b["id"]
            for b in blocks
            if isinstance(b, dict)
            and b.get("type") == "server_tool_use"
            and b.get("name") == "web_search"
            and isinstance(b.get("id"), str)
        }
        found: dict[str, WebSearchSource] = {}
        search_error = False
        for block in blocks:
            if (
                not isinstance(block, dict)
                or block.get("type") != "web_search_tool_result"
                or not isinstance(block.get("tool_use_id"), str)
                or block.get("tool_use_id") not in calls
            ):
                continue
            results = block.get("content")
            if not isinstance(results, list):
                search_error = True
                continue
            for item in results:
                if (
                    not isinstance(item, dict)
                    or item.get("type") != "web_search_result"
                    or not isinstance(item.get("url"), str)
                ):
                    continue
                try:
                    url = normalize_public_url(item["url"])
                except WebSearchError:
                    continue
                text = item.get("content", "")
                snippet = text[:1000] if isinstance(text, str) else ""
                found.setdefault(
                    url,
                    WebSearchSource(
                        hashlib.sha256(f"{url}\x1f{request.query}".encode()).hexdigest()[:24],
                        str(item.get("title") or url)[:300],
                        url,
                        urlsplit(url).hostname or "",
                        snippet,
                        snippet,
                        provider=self.name,
                    ),
                )
        if not found:
            raise WebSearchError("no_search_evidence", "没有返回真实的搜索来源")
        sources = list(found.values())[: max(1, min(request.max_results, 5))]
        partial = value.get("stop_reason") != "end_turn" or search_error
        for index in range(
            min(
                len(sources),
                request.extract_max_results
                if request.extract_max_results is not None
                else self.extract_max_results,
                3,
            )
        ):
            try:
                page = await self._extract(sources[index].url, request.query)
                sources[index] = replace(sources[index], relevant_content=page.relevant_content)
            except WebSearchError:
                partial = True
        usage = value.get("usage") or {}
        if isinstance(usage, dict):
            logger.info(
                "deepseek_search_usage input_tokens=%s output_tokens=%s",
                usage.get("input_tokens"),
                usage.get("output_tokens"),
            )
        return WebSearchResponse(
            request.query,
            tuple(sources),
            value.get("id"),
            time.monotonic() - started,
            partial,
            self.name,
        )

    async def _extract(self, url: str, query: str) -> WebSearchSource:
        normalized = normalize_public_url(url)
        try:
            page = await self.media.resolve(MediaReference(url=normalized))
        except (MediaResolutionError, OSError) as exc:
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
        terms = query.casefold().split()
        matches = [line for line in lines if any(term in line.casefold() for term in terms)]
        content = "\n".join(matches or lines)[:2500]
        if not content:
            raise WebSearchError("empty_page", "网页正文为空")
        return WebSearchSource(
            hashlib.sha256(f"{normalized}\x1f{query}".encode()).hexdigest()[:24],
            urlsplit(normalized).hostname or "",
            normalized,
            urlsplit(normalized).hostname or "",
            content[:1000],
            content,
            provider="direct_http",
        )

    async def extract(self, url: str, query: str) -> WebSearchSource:
        normalized = normalize_public_url(url)
        key = hashlib.sha256(f"page:{normalized}:{query}".encode()).hexdigest()
        async with self.slot:
            cached = await asyncio.to_thread(self.state.access, key)
            if isinstance(cached, WebSearchResponse):
                return cached.sources[0]
            try:
                source = await self._extract(normalized, query)
            except WebSearchError:
                if self.fallback is None:
                    raise
                source = await self.fallback.extract(normalized, query)
            await asyncio.to_thread(
                self.state.access,
                key,
                WebSearchResponse(query, (source,), None, 0, provider=source.provider),
            )
            return source

    async def close(self) -> None:
        await self.client.aclose()
        await self.media.close()
        if self.fallback is not None:
            await self.fallback.close()
