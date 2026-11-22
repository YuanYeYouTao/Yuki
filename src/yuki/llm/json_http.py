"""Shared bounded HTTP transport; adapters own only serialization and parsing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from yuki.domain.messages import ChatRequest, ChatResponse
from yuki.execution_trace.phases import current_model_phases, model_detail
from yuki.execution_trace.recorder import record_http_response, record_trace, trace_span
from yuki.llm.base import (
    LLMConfigurationError,
    LLMError,
    LLMInvalidResponseError,
    LLMProvider,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMUnsupportedFeatureError,
    ProviderModel,
    RetryableProviderError,
)
from yuki.llm.http_errors import check_provider_response
from yuki.llm.protocol_state import integer
from yuki.model_runtime.dispatch_guard import check_model_dispatch
from yuki.model_runtime.models import DEFAULT_CONNECT_TIMEOUT_SECONDS
from yuki.model_runtime.request_accounting import (
    after_provider_request,
    before_provider_request,
    current_provider_attempts,
)
from yuki.runtime.work_activation import current_work_control


@dataclass(frozen=True, slots=True)
class _PreparedHTTP:
    request: httpx.Request
    payload: dict[str, Any]
    # The fixed tool list is linked by digest (and the turn's manifest revision),
    # never copied into each attempt's diagnostics.
    tools_sha256: str | None = None
    tool_count: int = 0

    def metadata(self, protocol: str) -> dict[str, Any]:
        return {
            "protocol": protocol,
            "dispatch": "prepared",
            "model": self.payload.get("model"),
            "request_bytes": len(self.request.content),
            "tools_sha256": self.tools_sha256,
            "tool_count": self.tool_count,
        }

    def full(self, protocol: str) -> dict[str, Any]:
        return {
            "protocol": protocol,
            "body": {key: value for key, value in self.payload.items() if key != "tools"},
            "tools_sha256": self.tools_sha256,
        }


def _tools_digest(tools: Any) -> str | None:
    if not tools:
        return None
    encoded = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class JSONHTTPProvider(LLMProvider):
    provider_name = "openai_compatible"
    protocol = "chat_completions"
    transport_errors: tuple[type[httpx.TransportError], ...] = (httpx.TransportError,)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_key_env: str | None = None,
        timeout_seconds: float,
        max_retries: int,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ) -> None:
        if provider_name is not None:
            self.provider_name = provider_name
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._max_retries = max_retries
        self._headers = dict(headers or {})
        self._owns_client = client is None
        # Only establishing the connection is shortened; reading the reply, writing the
        # body and waiting for a pool slot keep the Profile's full timeout.
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=min(connect_timeout_seconds, timeout_seconds)
        )
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/") + "/")

    @abstractmethod
    def _build_payload(self, request: ChatRequest) -> dict[str, Any]: ...

    @abstractmethod
    def _parse(self, response: httpx.Response, request: ChatRequest, payload: Any) -> ChatResponse:
        """Read the attempt-owned decoded body without mutating it.

        Opaque state retained beyond this response must keep its existing owned
        copies. The raw response remains available for byte-derived protocol IDs.
        """
        ...

    def _path(self, request: ChatRequest) -> str:
        return "chat/completions"

    def _request_headers(self) -> dict[str, str]:
        return {
            **self._headers,
            **({"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}),
        }

    def _parse_models(self, payload: Any) -> list[ProviderModel]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise LLMInvalidResponseError("provider returned an invalid model list")
        result = []
        for item in payload["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise LLMInvalidResponseError("provider returned an invalid model identifier")
            name = item.get("display_name")
            model: ProviderModel = {"id": item["id"]}
            if isinstance(name, str):
                model["display_name"] = name
            result.append(model)
        return result

    async def list_models(self) -> list[ProviderModel]:
        """Read one upstream page using this adapter's existing auth and base path."""

        prepared = self._client.build_request(
            "GET", "models", headers=self._request_headers(), timeout=self._timeout
        )
        await check_model_dispatch()
        try:
            response = await self._client.send(prepared)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("model list request timed out") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError("model list is unavailable") from exc
        if response.status_code in {404, 405, 501}:
            raise LLMUnsupportedFeatureError("provider does not expose a model list")
        check_provider_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMInvalidResponseError("provider returned invalid JSON") from exc
        return self._parse_models(payload)

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

    def _prepare(self, request: ChatRequest) -> _PreparedHTTP:
        with model_detail("payload_preparation"):
            payload = self._build_payload(request)
            headers = self._request_headers()
            if request.provider_session_id is not None:
                headers["x-session-id"] = request.provider_session_id
            prepared = self._client.build_request(
                "POST",
                self._path(request),
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
            tools = payload.get("tools")
            return _PreparedHTTP(
                prepared,
                payload,
                _tools_digest(tools),
                len(tools) if isinstance(tools, list) else 0,
            )

    async def _record_full_attempt(
        self, prepared: _PreparedHTTP, response: httpx.Response | None
    ) -> None:
        """Failed, rejected and retried attempts keep the complete actual exchange."""
        await record_trace("provider_request", prepared.full(self.protocol))
        if response is not None:
            await record_http_response(response)

    async def _post(
        self, prepared: _PreparedHTTP, *, retry: bool = False
    ) -> tuple[httpx.Response, Any]:
        async with trace_span("provider", prepared.metadata(self.protocol)):
            # Keep the real permission/budget fence immediately before HTTP dispatch.
            with model_detail("attempt_dispatch_preparation"):
                await check_model_dispatch()

            account, finish = before_provider_request.get(), after_provider_request.get()
            if account is not None:
                await account()
            attempts = current_provider_attempts.get()
            if attempts is not None:
                attempts.dispatched()
            phases = current_model_phases.get()
            if phases is not None:
                phases.attempts += 1
            response: httpx.Response | None = None
            try:
                with model_detail("transport"):
                    response = await self._client.send(prepared.request)
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
            except BaseException as exc:
                if finish is not None:
                    await finish("failed", None)
                if isinstance(exc, Exception):
                    await self._record_full_attempt(prepared, response)
                raise
            try:
                body = response.json()
            except ValueError as exc:
                if finish is not None:
                    await finish("unknown", None)
                await self._record_full_attempt(prepared, response)
                raise LLMInvalidResponseError("provider returned invalid JSON") from exc
            mapping = body if isinstance(body, dict) else {}
            diagnostics = self._usage_diagnostics(mapping)
            if finish is not None:
                usage = diagnostics.get("usage", {})
                usage = usage if isinstance(usage, dict) else {}
                await finish(str(mapping.get("status", "unknown")), usage.get("completion_tokens"))
            if retry:
                await self._record_full_attempt(prepared, response)
            else:
                # A successful attempt keeps route, size and usage; model_phases
                # keeps the logical usage and phase timings.
                await record_trace(
                    "provider_response",
                    {
                        "http_status": response.status_code,
                        "dispatch": "response_received",
                        "response_bytes": len(response.content),
                        **diagnostics,
                    },
                )
        return response, body

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if not request.model or (self._api_key_env != "" and not self._api_key):
            raise LLMConfigurationError("LLM is not configured")
        started = time.perf_counter()
        # Provider-executed tools have no local receipt for uncertain transport outcomes.
        attempts = 1 if request.native_tools else self._max_retries + 1
        retry_errors: tuple[type[Exception], ...] = (*self.transport_errors, RetryableProviderError)
        prepared = self._prepare(request)

        async def retry_sleep(seconds: float) -> None:
            with model_detail("retry_backoff"):
                await asyncio.sleep(seconds)

        retried = False
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
                    retried = attempt.retry_state.attempt_number > 1
                    response, raw_body = await self._post(prepared, retry=retried)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except (httpx.TransportError, RetryableProviderError) as exc:
            # Errors outside this adapter's retry set (a reply cut mid-body: the outcome is
            # unknown) get the same transport category after their single attempt.
            raise LLMUnavailableError(
                "LLM is temporarily unavailable",
                diagnostics=getattr(exc, "diagnostics", {}),
            ) from exc
        counter = current_provider_attempts.get()
        diagnostics = self._usage_diagnostics(raw_body) if isinstance(raw_body, dict) else {}
        reported_usage = diagnostics.get("usage", {})
        if counter is not None and isinstance(reported_usage, dict):
            counter.reported_usage(reported_usage.get("total_tokens"), usage=reported_usage)
        try:
            with model_detail("provider_response_preparation"):
                parsed = self._parse(response, request, raw_body)
        except LLMError as exc:
            if not retried:
                await self._record_full_attempt(prepared, response)
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
