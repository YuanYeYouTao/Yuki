"""A search-only Gemini request behind the stable local web_search function."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, replace
from urllib.parse import urlsplit

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    CitationOrigin,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolStatus,
    NativeToolType,
)
from qq_ai_bot.llm.base import LLMError, LLMProvider
from qq_ai_bot.model_runtime.models import ModelProfile
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.request_accounting import (
    ProviderAttemptCounter,
    current_provider_attempts,
)
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.web.base import WebSearchError, WebSearchProvider, normalize_public_url
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource

logger = logging.getLogger(__name__)


class GeminiSearchBridge:
    """Never passes Main Agent history or function declarations to Google Search."""

    name = "gemini_native_bridge"

    def __init__(
        self,
        *,
        profile: ModelProfile,
        credential: str,
        provider: LLMProvider,
        state: BridgeState,
        fallback: WebSearchProvider | None = None,
        invocations: ModelInvocationRepository | None = None,
        summary_max_characters: int = 16000,
    ) -> None:
        if summary_max_characters < 1:
            raise ValueError("search summary limit must be positive")
        if (
            profile.max_output_tokens_limit is not None
            and profile.default_max_output_tokens > profile.max_output_tokens_limit
        ):
            raise ValueError("search default exceeds configured provider output limit")
        self.profile = profile
        self.provider = provider
        self.state = state
        self.fallback = fallback
        self.invocations = invocations
        self.summary_max_characters = summary_max_characters
        self.slot = asyncio.Lock()
        self._namespace = hashlib.sha256(
            json.dumps(
                {
                    "version": 3,
                    "profile": profile.id,
                    "model": profile.model,
                    "base_url": profile.base_url,
                    "max_output": profile.default_max_output_tokens,
                    "summary_max_characters": summary_max_characters,
                    "reasoning_effort": profile.reasoning_effort,
                    "wire_options": str(profile.wire_options),
                    "credential": credential,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).digest()

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        query = " ".join(request.query.split())
        if not 1 <= len(query) <= 400:
            raise WebSearchError("invalid_query", "搜索词须为 1–400 字符")
        normalized_request = replace(request, query=query)
        key = hashlib.sha256(
            self._namespace
            + json.dumps(asdict(normalized_request), sort_keys=True, default=str).encode()
        ).hexdigest()
        async with self.slot:
            cached = await asyncio.to_thread(self.state.access, key)
            if (
                isinstance(cached, WebSearchResponse)
                and cached.provider == self.name
                and not cached.partial_failure
            ):
                return cached
            try:
                result = await self._search(normalized_request)
            except WebSearchError as exc:
                if self.fallback is None:
                    raise
                logger.info("gemini_search_fallback category=%s", exc.code)
                # A fallback receipt is never cached under the primary search key.
                return await self.fallback.search(request)
            # A failed extraction or incomplete native response may recover on
            # the next request; do not pin that partial receipt for ten minutes.
            if not result.partial_failure:
                await asyncio.to_thread(self.state.access, key, result)
            return result

    async def _search(self, request: WebSearchRequest) -> WebSearchResponse:
        started = time.perf_counter()
        control = current_work_control.get()
        if control is not None:
            await control.validate()
            await control.reserve_request(auxiliary=True)
        constraints = {key: value for key, value in asdict(request).items() if value is not None}
        prompt = (
            "请使用 Google 搜索查找以下问题的公开来源。只依据实际搜索到的来源回答；"
            "不要编造 URL、来源或发布日期。日期无法核实时请说明。\n"
            + json.dumps(constraints, ensure_ascii=False, default=str)
        )
        model_request = ChatRequest(
            messages=(ChatMessage(role="user", content=prompt),),
            model=self.profile.model,
            max_output_tokens=self.profile.default_max_output_tokens,
            thinking_enabled=self.profile.thinking_enabled,
            reasoning_effort=self.profile.reasoning_effort,
            tools=(),
            native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),),
        )
        attempts = ProviderAttemptCounter()
        token = current_provider_attempts.set(attempts)
        try:
            try:
                response = await self.provider.complete(model_request)
            except LLMError as exc:
                await self._record(
                    None,
                    attempts,
                    started=started,
                    error_category=type(exc).__name__,
                    failure=exc,
                )
                raise WebSearchError("search_failed", "Gemini 搜索请求失败") from exc
            except Exception as exc:
                await self._record(
                    None,
                    attempts,
                    started=started,
                    error_category=type(exc).__name__,
                )
                raise WebSearchError("search_failed", "Gemini 搜索请求失败") from exc
        finally:
            current_provider_attempts.reset(token)
        if response.tool_calls:
            await self._record(
                response, attempts, started=started, error_category="unexpected_tools"
            )
            raise WebSearchError("invalid_response", "搜索请求返回了非预期的函数调用")
        found: dict[str, WebSearchSource] = {}
        for citation in response.citations:
            if citation.origin is not CitationOrigin.ANNOTATION:
                continue
            try:
                url = normalize_public_url(citation.url)
            except WebSearchError:
                continue
            found.setdefault(
                url,
                WebSearchSource(
                    source_id=hashlib.sha256(f"{url}\x1f{request.query}".encode()).hexdigest()[:24],
                    title=(citation.title or url)[:300],
                    url=url,
                    domain=urlsplit(url).hostname or "",
                    snippet="",
                    relevant_content="",
                    provider=self.name,
                ),
            )
        if not found:
            await self._record(response, attempts, started=started, error_category="no_grounding")
            raise WebSearchError("no_search_evidence", "Gemini 没有返回可信的搜索来源")
        await self._record(response, attempts, started=started)
        logger.info(
            "gemini_search_usage input_tokens=%s output_tokens=%s cached_input_tokens=%s",
            response.prompt_tokens,
            response.completion_tokens,
            response.cached_prompt_tokens,
        )
        sources = list(found.values())[: max(1, min(request.max_results, 5))]
        extract_failed = False
        if self.fallback is not None:
            fallback = self.fallback

            async def extract_source(index: int) -> None:
                nonlocal extract_failed
                try:
                    page = await fallback.extract(sources[index].url, request.query)
                    sources[index] = replace(
                        sources[index],
                        snippet=page.snippet[:1000],
                        relevant_content=page.relevant_content[:2500],
                    )
                except WebSearchError:
                    extract_failed = True

            # At most three pages; Tavily keeps its existing global HTTP semaphore.
            # Assign by original index so response order is independent of completion order.
            extracts = [
                asyncio.create_task(extract_source(index))
                for index in range(min(len(sources), request.extract_max_results or 0, 3))
            ]
            gathered = asyncio.gather(*extracts)
            try:
                # Own child cancellation below; repeated caller cancellation must
                # not interrupt a child's transport cleanup for a second time.
                await asyncio.shield(gathered)
            except BaseException:
                # gather can raise while siblings are still on wire. Cancel and join
                # every child before releasing the bridge slot, including repeated cancellation.
                for child in extracts:
                    if not child.done() and not child.cancelling():
                        child.cancel()
                joined = asyncio.gather(*extracts, return_exceptions=True)
                while not joined.done():
                    try:
                        await asyncio.shield(joined)
                    except asyncio.CancelledError:
                        continue
                joined.result()
                if gathered.done() and not gathered.cancelled():
                    gathered.exception()
                raise
        return WebSearchResponse(
            query=request.query,
            sources=tuple(sources),
            provider_request_id=response.provider_request_id,
            latency_seconds=response.latency_seconds,
            partial_failure=(
                response.status is not ModelResponseStatus.COMPLETED
                or any(
                    event.status is NativeToolStatus.FAILED for event in response.native_tool_events
                )
                or request.start_date is not None
                or request.end_date is not None
                or request.time_range is not None
                or extract_failed
            ),
            provider=self.name,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
            provider_summary=response.content[: self.summary_max_characters].strip() or None,
        )

    async def _record(
        self,
        response: ChatResponse | None,
        attempts: ProviderAttemptCounter,
        *,
        started: float,
        error_category: str | None = None,
        failure: LLMError | None = None,
    ) -> None:
        if self.invocations is None:
            return
        diagnostic_usage = failure.diagnostics.get("usage") if failure is not None else None
        diagnostic_usage = diagnostic_usage if isinstance(diagnostic_usage, dict) else {}

        def reported_tokens(name: str) -> int | None:
            value = diagnostic_usage.get(name)
            return value if type(value) is int and value >= 0 else None

        try:
            await self.invocations.record(
                task="web_search",
                profile_id=self.profile.id,
                provider=self.profile.provider,
                model=self.profile.model,
                success=error_category is None,
                prompt_tokens=response.prompt_tokens
                if response
                else reported_tokens("prompt_tokens"),
                completion_tokens=(
                    response.completion_tokens if response else reported_tokens("completion_tokens")
                ),
                total_tokens=response.total_tokens if response else reported_tokens("total_tokens"),
                cached_prompt_tokens=(
                    response.cached_prompt_tokens
                    if response
                    else reported_tokens("cached_prompt_tokens")
                ),
                latency_seconds=time.perf_counter() - started,
                error_category=error_category,
                physical_request_count=attempts.requests,
                unknown_usage_request_count=attempts.unknown_usage_requests,
                native_search_requested=True,
            )
        except Exception:
            logger.exception("gemini_search_usage_record_failed")

    async def extract(self, url: str, query: str) -> WebSearchSource:
        normalized = normalize_public_url(url)
        if self.fallback is None:
            raise WebSearchError("extract_unavailable", "当前搜索桥没有配置网页读取后端")
        return await self.fallback.extract(normalized, query)

    async def close(self) -> None:
        await self.provider.close()
        if self.fallback is not None:
            await self.fallback.close()
