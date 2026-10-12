"""Opt-in paid replay of exported Main requests; no business tools are executed."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import TypeAdapter
from tools.cache_probe import route_from_stdin

from qq_ai_bot.domain.messages import ChatRequest
from qq_ai_bot.model_runtime.models import ModelProtocol
from qq_ai_bot.model_runtime.pool import ModelClientPool


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


async def replay(args: argparse.Namespace) -> None:
    profile, key = route_from_stdin(sys.stdin)
    root = args.replay_root / args.variant
    requests = [
        TypeAdapter(ChatRequest).validate_json(line)
        for line in (root / "chatrequests.jsonl").read_text("utf-8").splitlines()
    ]
    cases = json.loads((root / "cases.json").read_text("utf-8"))
    prepared = (
        [
            json.loads(line)
            for line in (root / "gemini.bodies.jsonl").read_text("utf-8").splitlines()
        ]
        if profile.protocol is ModelProtocol.GEMINI
        else None
    )
    args.output.mkdir(parents=True, exist_ok=True)
    sequence = [int(value) for value in args.sequence.split(",")]
    records: list[dict[str, object]] = []
    async with AsyncExitStack() as stack:
        pool = ModelClientPool(secret_overrides={profile.api_key_env: key})
        stack.push_async_callback(pool.close)
        provider = pool.get(profile)
        for ordinal, index in enumerate(sequence):
            request = replace(
                requests[index],
                model=profile.model,
                max_output_tokens=profile.default_max_output_tokens,
                thinking_enabled=profile.thinking_enabled,
                reasoning_effort=profile.reasoning_effort,
            )
            # Native replay uses the sampled prepared fixed configuration unchanged.
            # Chat uses the selected deployed vendor adapter, including no DeepSeek
            # tool_choice. Never map a generic Chat export into a vendor by hand.
            payload = prepared[index] if prepared is not None else provider._build_payload(request)
            headers = provider._request_headers()
            if request.provider_session_id is not None:
                headers["x-session-id"] = request.provider_session_id
            fixed = (
                {key: value for key, value in payload.items() if key != "contents"}
                if prepared is not None
                else {key: value for key, value in payload.items() if key != "messages"}
            )
            record: dict[str, object] = {
                "name": args.name,
                "variant": args.variant,
                "ordinal": ordinal,
                "case": cases[index]["name"],
                "started_at": datetime.now(UTC).isoformat(),
                "provider": profile.provider,
                "protocol": profile.protocol.value,
                "model": profile.model,
                "body_sha256": digest(payload),
                "fixed_sha256": digest(fixed),
                "body_bytes": len(json.dumps(payload, ensure_ascii=False).encode()),
                "runtime_characters": cases[index].get("dynamic_characters"),
                "session_metadata": request.provider_session_id,
                "client_dispatches": 1,
            }
            (args.output / f"{ordinal:03}-request.private.json").write_text(
                json.dumps(payload, ensure_ascii=False), "utf-8"
            )
            started = time.perf_counter()
            try:
                response = await provider._client.post(
                    provider._path(request),
                    headers=headers,
                    json=payload,
                    timeout=profile.timeout_seconds,
                )
                (args.output / f"{ordinal:03}-response.private.txt").write_bytes(response.content)
                record["http_status"] = response.status_code
                if response.is_success:
                    record.update(provider._usage_diagnostics(response.json()))
                    record["response_json_sha256"] = digest(response.json())
            except (httpx.HTTPError, ValueError) as exc:
                record["error_type"] = type(exc).__name__
            record["latency_seconds"] = time.perf_counter() - started
            records.append(record)
            (args.output / "results.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2), "utf-8"
            )
            print(json.dumps(record, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--replay-root", type=Path, required=True)
    parser.add_argument("--variant", choices=("old", "current"), required=True)
    parser.add_argument("--sequence", default="0,1,2,0,1,2")
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
