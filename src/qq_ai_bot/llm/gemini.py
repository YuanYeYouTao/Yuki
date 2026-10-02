"""Native Gemini GenerateContent with lossless thought-signature replay."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from urllib.parse import quote

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
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMUnsupportedFeatureError,
)
from qq_ai_bot.llm.gemini_schema import response_schema
from qq_ai_bot.llm.json_http import JSONHTTPProvider
from qq_ai_bot.llm.protocol_state import checkpoint_items, integer, ordered_delta
from qq_ai_bot.llm.vendor_policy import ChatWireOptions, effort_value, thinking_budget, wire_options


class GeminiProvider(JSONHTTPProvider):
    provider_name = "gemini"
    protocol = "gemini"

    def __init__(self, *, options: ChatWireOptions | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.options = wire_options(self.provider_name, options)

    def _path(self, request: ChatRequest) -> str:
        return f"models/{quote(request.model.removeprefix('models/'), safe='')}:generateContent"

    def _request_headers(self) -> dict[str, str]:
        return {**self._headers, "x-goog-api-key": self._api_key}

    @staticmethod
    def _usage_diagnostics(payload: dict[str, Any]) -> dict[str, object]:
        usage = payload.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}
        output = integer(usage.get("candidatesTokenCount"))
        thinking = integer(usage.get("thoughtsTokenCount"))
        return {
            "usage": {
                "prompt_tokens": integer(usage.get("promptTokenCount")),
                "completion_tokens": output + (thinking or 0) if output is not None else None,
                "total_tokens": integer(usage.get("totalTokenCount")),
                "cached_prompt_tokens": integer(usage.get("cachedContentTokenCount")),
            }
        }

    def _message(self, message: ChatMessage) -> dict[str, Any]:
        if message.response_item is not None:
            raise LLMInvalidRequestError("opaque history requires its original protocol")
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"text": message.content})
        if message.images:
            if message.role != "user":
                raise LLMInvalidRequestError("images must be attached to a user message")
            for image in message.images:
                prefix, data = image.data_url.split(",", 1)
                parts.append({"inlineData": {"mimeType": prefix[5:].split(";")[0], "data": data}})
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.function.arguments)
            except ValueError as exc:
                raise LLMInvalidRequestError("invalid local tool arguments") from exc
            parts.append(
                {
                    "functionCall": {
                        "name": call.function.name,
                        "args": arguments,
                        "id": call.id,
                    }
                }
            )
        return {"role": "model" if message.role == "assistant" else "user", "parts": parts}

    def _result(self, item: FunctionCallOutput, history: list[dict[str, Any]]) -> dict[str, Any]:
        for message in reversed(history):
            ids = iter(message.get("_call_ids", ()))
            for part in message.get("parts", ()):
                call = part.get("functionCall")
                if not isinstance(call, dict):
                    continue
                local_id = next(ids, call.get("id"))
                if local_id != item.call_id:
                    continue
                result: dict[str, Any] = {
                    "name": call["name"],
                    "response": {"output": item.output},
                }
                if call.get("id"):
                    result["id"] = call["id"]
                return {"role": "user", "parts": [{"functionResponse": result}]}
        raise LLMInvalidRequestError("Gemini tool receipt has no matching call")

    def _tail(self, request: ChatRequest) -> list[dict[str, Any]]:
        tail = checkpoint_items(request, self.provider_name, self.protocol)
        history: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role in {"system", "developer"} and not history:
                continue
            if message.role == "tool":
                history.append(
                    self._result(
                        FunctionCallOutput(
                            call_id=message.tool_call_id or "",
                            output=message.content or "",
                        ),
                        history,
                    )
                )
            else:
                history.append(self._message(message))
        history.extend(tail)
        for item in ordered_delta(request):
            wire = (
                self._result(item, history)
                if isinstance(item, FunctionCallOutput)
                else self._message(item)
            )
            tail.append(wire)
            history.append(wire)
        return tail

    def _build_payload(self, request: ChatRequest) -> dict[str, Any]:
        if any(tool.type is not NativeToolType.WEB_SEARCH for tool in request.native_tools):
            raise LLMUnsupportedFeatureError("unsupported Gemini native tool")
        system: list[dict[str, Any]] = []
        contents: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role in {"system", "developer"} and not contents:
                system.append({"text": message.content or ""})
            elif message.role == "tool":
                contents.append(
                    self._result(
                        FunctionCallOutput(
                            call_id=message.tool_call_id or "",
                            output=message.content or "",
                        ),
                        contents,
                    )
                )
            else:
                contents.append(self._message(message))
        contents.extend(self._tail(request))
        # Parallel receipts belong to one user turn, in the original call order.
        combined: list[dict[str, Any]] = []
        for item in contents:
            if combined and item["role"] == combined[-1]["role"] == "user":
                combined[-1]["parts"].extend(deepcopy(item["parts"]))
            else:
                combined.append(deepcopy(item))
        contents = combined
        config: dict[str, Any] = {}
        if request.max_output_tokens is not None:
            config["maxOutputTokens"] = request.max_output_tokens
        if request.thinking_enabled:
            if self.options.reasoning == "budget":
                if request.model.removeprefix("models/").startswith("gemini-3.8-"):
                    raise LLMUnsupportedFeatureError(
                        "Gemini 3.8 requires thinkingLevel, not a fixed thinkingBudget"
                    )
                config["thinkingConfig"] = {
                    "thinkingBudget": thinking_budget(self.options, request.reasoning_effort)
                }
            elif self.options.reasoning == "gemini":
                config["thinkingConfig"] = {
                    "thinkingLevel": effort_value(self.options, request.reasoning_effort),
                }
            else:
                raise LLMUnsupportedFeatureError("Gemini requires thinking level or budget")
        if self.options.send_temperature and request.temperature is not None:
            config["temperature"] = request.temperature
        if request.response_format is not None:
            spec = request.response_format
            schema_spec = spec.get("json_schema")
            config["responseMimeType"] = "application/json"
            if spec.get("type") == "json_schema" and isinstance(schema_spec, dict):
                if self.options.gemini_schema_format == "response_schema":
                    config["responseSchema"] = response_schema(schema_spec["schema"])
                else:
                    config["responseJsonSchema"] = schema_spec["schema"]
            elif spec.get("type") != "json_object":
                raise LLMUnsupportedFeatureError("unsupported Gemini structured format")
        payload: dict[str, Any] = {
            "contents": [
                {k: deepcopy(v) for k, v in item.items() if not k.startswith("_")}
                for item in contents
            ],
            "generationConfig": config,
        }
        if system:
            payload["systemInstruction"] = {"parts": system}
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parametersJsonSchema": tool.parameters,
                        }
                        for tool in request.tools
                    ]
                }
            ]
            choice = request.tool_choice or "auto"
            mode = "AUTO" if choice == "auto" else "NONE" if choice == "none" else "ANY"
            function_config: dict[str, Any] = {"mode": mode}
            if choice not in {"auto", "none", "required"}:
                function_config["allowedFunctionNames"] = [choice]
            payload["toolConfig"] = {"functionCallingConfig": function_config}
        if request.native_tools:
            payload.setdefault("tools", []).append({"googleSearch": {}})
            payload.setdefault("toolConfig", {})["includeServerSideToolInvocations"] = True
            function_config = payload["toolConfig"].get("functionCallingConfig")
            if isinstance(function_config, dict) and function_config.get("mode") == "AUTO":
                # Tool context circulation rejects AUTO for Gemini 3 tool combinations.
                function_config["mode"] = "VALIDATED"
        return payload

    def _parse(self, response: httpx.Response, request: ChatRequest) -> ChatResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMInvalidResponseError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LLMInvalidResponseError("Gemini returned invalid response")
        candidates = payload.get("candidates")
        if (
            not isinstance(candidates, list)
            or len(candidates) != 1
            or not isinstance(candidates[0], dict)
        ):
            raise LLMInvalidResponseError(
                "Gemini returned no unique candidate",
                diagnostics=self._usage_diagnostics(payload),
            )
        candidate = candidates[0]
        if candidate.get("finishReason") not in {"STOP", "MAX_TOKENS"}:
            raise LLMInvalidResponseError(
                "Gemini response was blocked or not completed",
                diagnostics=self._usage_diagnostics(payload),
            )
        truncated = candidate.get("finishReason") == "MAX_TOKENS"
        content = candidate.get("content")
        if truncated and (
            content is None or (isinstance(content, dict) and "parts" not in content)
        ):
            content = {"role": "model", "parts": []}
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            raise LLMInvalidResponseError(
                "Gemini returned invalid parts",
                diagnostics=self._usage_diagnostics(payload),
            )
        texts: list[str] = []
        thoughts: list[str] = []
        calls: list[ToolCall] = []
        call_ids: list[str] = []
        native_events: list[NativeToolEvent] = []
        search_queries: dict[str, str] = {}
        tail = self._tail(request)
        seed = hashlib.sha256(response.content + str(len(tail)).encode()).hexdigest()[:20]
        for index, part in enumerate(content["parts"]):
            if not isinstance(part, dict):
                raise LLMInvalidResponseError("invalid Gemini content part")
            if not any(key in part for key in ("text", "functionCall", "toolCall", "toolResponse")):
                raise LLMInvalidResponseError("unsupported Gemini content part")
            if isinstance(part.get("text"), str):
                (thoughts if part.get("thought") else texts).append(part["text"])
            call = part.get("functionCall")
            if call is not None:
                if (
                    not isinstance(call, dict)
                    or not isinstance(call.get("name"), str)
                    or not call["name"]
                ):
                    raise LLMInvalidResponseError("invalid Gemini function call")
                args = call.get("args", {})
                if not isinstance(args, dict):
                    raise LLMInvalidResponseError("invalid Gemini function arguments")
                call_id = call.get("id") or f"gemini_{seed}_{index}"
                if not isinstance(call_id, str):
                    raise LLMInvalidResponseError("invalid Gemini function ID")
                call_ids.append(call_id)
                calls.append(
                    ToolCall(
                        id=call_id,
                        function=ToolFunction(
                            name=call["name"],
                            arguments=json.dumps(args, ensure_ascii=False),
                        ),
                    )
                )
            server_call = part.get("toolCall")
            if server_call is not None:
                if not request.native_tools or not isinstance(server_call, dict):
                    raise LLMInvalidResponseError("unexpected Gemini server tool call")
                if server_call.get("toolType") not in {"GOOGLE_SEARCH", "GOOGLE_SEARCH_WEB"}:
                    raise LLMInvalidResponseError("unsupported Gemini server tool call")
                call_id = server_call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise LLMInvalidResponseError("Gemini server tool call has no id")
                args = server_call.get("args")
                queries = args.get("queries") if isinstance(args, dict) else None
                search_queries[call_id] = (
                    queries[0]
                    if isinstance(queries, list) and queries and isinstance(queries[0], str)
                    else ""
                )
            server_result = part.get("toolResponse")
            if server_result is not None:
                if not request.native_tools or not isinstance(server_result, dict):
                    raise LLMInvalidResponseError("unexpected Gemini server tool result")
                call_id = server_result.get("id")
                if not isinstance(call_id, str) or call_id not in search_queries:
                    raise LLMInvalidResponseError("Gemini server tool result has no matching call")
                native_events.append(
                    NativeToolEvent(
                        tool_type=NativeToolType.WEB_SEARCH,
                        call_id=call_id,
                        status=NativeToolStatus.COMPLETED,
                        action_type="search",
                        query=search_queries[call_id],
                    )
                )
        if len(set(call_ids)) != len(call_ids):
            raise LLMInvalidResponseError("duplicate Gemini function IDs")
        text = "".join(texts)
        if not text and not calls and not truncated:
            raise LLMEmptyResponseError(
                "Gemini returned no visible text or tool calls",
                diagnostics=self._usage_diagnostics(payload),
            )
        if content["parts"]:
            tail.append(
                {"role": "model", "parts": deepcopy(content["parts"]), "_call_ids": call_ids}
            )
        usage = payload.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}
        grounding = candidate.get("groundingMetadata")
        grounding = grounding if isinstance(grounding, dict) else {}
        queries = grounding.get("webSearchQueries")
        queries = (
            [value for value in queries if isinstance(value, str)]
            if isinstance(queries, list)
            else []
        )
        chunks = grounding.get("groundingChunks")
        chunks = chunks if isinstance(chunks, list) else []
        citations: list[ResponseCitation] = []
        for chunk in chunks:
            web = chunk.get("web") if isinstance(chunk, dict) else None
            if not isinstance(web, dict):
                continue
            url = web.get("uri")
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                title = web.get("title")
                citations.append(
                    ResponseCitation(
                        url=url,
                        title=title if isinstance(title, str) else "",
                        origin=CitationOrigin.ANNOTATION,
                    )
                )
        if request.native_tools and (queries or citations) and not native_events:
            native_events.append(
                NativeToolEvent(
                    tool_type=NativeToolType.WEB_SEARCH,
                    call_id=f"gemini-search-{seed}",
                    status=NativeToolStatus.COMPLETED,
                    action_type="search",
                    query=queries[0] if queries else "",
                ),
            )
        output = integer(usage.get("candidatesTokenCount"))
        thinking = integer(usage.get("thoughtsTokenCount"))
        return ChatResponse(
            content=text,
            latency_seconds=0,
            provider_request_id=payload.get("responseId")
            if isinstance(payload.get("responseId"), str)
            else None,
            reasoning_content="\n".join(thoughts) or None,
            tool_calls=tuple(calls),
            prompt_tokens=integer(usage.get("promptTokenCount")),
            completion_tokens=output + (thinking or 0) if output is not None else None,
            total_tokens=integer(usage.get("totalTokenCount")),
            reasoning_tokens=thinking,
            cached_prompt_tokens=integer(usage.get("cachedContentTokenCount")),
            native_tool_events=tuple(native_events),
            citations=tuple(citations),
            status=ModelResponseStatus.INCOMPLETE if truncated else ModelResponseStatus.COMPLETED,
            incomplete_reason="max_output_tokens" if truncated else None,
            continuation=ProviderContinuation(
                provider=self.provider_name,
                protocol=self.protocol,
                payload=tuple(tail),
            ),
        )
