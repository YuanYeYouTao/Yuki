"""Tavily REST search/extract provider with bounded retries and output."""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from qq_ai_bot.web.base import WebSearchError, normalize_public_url
from qq_ai_bot.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource

_SOURCE_CONTENT_LIMIT = 2500
_SNIPPET_LIMIT = 1000


class TavilyWebSearchProvider:
    """Call Tavily Search then one batched Tavily Extract request."""

    def __init__(
        self,
        *,
        api_key: str,
        search_depth: str = "advanced",
        extract_max_results: int = 3,
        timeout_seconds: float = 180,
        max_retries: int = 1,
        global_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("TAVILY_API_KEY is required")
        self._search_depth = search_depth
        self._extract_max_results = min(max(1, extract_max_results), 3)
        self._max_retries = min(max(0, max_retries), 1)
        self._semaphore = asyncio.Semaphore(global_concurrency)
        self._authorization_header = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient(
            base_url="https://api.tavily.com",
            timeout=timeout_seconds,
        )
        self._closed = False

    def __repr__(self) -> str:
        return "TavilyWebSearchProvider(base_url='https://api.tavily.com')"

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        query = _validated_query(request.query)
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "query": query,
            "topic": request.topic,
            "search_depth": self._search_depth,
            "max_results": min(max(1, request.max_results), 5),
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if request.time_range is not None:
            payload["time_range"] = request.time_range
        if request.start_date is not None:
            payload["start_date"] = request.start_date.isoformat()
        if request.end_date is not None:
            payload["end_date"] = request.end_date.isoformat()

        data = await self._post_json("/search", payload)
        candidates = self._search_sources(data, query)
        if not candidates:
            raise WebSearchError("empty_results", "没有找到可用的联网结果")
        extract_limit = (
            self._extract_max_results
            if request.extract_max_results is None
            else max(1, min(request.extract_max_results, 3))
        )
        selected = candidates[:extract_limit]
        extracted: dict[str, str] = {}
        partial_failure = False
        try:
            extracted, partial_failure = await self._extract_batch(
                tuple(source.url for source in selected), query
            )
        except WebSearchError:
            partial_failure = True

        sources = tuple(
            WebSearchSource(
                source_id=source.source_id,
                title=source.title,
                url=source.url,
                domain=source.domain,
                snippet=source.snippet,
                relevant_content=extracted.get(source.url, source.snippet)[:_SOURCE_CONTENT_LIMIT],
                published_at=source.published_at,
                provider_score=source.provider_score,
            )
            for source in selected
        )
        return WebSearchResponse(
            query=query,
            sources=sources,
            provider_request_id=_optional_string(data.get("request_id")),
            latency_seconds=time.perf_counter() - started,
            partial_failure=partial_failure,
        )

    async def extract(self, url: str, query: str) -> WebSearchSource:
        normalized = normalize_public_url(url)
        intent = _validated_query(query or "读取用户指定的网页")
        extracted, partial_failure = await self._extract_batch((normalized,), intent)
        content = extracted.get(normalized, "")
        if partial_failure or not content:
            raise WebSearchError("extract_failed", "网页正文提取失败")
        domain = urlsplit(normalized).hostname or ""
        return WebSearchSource(
            source_id=_source_id(normalized, intent),
            title=domain,
            url=normalized,
            domain=domain,
            snippet=content[:_SNIPPET_LIMIT],
            relevant_content=content[:_SOURCE_CONTENT_LIMIT],
        )

    async def _extract_batch(
        self,
        urls: tuple[str, ...],
        query: str,
    ) -> tuple[dict[str, str], bool]:
        data = await self._post_json(
            "/extract",
            {
                "urls": list(urls),
                "extract_depth": "basic",
                "format": "markdown",
                "query": query,
                "chunks_per_source": 3,
                "include_images": False,
            },
        )
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise WebSearchError("invalid_response", "联网服务返回了无效的提取结果")
        extracted: dict[str, str] = {}
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("url")
            content = item.get("raw_content")
            if not isinstance(raw_url, str) or not isinstance(content, str) or not content.strip():
                continue
            try:
                normalized = normalize_public_url(raw_url)
            except WebSearchError:
                continue
            if normalized in urls:
                extracted[normalized] = content.strip()[:_SOURCE_CONTENT_LIMIT]
        failed = data.get("failed_results")
        has_failed_results = isinstance(failed, list) and bool(failed)
        return extracted, has_failed_results or len(extracted) != len(urls)

    @staticmethod
    def _search_sources(data: dict[str, Any], query: str) -> list[WebSearchSource]:
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise WebSearchError("invalid_response", "联网服务返回了无效的搜索结果")
        seen: set[str] = set()
        sources: list[WebSearchSource] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            raw_url = item.get("url")
            if not isinstance(title, str) or not title.strip() or not isinstance(raw_url, str):
                continue
            try:
                normalized = normalize_public_url(raw_url)
            except WebSearchError:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            domain = urlsplit(normalized).hostname or ""
            snippet = item.get("content")
            score = item.get("score")
            sources.append(
                WebSearchSource(
                    source_id=_source_id(normalized, query),
                    title=title.strip()[:300],
                    url=normalized,
                    domain=domain,
                    snippet=(snippet.strip()[:_SNIPPET_LIMIT] if isinstance(snippet, str) else ""),
                    relevant_content="",
                    published_at=_parse_datetime(
                        item.get("published_at") or item.get("published_date")
                    ),
                    provider_score=(
                        float(score)
                        if isinstance(score, int | float) and not isinstance(score, bool)
                        else None
                    ),
                )
            )
        return sources

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    response = await self._client.post(
                        path,
                        json=payload,
                        headers={"Authorization": self._authorization_header},
                    )
            except httpx.TimeoutException as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                raise WebSearchError("timeout", "联网服务请求超时") from exc
            except httpx.RequestError as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                raise WebSearchError("connection_failed", "无法连接联网服务") from exc

            if response.status_code in {401, 403}:
                raise WebSearchError("authentication_failed", "联网服务鉴权失败")
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt < self._max_retries:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                code = "rate_limited" if response.status_code == 429 else "provider_unavailable"
                detail = (
                    "联网服务请求过于频繁" if response.status_code == 429 else "联网服务暂不可用"
                )
                raise WebSearchError(code, detail)
            if response.status_code >= 400:
                raise WebSearchError("provider_rejected", "联网服务拒绝了请求")
            try:
                decoded = response.json()
            except ValueError as exc:
                raise WebSearchError("invalid_json", "联网服务返回了无效 JSON") from exc
            if not isinstance(decoded, dict):
                raise WebSearchError("invalid_response", "联网服务返回结构无效")
            return decoded
        raise WebSearchError("provider_unavailable", "联网服务暂不可用")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()


def _validated_query(query: str) -> str:
    normalized = " ".join(query.split())
    if not normalized:
        raise WebSearchError("invalid_query", "搜索词不能为空")
    if len(normalized) > 400:
        raise WebSearchError("invalid_query", "搜索词不能超过 400 个字符")
    return normalized


def _source_id(url: str, query: str) -> str:
    return hashlib.sha256(f"{url}\x1f{query}".encode()).hexdigest()[:24]


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(candidate)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "").strip()
    if header:
        try:
            return min(max(float(header), 0.0), 10.0)
        except ValueError:
            try:
                target = parsedate_to_datetime(header)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                return min(max((target - datetime.now(UTC)).total_seconds(), 0.0), 10.0)
            except (TypeError, ValueError):
                pass
    return 0.25 * float(2**attempt)
