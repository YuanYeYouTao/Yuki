"""Reflection wire, budget and report contracts without unrelated full-suite work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
)
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.model_runtime.request_accounting import (
    after_provider_request,
    before_provider_request,
)


@pytest.mark.asyncio
async def test_each_physical_transport_attempt_is_accounted():
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": {"code": "busy"}})
        return httpx.Response(
            200,
            json={
                "id": "r",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "{}"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )

    reserve = AsyncMock()
    finish = AsyncMock()
    t = before_provider_request.set(reserve)
    u = after_provider_request.set(finish)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://example.test"
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://example.test",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=1,
            client=client,
        )
        try:
            await provider.complete(
                ChatRequest(model="test", messages=(ChatMessage(role="user", content="test"),))
            )
        finally:
            before_provider_request.reset(t)
            after_provider_request.reset(u)
    assert reserve.await_count == finish.await_count == 2
    assert finish.await_args_list[-1].args == ("completed", 3)
    from qq_ai_bot.memory.self_reflection.reporting import deliver_report

    # A report receipt remains authoritative even if its original event is gone.
    prior = SimpleNamespace(
        status=SimpleNamespace(value="succeeded"),
        model_dump=lambda **kwargs: {"status": "succeeded"},
    )
    social = SimpleNamespace(
        receipts=SimpleNamespace(find=AsyncMock(return_value=prior)), execute=AsyncMock()
    )
    assert await deliver_report(social, {"id": "sr_same"}) == {"status": "succeeded"}
    social.execute.assert_not_awaited()
