"""Standard Responses wire policy, separate from DeepSeek-specific omissions."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.domain.messages import ChatRequest
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider


class OpenAIResponsesProvider(DeepSeekResponsesProvider):
    """Reuse ordered item parsing while preserving standard execution controls."""

    provider_name = "openai"
    supports_tool_choice = True

    def _build_payload(self, request: ChatRequest) -> dict[str, Any]:
        payload = super()._build_payload(request)
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        # Yuki replays its own ordered transcript, rather than a remote stored thread.
        payload["store"] = False
        payload["include"] = ["reasoning.encrypted_content"]
        return payload


class OpenAICompatibleResponsesProvider(OpenAIResponsesProvider):
    """Keep compatible endpoints in their configured provider continuation domain."""

    provider_name = "openai_compatible"
