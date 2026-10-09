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
    transport_errors: tuple[type[httpx.TransportError], ...] = (httpx.TransportError,)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ) -> None:
        if provider_name is not None:
            self.provider_name = provider_name
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
            prepared = self._client.build_request(
                "POST",
                self._path(request),
                headers=self._request_headers(),
                json=payload,
                timeout=self._timeout,
            )
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
            from qq_ai_bot.model_runtime.request_accounting import (
                after_provider_request,
                before_provider_request,
            )

            account, finish = before_provider_request.get(), after_provider_request.get()
            if account is not None:
                await account()
            attempts = current_provider_attempts.get()
            if attempts is not None:
                attempts.dispatched()
            phases = current_model_phases.get()
            if phases is not None:
                phases.attempts += 1
            try:
                with model_detail("transport"):
                    response = await self._client.send(prepared)
                await record_http_response(response)
                try:
                    check_provider_response(response)
                except LLMError as exc:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    diagnostics = (
                        self._usage_diagnostics(payload) if isinstance(payload, dict) else {}
                    )
                    usage = diagnostics.get("usage")
                    if isinstance(usage, dict) and any(
                        type(value) is int and value >= 0 for value in usage.values()
                    ):
                        if attempts is not None:
                            attempts.reported_usage(usage.get("total_tokens"), usage=usage)
                        exc.diagnostics = {**exc.diagnostics, "usage": usage}
                    raise
            except BaseException:
                if finish is not None:
                    await finish("failed", None)
                raise
            if finish is not None:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                body = body if isinstance(body, dict) else {}
                usage = self._usage_diagnostics(body).get("usage", {})
                usage = usage if isinstance(usage, dict) else {}
                await finish(str(body.get("status", "unknown")), usage.get("completion_tokens"))
        return response

    async def complete(self, request: ChatRequest) -> ChatResponse:
        from dataclasses import replace

        from qq_ai_bot.runtime.work_activation import current_work_control

        if not self._api_key or not request.model:
            raise LLMConfigurationError("LLM is not configured")
        started = time.perf_counter()
        # Provider-executed tools have no local receipt for uncertain transport outcomes.
        attempts = 1 if request.native_tools else self._max_retries + 1
        retry_errors: tuple[type[Exception], ...] = (*self.transport_errors, RetryableProviderError)

        async def retry_sleep(seconds: float) -> None:
            with model_detail("retry_backoff"):
                await asyncio.sleep(seconds)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_random_exponential(multiplier=0.25, max=2),
                retry=retry_if_exception_type(retry_errors),
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
        except retry_errors as exc:
            raise LLMUnavailableError(
                "LLM is temporarily unavailable",
                diagnostics=getattr(exc, "diagnostics", {}),
            ) from exc
        counter = current_provider_attempts.get()
        try:
            raw_body = response.json()
        except ValueError:
            raw_body = {}
        diagnostics = self._usage_diagnostics(raw_body) if isinstance(raw_body, dict) else {}
        reported_usage = diagnostics.get("usage", {})
        if counter is not None and isinstance(reported_usage, dict):
            counter.reported_usage(reported_usage.get("total_tokens"), usage=reported_usage)
        try:
            with model_detail("provider_response_preparation"):
                parsed = self._parse(response, request)
        except LLMError as exc:
            if reported_usage:
                exc.diagnostics = {**exc.diagnostics, "usage": reported_usage}
            usage = exc.diagnostics.get("usage")
            if counter is not None and isinstance(usage, dict):
                counter.reported_usage(usage.get("total_tokens"), usage=usage)
            raise
        if counter is not None:
            counter.reported_response(parsed)
        return replace(parsed, latency_seconds=time.perf_counter() - started)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
