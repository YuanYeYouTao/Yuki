"""Shared bounded HTTP transport; adapters own only serialization and parsing."""

from __future__ import annotations

import asyncio
import time
from abc import abstractmethod
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.execution_trace.phases import current_model_phases, model_detail
from qq_ai_bot.execution_trace.recorder import record_http_response, trace_span
from qq_ai_bot.llm.base import (
    LLMConfigurationError,
    LLMError,
    LLMProvider,
    LLMTimeoutError,
    LLMUnavailableError,
    RetryableProviderError,
)
from qq_ai_bot.llm.http_errors import check_provider_response
from qq_ai_bot.llm.protocol_state import integer
from qq_ai_bot.llm.wire_diagnostics import WireRequestObserver
from qq_ai_bot.model_runtime.request_accounting import current_provider_attempts


class JSONHTTPProvider(LLMProvider):
    provider_name = "openai_compatible"
    protocol = "chat_completions"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._headers = dict(headers or {})
        self._owns_client = client is None
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/") + "/")
        self._wire_observer = WireRequestObserver()

    @abstractmethod
    def _build_payload(self, request: ChatRequest) -> dict[str, Any]: ...

    @abstractmethod
    def _parse(self, response: httpx.Response, request: ChatRequest) -> ChatResponse: ...

    def _path(self, request: ChatRequest) -> str:
        return "chat/completions"

    def _request_headers(self) -> dict[str, str]:
        return {**self._headers, "Authorization": f"Bearer {self._api_key}"}

    @staticmethod
    def _usage_diagnostics(payload: dict[str, Any]) -> dict[str, object]:
        """OpenAI-style Chat usage; native adapters override their wire mapping."""
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        output_details = usage.get("completion_tokens_details")
        output_details = output_details if isinstance(output_details, dict) else {}
        incoming = integer(usage.get("prompt_tokens"))
        output = integer(usage.get("completion_tokens"))
        total = integer(usage.get("total_tokens"))
        if total is None and incoming is not None and output is not None:
            total = incoming + output
        return {
            "usage": {
                "prompt_tokens": incoming,
                "completion_tokens": output,
                "total_tokens": total,
                "cached_prompt_tokens": integer(
                    details.get("cached_tokens", usage.get("prompt_cache_hit_tokens"))
                ),
                "reasoning_tokens": integer(output_details.get("reasoning_tokens")),
            }
        }

    async def _post(self, request: ChatRequest) -> httpx.Response:
        from qq_ai_bot.model_runtime.dispatch_guard import check_model_dispatch

        with model_detail("payload_preparation"):
            payload = self._build_payload(request)
            self._wire_observer.observe(
                payload,
                self.protocol,
                chain_id=request.request_chain_id,
                provider=self.provider_name,
            )
        async with trace_span(
            "provider", {"protocol": self.protocol, "body": payload, "dispatch": "prepared"}
        ):
            # Keep the real permission/budget fence immediately before HTTP dispatch.
            with model_detail("attempt_dispatch_preparation"):
                await check_model_dispatch()
            attempts = current_provider_attempts.get()
            if attempts is not None:
                attempts.dispatched()
            phases = current_model_phases.get()
            if phases is not None:
                phases.attempts += 1
            with model_detail("transport"):
                response = await self._client.post(
                    self._path(request),
                    headers=self._request_headers(),
                    json=payload,
                    timeout=self._timeout,
                )
            await record_http_response(response)
            try:
                check_provider_response(response)
            except LLMError as exc:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                diagnostics = self._usage_diagnostics(payload) if isinstance(payload, dict) else {}
                usage = diagnostics.get("usage")
                if isinstance(usage, dict) and any(
                    type(value) is int and value >= 0 for value in usage.values()
                ):
                    if attempts is not None:
                        attempts.reported_usage(usage.get("total_tokens"))
                    exc.diagnostics = {**exc.diagnostics, "usage": usage}
                raise
        return response

    async def complete(self, request: ChatRequest) -> ChatResponse:
        from dataclasses import replace

        from qq_ai_bot.runtime.work_activation import current_work_control

        if not self._api_key or not request.model:
            raise LLMConfigurationError("LLM is not configured")
        started = time.perf_counter()
        # Provider-executed tools have no local receipt for uncertain transport outcomes.
        attempts = 1 if request.native_tools else self._max_retries + 1

        async def retry_sleep(seconds: float) -> None:
            with model_detail("retry_backoff"):
                await asyncio.sleep(seconds)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_random_exponential(multiplier=0.25, max=2),
                retry=retry_if_exception_type((httpx.TransportError, RetryableProviderError)),
                reraise=True,
                sleep=retry_sleep,
            ):
                with attempt:
                    work = current_work_control.get()
                    if work is not None and attempt.retry_state.attempt_number > 1:
                        with model_detail("retry_budget_preparation"):
                            await work.reserve_request(auxiliary=True)
                    response = await self._post(request)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except (httpx.TransportError, RetryableProviderError) as exc:
            raise LLMUnavailableError(
                "LLM is temporarily unavailable",
                diagnostics=getattr(exc, "diagnostics", {}),
            ) from exc
        counter = current_provider_attempts.get()
        try:
            with model_detail("provider_response_preparation"):
                parsed = self._parse(response, request)
        except LLMError as exc:
            usage = exc.diagnostics.get("usage")
            if counter is not None and isinstance(usage, dict):
                counter.reported_usage(usage.get("total_tokens"))
            raise
        if counter is not None:
            counter.reported_usage(parsed.total_tokens)
        return replace(parsed, latency_seconds=time.perf_counter() - started)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
