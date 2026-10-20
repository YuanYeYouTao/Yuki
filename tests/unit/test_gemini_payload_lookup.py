"""One Gemini payload resolves original receipts with linear local lookup work."""

import json

import httpx
import pytest

from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    FunctionCallOutput,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from yuki.llm.gemini import GeminiProvider


@pytest.mark.parametrize("source", ["checkpoint", "base-history"])
async def test_many_original_receipts_keep_native_pairing_without_quadratic_lookup(
    monkeypatch, source
):
    visits = 0

    class CountedPart(dict):
        def get(self, key, *args):
            nonlocal visits
            if key == "functionCall":
                visits += 1
            return super().get(key, *args)

    def unexpected_http(request):
        raise AssertionError("Payload projection does not make model requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_http)) as client:
        adapter = GeminiProvider(
            base_url="https://model.invalid",
            api_key="offline",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        message = adapter._message

        def counted_message(item):
            wire = message(item)
            wire["parts"] = [CountedPart(part) for part in wire["parts"]]
            return wire

        monkeypatch.setattr(adapter, "_message", counted_message)
        costs = []
        for size in (16, 640):
            outputs = tuple(
                FunctionCallOutput(f"local-{i}", json.dumps({"ok": i % 5 != 0, "value": i}))
                for i in range(size)
            )
            opaque = {
                "role": "model",
                "parts": [
                    CountedPart(
                        functionCall={"name": "inspect", "id": f"native-{i}", "args": {"i": i}},
                        thoughtSignature=f"original-signature-{i}",
                        vendorOpaque={"original": i},
                    )
                    for i in range(size)
                ],
                "_call_ids": [f"local-{i}" for i in range(size)],
            }
            if source == "checkpoint":
                request = ChatRequest(
                    (ChatMessage("system", "fixed instructions"), ChatMessage("user", "continue")),
                    continuation=ProviderContinuation("gemini", "gemini", (opaque,)),
                    continuation_items=outputs,
                )
            else:
                request = ChatRequest(
                    (
                        ChatMessage("system", "fixed instructions"),
                        ChatMessage(
                            "assistant",
                            "",
                            tool_calls=tuple(
                                ToolCall(
                                    f"local-{i}", ToolFunction("inspect", json.dumps({"i": i}))
                                )
                                for i in range(size)
                            ),
                        ),
                        *(
                            ChatMessage("tool", result.output, tool_call_id=result.call_id)
                            for result in outputs
                        ),
                    )
                )
            original = json.dumps(opaque, sort_keys=True)
            visits = 0
            payload = adapter._build_payload(request)
            costs.append(visits)
            responses = [
                part["functionResponse"]
                for item in payload["contents"]
                for part in item["parts"]
                if "functionResponse" in part
            ]
            assert [item["id"] for item in responses] == [
                f"{'native' if source == 'checkpoint' else 'local'}-{i}" for i in range(size)
            ]
            assert [list(item["response"].values()) for item in responses] == [
                [result.output] for result in outputs
            ]
            assert [next(iter(item["response"])) for item in responses] == [
                "error" if i % 5 == 0 else "output" for i in range(size)
            ]
            if source == "checkpoint":
                native_parts = next(
                    item["parts"] for item in payload["contents"] if item["role"] == "model"
                )
                assert native_parts == opaque["parts"]
            assert json.dumps(opaque, sort_keys=True) == original
            assert all("_call_ids" not in item for item in payload["contents"])
        # Output size grows 40x. Lookup work may grow proportionally, not quadratically.
        assert costs[0] > 0 and costs[1] <= costs[0] * (640 // 16) * 4, costs
