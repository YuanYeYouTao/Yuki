"""Synthetic Provider wire responses and captured-request inspection per dialect."""

import json
from dataclasses import asdict

DIALECTS = [
    ("openai_compatible", "chat_completions"),
    ("openai", "responses"),
    ("anthropic", "anthropic_messages"),
    ("gemini", "gemini"),
]
SIGNATURE = "synthetic-private-continuation"


def answer(response, protocol, sequence):
    calls = response.tool_calls
    if protocol == "chat_completions":
        message = {"role": "assistant", "content": response.content}
        if calls:
            message.update(
                tool_calls=[asdict(c) for c in calls],
                reasoning_details=[{"type": "reasoning.encrypted", "data": SIGNATURE}],
            )
        return {
            "choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]
        }
    if protocol == "responses":
        output = (
            [
                {"id": f"reason-{sequence}", "type": "reasoning", "encrypted_content": SIGNATURE},
                *[
                    {
                        "id": f"item-{c.id}",
                        "type": "function_call",
                        "call_id": c.id,
                        "name": c.function.name,
                        "arguments": c.function.arguments,
                        "status": "completed",
                    }
                    for c in calls
                ],
            ]
            if calls
            else [
                {
                    "id": f"message-{sequence}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": response.content}],
                }
            ]
        )
        return {"id": f"r-{sequence}", "status": "completed", "output": output}
    if protocol == "anthropic_messages":
        content = (
            [
                {"type": "thinking", "thinking": "synthetic reasoning", "signature": SIGNATURE},
                *[
                    {
                        "type": "tool_use",
                        "id": c.id,
                        "name": c.function.name,
                        "input": json.loads(c.function.arguments),
                    }
                    for c in calls
                ],
            ]
            if calls
            else [{"type": "text", "text": response.content}]
        )
        return {"content": content, "stop_reason": "tool_use" if calls else "end_turn"}
    parts = (
        [
            {
                "functionCall": {
                    "id": c.id,
                    "name": c.function.name,
                    "args": json.loads(c.function.arguments),
                },
                "thoughtSignature": SIGNATURE,
            }
            for c in calls
        ]
        if calls
        else [{"text": response.content}]
    )
    return {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}]}


def wire_calls_and_results(payload, protocol):
    """Inspect captured bytes, never the Runner's normalized transcript."""
    if protocol == "chat_completions":
        messages = payload["messages"]
        calls = [c["function"]["name"] for m in messages for c in m.get("tool_calls", [])]
        results = [(m["tool_call_id"], m["content"]) for m in messages if m["role"] == "tool"]
    elif protocol == "responses":
        calls = [i["name"] for i in payload["input"] if i.get("type") == "function_call"]
        results = [
            (i["call_id"], i["output"])
            for i in payload["input"]
            if i.get("type") == "function_call_output"
        ]
    elif protocol == "anthropic_messages":
        blocks = [
            b for m in payload["messages"] if isinstance(m["content"], list) for b in m["content"]
        ]
        calls = [b["name"] for b in blocks if b["type"] == "tool_use"]
        results = [(b["tool_use_id"], b["content"]) for b in blocks if b["type"] == "tool_result"]
    else:
        parts = [p for c in payload["contents"] for p in c["parts"]]
        calls = [p["functionCall"]["name"] for p in parts if "functionCall" in p]
        results = [
            (p["functionResponse"]["id"], p["functionResponse"]["response"]["output"])
            for p in parts
            if "functionResponse" in p
        ]
    return calls, results
