"""Native Claude Messages adapter with signed thinking and ordered tool receipts."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import replace
from typing import Any

import httpx

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    CitationOrigin,
    FunctionCallOutput,
    ModelResponseStatus,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ProviderContinuation,
    ResponseCitation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import (
    LLMEmptyResponseError,
    LLMError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMUnsupportedFeatureError,
)
from qq_ai_bot.llm.json_http import JSONHTTPProvider
from qq_ai_bot.llm.protocol_state import (
    checkpoint_items,
    integer,
    ordered_delta,
    tool_result_failed,
)
from qq_ai_bot.llm.vendor_policy import ChatWireOptions, effort_value, thinking_budget, wire_options

logger = logging.getLogger(__name__)


class AnthropicMessagesProvider(JSONHTTPProvider):
    provider_name = "anthropic"
    protocol = "anthropic_messages"

    def __init__(self, *, options: ChatWireOptions | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.options = wire_options(self.provider_name, options)

    def _path(self, request: ChatRequest) -> str:
        return "messages"

    def _request_headers(self) -> dict[str, str]:
        return {
            "anthropic-version": "2023-06-01",
            **self._headers,
            "x-api-key": self._api_key,
        }

    async def complete(self, request: ChatRequest) -> ChatResponse:
        """Resume a bounded server-search pause with the original opaque blocks."""
        response = await super().complete(request)
        for _ in range(2):
            if response.incomplete_reason != "pause_turn":
                return response
            if response.continuation is None:
                raise LLMInvalidResponseError("Claude search pause has no checkpoint")
            from qq_ai_bot.runtime.work_activation import current_work_control

            work = current_work_control.get()
            if work is not None:
                from qq_ai_bot.runtime.activation_outcome import SegmentBudgetReached

                try:
                    await work.reserve_request(auxiliary=True)
                except SegmentBudgetReached:
                    return response
            try:
                followup = await super().complete(
                    replace(request, continuation=response.continuation, continuation_items=())
                )
            except LLMError as exc:
                later = exc.diagnostics.get("usage")
                later = later if isinstance(later, dict) else {}
                previous = {
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "total_tokens": response.total_tokens,
                    "cached_prompt_tokens": response.cached_prompt_tokens,
                    "cache_creation_input_tokens": response.cache_creation_input_tokens,
                    "cache_creation_5m_input_tokens": response.cache_creation_5m_input_tokens,
                    "cache_creation_1h_input_tokens": response.cache_creation_1h_input_tokens,
                }
                # A partial input sum cannot serve as the denominator for a
                # cache-read/write total spanning both physical requests.
                complete_input = (
                    previous["prompt_tokens"] is not None
                    and integer(later.get("prompt_tokens")) is not None
                )
                usage = {
                    key: (
                        self._sum_known_usage(previous[key], later.get(key))
                        if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                        else self._add_usage(previous[key], integer(later.get(key)))
                        if complete_input
                        else None
                    )
                    for key in previous
                }
                logger.warning(
                    "claude_search_pause_followup_failed category=%s",
                    type(exc).__name__,
                )
                return replace(
                    response,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                    cached_prompt_tokens=usage["cached_prompt_tokens"],
                    cache_creation_input_tokens=usage["cache_creation_input_tokens"],
                    cache_creation_5m_input_tokens=usage["cache_creation_5m_input_tokens"],
                    cache_creation_1h_input_tokens=usage["cache_creation_1h_input_tokens"],
                )
            response = replace(
                followup,
                content=response.content + followup.content,
                latency_seconds=response.latency_seconds + followup.latency_seconds,
                prompt_tokens=self._add_usage(response.prompt_tokens, followup.prompt_tokens),
                completion_tokens=self._add_usage(
                    response.completion_tokens, followup.completion_tokens
                ),
                total_tokens=self._add_usage(response.total_tokens, followup.total_tokens),
                cached_prompt_tokens=self._add_usage(
                    response.cached_prompt_tokens, followup.cached_prompt_tokens
                ),
                cache_creation_input_tokens=self._add_usage(
                    response.cache_creation_input_tokens,
                    followup.cache_creation_input_tokens,
                ),
                cache_creation_5m_input_tokens=self._add_usage(
                    response.cache_creation_5m_input_tokens,
                    followup.cache_creation_5m_input_tokens,
                ),
                cache_creation_1h_input_tokens=self._add_usage(
                    response.cache_creation_1h_input_tokens,
                    followup.cache_creation_1h_input_tokens,
                ),
                native_tool_events=response.native_tool_events + followup.native_tool_events,
                citations=response.citations + followup.citations,
                reasoning_content="\n".join(
                    part
                    for part in (response.reasoning_content, followup.reasoning_content)
                    if part
                )
                or None,
            )
        # Let the Runner retain the opaque checkpoint and perform its own bounded
        # incomplete-response recovery instead of losing a paid server-tool turn.
        return response

    @staticmethod
    def _add_usage(first: int | None, second: int | None) -> int | None:
        return first + second if first is not None and second is not None else None

    @staticmethod
    def _sum_known_usage(first: object, second: object) -> int | None:
        values = [
            value
            for value in (first, second)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        ]
        return sum(values) if values else None

    @staticmethod
    def _cache_creation_breakdown(usage: dict[str, Any]) -> tuple[int | None, int | None]:
        breakdown = usage.get("cache_creation")
        breakdown = breakdown if isinstance(breakdown, dict) else {}
        return (
            integer(breakdown.get("ephemeral_5m_input_tokens")),
            integer(breakdown.get("ephemeral_1h_input_tokens")),
        )

    @staticmethod
    def _usage_diagnostics(payload: dict[str, Any]) -> dict[str, object]:
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        incoming = integer(usage.get("input_tokens"))
        cached = integer(usage.get("cache_read_input_tokens"))
        creation = integer(usage.get("cache_creation_input_tokens"))
        creation_5m, creation_1h = AnthropicMessagesProvider._cache_creation_breakdown(usage)
        output = integer(usage.get("output_tokens"))
        total_input = (
            incoming + cached + creation
            if incoming is not None and cached is not None and creation is not None
            else None
        )
        return {
            "usage": {
                "prompt_tokens": total_input,
                "completion_tokens": output,
                "total_tokens": total_input + output
                if total_input is not None and output is not None
                else None,
                "cached_prompt_tokens": cached,
                "cache_creation_input_tokens": creation,
                "cache_creation_5m_input_tokens": creation_5m,
                "cache_creation_1h_input_tokens": creation_1h,
            }
        }

    def _message(self, message: ChatMessage) -> dict[str, Any]:
        if message.response_item is not None:
            raise LLMInvalidRequestError("opaque history requires its original protocol")
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        if message.images:
            if message.role != "user":
                raise LLMInvalidRequestError("images must be attached to a user message")
            for image in message.images:
                prefix, data = image.data_url.split(",", 1)
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": prefix[5:].split(";")[0],
                            "data": data,
                        },
                    }
                )
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.function.arguments)
            except ValueError as exc:
                raise LLMInvalidRequestError("invalid local tool arguments") from exc
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.function.name, "input": arguments}
            )
        if message.role == "tool":
            if not message.tool_call_id:
                raise LLMInvalidRequestError("tool result requires call ID")
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                    **({"is_error": True} if tool_result_failed(message.content or "") else {}),
                }
            ]
        return {"role": "assistant" if message.role == "assistant" else "user", "content": blocks}

    @staticmethod
    def _coalesce(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if result and result[-1]["role"] == message["role"]:
                result[-1]["content"].extend(deepcopy(message["content"]))
            else:
                result.append(deepcopy(message))
        return result

    def _history(self, request: ChatRequest) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        system: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role in {"system", "developer"} and not messages:
                system.append({"type": "text", "text": message.content or ""})
            else:
                messages.append(self._message(message))
        messages.extend(checkpoint_items(request, self.provider_name, self.protocol))
        for item in ordered_delta(request):
            messages.append(
                self._message(
                    ChatMessage(role="tool", content=item.output, tool_call_id=item.call_id)
                    if isinstance(item, FunctionCallOutput)
                    else item,
                )
            )
        return system, self._coalesce(messages)

    @staticmethod
    def _cache_conversation_prefix(messages: list[dict[str, Any]]) -> None:
        # Keep the existing system/tool breakpoints, then write one moving
        # conversation breakpoint. Block-level controls also work with native
        # Messages-compatible endpoints that reject top-level cache_control.
        for message in reversed(messages):
            for block in reversed(message["content"]):
                if block.get("type") in {"text", "tool_result"}:
                    block["cache_control"] = {"type": "ephemeral"}
                    return

    def _build_payload(self, request: ChatRequest) -> dict[str, Any]:
        if any(tool.type is not NativeToolType.WEB_SEARCH for tool in request.native_tools):
            raise LLMUnsupportedFeatureError("unsupported Claude native tool")
        if request.native_tools and any(tool.name == "web_search" for tool in request.tools):
            raise LLMInvalidRequestError("Claude native and local web_search names collide")
        system, messages = self._history(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens or 4096,
            "messages": messages,
            "stream": False,
        }
        if system:
            system[-1]["cache_control"] = {"type": "ephemeral"}
            payload["system"] = system
        if request.thinking_enabled:
            if self.options.reasoning == "budget":
                budget = thinking_budget(self.options, request.reasoning_effort)
                if budget >= payload["max_tokens"]:
                    raise LLMInvalidRequestError(
                        "Claude thinking budget must be below output limit"
                    )
                payload["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": budget,
                }
            elif self.options.reasoning == "effort":
                payload["thinking"] = {"type": "adaptive"}
                effort = effort_value(self.options, request.reasoning_effort)
                payload["output_config"] = {"effort": effort}
            else:
                raise LLMUnsupportedFeatureError("Claude requires adaptive effort or manual budget")
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
            choice = request.tool_choice or "auto"
            # Thinking cannot be forced into a tool-only response; validation remains local.
            if request.thinking_enabled and choice not in {"auto", "none"}:
                choice = "auto"
            payload["tool_choice"] = (
                {"type": "any" if choice == "required" else choice}
                if choice in {"auto", "none", "required"}
                else {"type": "tool", "name": choice}
            )
        if request.native_tools:
            payload.setdefault("tools", []).append(
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
            )
        if payload.get("tools"):
            # Claude caches tool definitions in order. The breakpoint must land
            # after native tools too, or a native-only request has no tool cache.
            payload["tools"][-1]["cache_control"] = {"type": "ephemeral"}
        self._cache_conversation_prefix(messages)
        if request.response_format is not None:
            nested = request.response_format.get("json_schema")
            if request.response_format.get("type") != "json_schema" or not isinstance(nested, dict):
                raise LLMUnsupportedFeatureError("Claude structured output requires JSON Schema")
            payload.setdefault("output_config", {})["format"] = {
                "type": "json_schema",
                "schema": nested["schema"],
            }
        return payload

    def _parse(self, response: httpx.Response, request: ChatRequest) -> ChatResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMInvalidResponseError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LLMInvalidResponseError("Claude returned invalid content blocks")
        if not isinstance(payload.get("content"), list):
            raise LLMInvalidResponseError(
                "Claude returned invalid content blocks",
                diagnostics=self._usage_diagnostics(payload),
            )
        stop = payload.get("stop_reason")
        if stop not in {"end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn"}:
            raise LLMInvalidResponseError(
                "Claude response was rejected or not completed",
                diagnostics=self._usage_diagnostics(payload),
            )
        blocks = payload["content"]
        texts: list[str] = []
        reasoning: list[str] = []
        calls: list[ToolCall] = []
        native_events: list[NativeToolEvent] = []
        citations: list[ResponseCitation] = []
        search_calls: dict[str, str] = {}
        for previous in checkpoint_items(request, self.provider_name, self.protocol):
            for prior_block in previous.get("content", ()):
                if (
                    not isinstance(prior_block, dict)
                    or prior_block.get("type") != "server_tool_use"
                ):
                    continue
                call_id = prior_block.get("id")
                query = prior_block.get("input")
                if isinstance(call_id, str):
                    search_calls[call_id] = (
                        query.get("query", "") if isinstance(query, dict) else ""
                    )
        for block in blocks:
            if not isinstance(block, dict):
                raise LLMInvalidResponseError("invalid Claude content block")
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
                raw_citations = block.get("citations")
                for cited in raw_citations if isinstance(raw_citations, list) else ():
                    if not isinstance(cited, dict):
                        continue
                    url = cited.get("url")
                    if isinstance(url, str) and url.startswith(("https://", "http://")):
                        title = cited.get("title")
                        citations.append(
                            ResponseCitation(
                                url=url,
                                title=title if isinstance(title, str) else "",
                                origin=CitationOrigin.ANNOTATION,
                            )
                        )
            elif kind == "thinking" and isinstance(block.get("thinking"), str):
                reasoning.append(block["thinking"])
            elif kind == "tool_use":
                if not isinstance(block.get("input"), dict) or not all(
                    isinstance(block.get(key), str) and block[key] for key in ("id", "name")
                ):
                    raise LLMInvalidResponseError("invalid Claude tool call")
                calls.append(
                    ToolCall(
                        id=block["id"],
                        function=ToolFunction(
                            name=block["name"],
                            arguments=json.dumps(block["input"], ensure_ascii=False),
                        ),
                    )
                )
            elif kind == "server_tool_use" and block.get("name") == "web_search":
                call_id = block.get("id")
                query = block.get("input")
                if not isinstance(call_id, str) or not call_id:
                    raise LLMInvalidResponseError("Claude search call has no id")
                search_calls[call_id] = query.get("query", "") if isinstance(query, dict) else ""
            elif kind == "web_search_tool_result":
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str) or call_id not in search_calls:
                    raise LLMInvalidResponseError("Claude search result has no matching call")
                result = block.get("content")
                failure = result if isinstance(result, dict) else None
                native_events.append(
                    NativeToolEvent(
                        tool_type=NativeToolType.WEB_SEARCH,
                        call_id=call_id,
                        status=(NativeToolStatus.FAILED if failure else NativeToolStatus.COMPLETED),
                        action_type="search",
                        query=search_calls[call_id],
                        error_category=(
                            str(failure.get("error_code", "unknown")) if failure else None
                        ),
                    )
                )
            elif kind != "redacted_thinking":
                raise LLMInvalidResponseError("unsupported Claude content block")
        completed_searches = {event.call_id for event in native_events}
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "server_tool_use"
                and block.get("id") not in completed_searches
            ):
                call_id = block["id"]
                native_events.append(
                    NativeToolEvent(
                        tool_type=NativeToolType.WEB_SEARCH,
                        call_id=call_id,
                        status=NativeToolStatus.SEARCHING,
                        action_type="search",
                        query=search_calls[call_id],
                    )
                )
        duplicate_call_ids = len({call.id for call in calls}) != len(calls)
        if duplicate_call_ids and not (native_events or request.native_tools):
            raise LLMInvalidResponseError("duplicate Claude tool IDs")
        truncated = stop == "max_tokens"
        content = "".join(texts)
        if (
            not content
            and not calls
            and not native_events
            and not request.native_tools
            and not truncated
            and stop != "pause_turn"
        ):
            raise LLMEmptyResponseError(
                "Claude returned no visible text or tool calls",
                diagnostics=self._usage_diagnostics(payload),
            )
        # Save only the protocol tail. Initial history is owned by TurnTranscript.
        # Coalescing can join a new tool result to the final initial message; use uncoalesced
        # checkpoint data so the initial prefix is never duplicated on replay.
        tail = checkpoint_items(request, self.provider_name, self.protocol)
        for item in ordered_delta(request):
            tail.append(
                self._message(
                    ChatMessage(
                        role="tool",
                        content=item.output,
                        tool_call_id=item.call_id,
                    )
                    if isinstance(item, FunctionCallOutput)
                    else item
                )
            )
        if blocks:
            tail.append({"role": "assistant", "content": deepcopy(blocks)})
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        incoming = integer(usage.get("input_tokens"))
        cached = integer(usage.get("cache_read_input_tokens"))
        creation = integer(usage.get("cache_creation_input_tokens"))
        creation_5m, creation_1h = self._cache_creation_breakdown(usage)
        if (
            creation is not None
            and creation_5m is not None
            and creation_1h is not None
            and creation != creation_5m + creation_1h
        ):
            logger.warning("claude_cache_creation_breakdown_mismatch")
        output = integer(usage.get("output_tokens"))
        total_input = (
            incoming + cached + creation
            if incoming is not None and cached is not None and creation is not None
            else None
        )
        return ChatResponse(
            content=content,
            latency_seconds=0,
            provider_request_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            reasoning_content="\n".join(reasoning) or None,
            tool_calls=() if duplicate_call_ids else tuple(calls),
            prompt_tokens=total_input,
            completion_tokens=output,
            cached_prompt_tokens=cached,
            cache_creation_input_tokens=creation,
            cache_creation_5m_input_tokens=creation_5m,
            cache_creation_1h_input_tokens=creation_1h,
            native_tool_events=tuple(native_events),
            citations=tuple(citations),
            total_tokens=total_input + output
            if total_input is not None and output is not None
            else None,
            status=(
                ModelResponseStatus.INCOMPLETE
                if duplicate_call_ids or truncated or stop == "pause_turn"
                else ModelResponseStatus.COMPLETED
            ),
            incomplete_reason=(
                "duplicate_tool_call_id"
                if duplicate_call_ids
                else "pause_turn"
                if stop == "pause_turn"
                else "max_output_tokens"
                if truncated
                else None
            ),
            continuation=ProviderContinuation(
                provider=self.provider_name,
                protocol=self.protocol,
                payload=tuple(tail),
            ),
        )
