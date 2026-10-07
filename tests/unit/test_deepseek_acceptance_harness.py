"""Unpaid MockTransport proof of the opt-in real Provider fixture/quotas."""

import httpx
import pytest
from scripts import verify_deepseek_codemode as real
from tests.integration.test_codemode_provider_wire import answer
from tests.integration.test_codemode_runner import BINARY, call

from qq_ai_bot.domain.messages import ChatResponse


@pytest.mark.skipif(not BINARY.is_file(), reason="requires pinned native Monty worker")
@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
@pytest.mark.parametrize("case", ["direct", "code", "truncation", "disconnect"])
async def test_unpaid_assembly(database, tmp_path, monkeypatch, protocol, case):
    monkeypatch.setattr(
        real,
        "CREDENTIALS",
        {
            "api_key": "synthetic",
            "model": "deepseek-flash",
            "base_url (openai)": "https://code.invalid",
            "base_url (anthropic)": "https://code.invalid/anthropic",
        },
    )
    monkeypatch.setattr(real, "RECORDS", [])
    monkeypatch.setattr(real, "PHYSICAL_CALLS", 0)
    monkeypatch.setattr(real, "RESERVED_USD", 0.0)
    script = (
        "r = await yuki_workspace_read({'path':'numbers.json'})\n"
        "n = sum(r['data']['values'])\n"
        "await yuki_workspace_write({'path':'result.txt','text':str(n)})\n"
        "n"
    )
    steps = iter(
        [call("execute_code", {"code": script}, "code"), ChatResponse("TASK_OK_18", 0)]
        if case == "code"
        else [
            call("workspace_read", {"path": "numbers.json"}, "read"),
            call("workspace_write", {"path": "result.txt", "text": "18"}, "write"),
            ChatResponse("TASK_OK_18", 0),
        ]
    )

    def transport(request):
        if case == "truncation":
            if protocol == "responses":
                return httpx.Response(
                    200,
                    json={
                        "id": "partial",
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "output": [],
                    },
                )
            if protocol == "anthropic_messages":
                return httpx.Response(
                    200,
                    json={
                        "content": [{"type": "thinking", "thinking": "x"}],
                        "stop_reason": "max_tokens",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": None, "reasoning_content": "x"},
                            "finish_reason": "length",
                        }
                    ]
                },
            )
        return httpx.Response(200, json=answer(next(steps), protocol, 1))

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    await real.provider_case(database, tmp_path, protocol, case)

    record = real.RECORDS[-1]
    assert record["real_business_effects"] == 0
    assert record["duplicates"] == 0
    assert record["request_shapes_fixed"]
    assert real.PHYSICAL_CALLS <= 3
    assert 0 <= real.RESERVED_USD < 1
