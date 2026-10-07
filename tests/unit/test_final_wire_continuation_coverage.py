"""Identical application messages do not imply identical native continuation bodies."""

from dataclasses import replace

import httpx
import pytest
from tests.support.correctness_wire import KINDS

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ProviderContinuation
from qq_ai_bot.llm.wire_diagnostics import WireRequestObserver
from qq_ai_bot.model_runtime.executor import provider_cache_shape_diagnostics


@pytest.mark.parametrize("kind", KINDS)
async def test_final_wire_observer_detects_opaque_changes_outside_application_hash(kind):
    cls, vendor, protocol = KINDS[kind]
    payloads = {
        "gemini": {"role": "model", "parts": [{"text": "same", "thoughtSignature": "one"}]},
        "anthropic": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "same", "signature": "one"}],
        },
        "chat": {"role": "assistant", "content": "same", "reasoning_content": "one"},
        "responses": {"type": "reasoning", "encrypted_content": "one", "summary": []},
    }
    original = payloads[kind]
    import json

    changed = json.loads(json.dumps(original).replace('"one"', '"two"'))
    base = ChatRequest(
        messages=(ChatMessage("system", "fixed"), ChatMessage("user", "same")),
        model="synthetic",
        request_chain_id="same-chain",
    )
    requests = tuple(
        replace(base, continuation=ProviderContinuation(vendor, protocol.value, (p,)))
        for p in (original, changed)
    )
    shapes = [
        provider_cache_shape_diagnostics(
            r, provider=vendor, model=r.model, profile_id="same", protocol=protocol.value
        )
        for r in requests
    ]
    assert shapes[0] == shapes[1]
    assert "excludes native continuation" in shapes[0].coverage
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as client:
        adapter = cls(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
            **({"provider_name": "openai"} if kind == "chat" else {}),
        )
        bodies = [adapter._build_payload(r) for r in requests]
    observer = WireRequestObserver()
    observer.observe(bodies[0], protocol.value, chain_id="same-chain", provider=vendor)
    actual = observer.observe(bodies[1], protocol.value, chain_id="same-chain", provider=vendor)
    assert actual["relation"] == "input_rewritten"
    assert actual["first_difference_index"] == 1
    assert actual["contract_change"] == "unchanged"
