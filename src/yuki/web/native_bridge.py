"""An independent native-search request behind the stable local web_search function."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, replace
from urllib.parse import urlsplit

from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    CitationOrigin,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolStatus,
    NativeToolType,
    ResponseCitation,
)
from yuki.llm.base import LLMError, LLMProvider
from yuki.model_runtime.dispatch_guard import check_model_dispatch
from yuki.model_runtime.models import ModelProfile
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.model_runtime.request_accounting import (
    ProviderAttemptCounter,
    current_provider_attempts,
)
from yuki.runtime.work_activation import current_work_control
from yuki.web.base import WebPageExtractor, WebSearchError, normalize_public_url
from yuki.web.bridge_state import BridgeState
from yuki.web.models import WebSearchRequest, WebSearchResponse, WebSearchSource

logger = logging.getLogger(__name__)


class NativeSearchBridge:
    """Use one selected Profile without Main history, client functions or search fallback."""

    name = "native_search_bridge"

    def __init__(
        self,
        *,
        profile: ModelProfile,
        credential: str,
        provider: LLMProvider,
        state: BridgeState,
        extractor: WebPageExtractor,
        invocations: ModelInvocationRepository | None = None,
    ) -> None:
        if (
            profile.max_output_tokens_limit is not None
            and profile.default_max_output_tokens > profile.max_output_tokens_limit
        ):
            raise ValueError("search default exceeds configured provider output limit")
        self.profile = profile
        self.provider = provider
        self.state = state
        self.extractor = extractor
        self.invocations = invocations
        self._namespace = hashlib.sha256(
            json.dumps(
                {
                    "version": "independent-native-search-v2",
                    "profile": profile.model_dump(mode="json"),
                    "credential": credential,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).digest()

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        query = " ".join(request.query.split())
        if not query:
            raise WebSearchError("invalid_query", "搜索词不能为空")
        normalized_request = replace(request, query=query)
        key = hashlib.sha256(
            self._namespace
            + json.dumps(asdict(normalized_request), sort_keys=True, default=str).encode()
        ).hexdigest()
        cached = await asyncio.to_thread(self.state.access, key)
        if (
            isinstance(cached, WebSearchResponse)
            and cached.provider == self.name
            and not cached.partial_failure
        ):
            return cached
        result = await self._search(normalized_request)
        # An incomplete native response may recover on the next request;
        # do not pin that partial receipt for ten minutes.
        if not result.partial_failure:
            await asyncio.to_thread(self.state.access, key, result)
        return result

    async def _search(self, request: WebSearchRequest) -> WebSearchResponse:
        started = time.perf_counter()
        control = current_work_control.get()
        provider_session_id = None
        if control is not None:
            await control.validate()
            await control.reserve_request(auxiliary=True)
            work_id = control.current["id"] if control.current else control.lease.work_id
            provider_session_id = (
                f"yuki:{control.lease.conversation_id}:g{control.lease.generation}:"
                + (f"work:{work_id}" if work_id else "main")
            )
        else:
            await check_model_dispatch()
        constraints = {key: value for key, value in asdict(request).items() if value is not None}
        prompt = (
            "请使用已声明的原生网页搜索查找以下问题的公开来源。只依据实际搜索到的来源回答；"
            "不要编造 URL、来源或发布日期。日期无法核实时请说明。\n"
            + json.dumps(constraints, ensure_ascii=False, default=str)
        )
        model_request = ChatRequest(
            provider_session_id=provider_session_id,
            messages=(ChatMessage(role="user", content=prompt),),
            model=self.profile.model,
            temperature=self.profile.default_temperature,
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
            except asyncio.CancelledError:
                await self._record(None, attempts, started=started, error_category="CancelledError")
                raise
            except LLMError as exc:
                await self._record(
                    None,
                    attempts,
                    started=started,
                    error_category=type(exc).__name__,
                    failure=exc,
                )
                raise WebSearchError("search_failed", "原生搜索请求失败") from exc
            except Exception as exc:
                await self._record(
                    None,
                    attempts,
                    started=started,
                    error_category=type(exc).__name__,
                )
                raise WebSearchError("search_failed", "原生搜索请求失败") from exc
        finally:
            current_provider_attempts.reset(token)
        if response.tool_calls:
            await self._record(
                response, attempts, started=started, error_category="unexpected_tools"
            )
            raise WebSearchError("invalid_response", "搜索请求返回了非预期的函数调用")
        found: dict[str, WebSearchSource] = {}
        search_events = {
            event.call_id: event
            for event in response.native_tool_events
            if event.tool_type is NativeToolType.WEB_SEARCH
        }
        citations = list(response.citations)
        citations.extend(
            ResponseCitation(
                url=event.url,
                origin=CitationOrigin.OPEN_PAGE_ACTION,
                call_id=event.call_id,
            )
            for event in search_events.values()
            if event.status is NativeToolStatus.COMPLETED
            and event.action_type == "open_page"
            and event.url
        )
        for citation in citations:
            if citation.origin not in {
                CitationOrigin.ANNOTATION,
                CitationOrigin.NATIVE_RESULT,
                CitationOrigin.OPEN_PAGE_ACTION,
            }:
                continue
            if citation.call_id is not None:
                event = search_events.get(citation.call_id)
                if event is None or event.status is not NativeToolStatus.COMPLETED:
                    continue
            elif citation.origin is CitationOrigin.NATIVE_RESULT:
                continue
            try:
                url = normalize_public_url(citation.url)
            except WebSearchError:
                continue
            snippet = citation.snippet or ""
            if url in found:
                original = found[url]
                found[url] = replace(
                    original,
                    title=citation.title
                    if original.title == url and citation.title
                    else original.title,
                    snippet=original.snippet or snippet,
                    relevant_content=original.relevant_content or snippet,
                )
            else:
                found[url] = WebSearchSource(
                    source_id=hashlib.sha256(f"{url}\x1f{request.query}".encode()).hexdigest()[:24],
                    title=citation.title or url,
                    url=url,
                    domain=urlsplit(url).hostname or "",
                    snippet=snippet,
                    relevant_content=snippet,
                    provider=self.name,
                )
        if not found:
            await self._record(response, attempts, started=started, error_category="no_grounding")
            raise WebSearchError("no_search_evidence", "原生搜索没有返回可信的搜索来源")
        await self._record(response, attempts, started=started)
        logger.info(
            "native_search_usage input_tokens=%s output_tokens=%s cached_input_tokens=%s",
            response.prompt_tokens,
            response.completion_tokens,
            response.cached_prompt_tokens,
        )
        return WebSearchResponse(
            query=request.query,
            sources=tuple(found.values()),
            provider_request_id=response.provider_request_id,
            latency_seconds=response.latency_seconds,
            partial_failure=(
                response.status is not ModelResponseStatus.COMPLETED
                or any(
                    event.tool_type is NativeToolType.WEB_SEARCH
                    and event.status is not NativeToolStatus.COMPLETED
                    for event in response.native_tool_events
                )
                or request.start_date is not None
                or request.end_date is not None
                or request.time_range is not None
            ),
            provider=self.name,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
            provider_summary=response.content.strip() or None,
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
                cache_creation_input_tokens=(
                    response.cache_creation_input_tokens
                    if response
                    else reported_tokens("cache_creation_input_tokens")
                ),
                cache_creation_5m_input_tokens=(
                    response.cache_creation_5m_input_tokens
                    if response
                    else reported_tokens("cache_creation_5m_input_tokens")
                ),
                cache_creation_1h_input_tokens=(
                    response.cache_creation_1h_input_tokens
                    if response
                    else reported_tokens("cache_creation_1h_input_tokens")
                ),
                latency_seconds=time.perf_counter() - started,
                error_category=error_category,
                physical_request_count=attempts.requests,
                unknown_usage_request_count=attempts.unknown_usage_requests,
                native_search_requested=True,
            )
        except Exception:
            logger.exception("native_search_usage_record_failed")

    async def extract(self, url: str, query: str) -> WebSearchSource:
        normalized = normalize_public_url(url)
        return await self.extractor.extract(normalized, query)

    async def close(self) -> None:
        errors: list[BaseException] = []
        closed: set[int] = set()
        for resource in (self.provider, self.extractor):
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            try:
                await resource.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            cancellation = next(
                (error for error in errors if not isinstance(error, Exception)), None
            )
            if cancellation is not None:
                for error in errors:
                    if error is not cancellation:
                        cancellation.add_note(f"close failed: {type(error).__name__}")
                raise cancellation
            raise BaseExceptionGroup("search bridge close failed", errors)
