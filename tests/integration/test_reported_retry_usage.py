"""Synthetic compatible error envelopes; real HTTP adapters/executor/SQL aggregation."""

import pytest
from tests.support.usage_retry_cases import KINDS, body, invoke, sql_summary


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("final_status", [200, 503])
async def test_known_retry_usage_is_counted_once(kind, final_status):
    result = await invoke(kind, [(503, body(kind, 80)), (final_status, body(kind, 0))], 1)
    row = result["record"]
    assert (row["prompt_tokens"], row["cached_prompt_tokens"], row["total_tokens"]) == (
        200,
        80,
        220,
    )
    assert row["physical_request_count"] == result["physical_mock_requests"] == 2
    assert row["unknown_usage_request_count"] == 0
    assert result["error"] == (None if final_status == 200 else "LLMUnavailableError")
    summary = await sql_summary([row])
    assert summary["input_tokens"] == 200 and summary["cached_input_tokens"] == 80


@pytest.mark.parametrize("kind", KINDS)
async def test_unknown_retry_response_remains_missing(kind):
    result = await invoke(kind, [(503, {"error": {"type": "overloaded"}}), (200, body(kind, 0))], 1)
    row = result["record"]
    assert row["physical_request_count"] == 2
    assert row["unknown_usage_request_count"] == 1
    assert row["prompt_tokens"] is None and row["cached_prompt_tokens"] is None
    assert result["error"] is None


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("cache", [60, 0, None])
async def test_single_response_preserves_explicit_zero_and_missing_cache(kind, cache):
    result = await invoke(kind, [(200, body(kind, cache))])
    row = result["record"]
    assert row["cached_prompt_tokens"] == cache
    assert row["physical_request_count"] == 1
    assert result["error"] is None
    if cache is not None:
        assert row["prompt_tokens"] == 100 and row["total_tokens"] == 110


@pytest.mark.parametrize("retry_first", [False, True])
async def test_claude_pause_and_retry_do_not_double_count(retry_first):
    sequence = [(200, body("claude", 60, pause=True)), (200, body("claude", 0))]
    if retry_first:
        sequence.insert(0, (503, body("claude", 80)))
    result = await invoke("claude", sequence, 1 if retry_first else 0)
    row = result["record"]
    count = len(sequence)
    assert (row["prompt_tokens"], row["total_tokens"]) == (100 * count, 110 * count)
    assert row["cached_prompt_tokens"] == (140 if retry_first else 60)
    assert row["physical_request_count"] == count and row["unknown_usage_request_count"] == 0


async def test_partial_claude_usage_does_not_invent_complete_input_coverage():
    partial = body("claude", 200, total=230)
    del partial["usage"]["cache_creation_input_tokens"]
    complete = await invoke("chat", [(200, body("chat", 60))])
    missing = await invoke("claude", [(200, partial)])
    assert missing["record"]["prompt_tokens"] is None
    assert missing["record"]["cached_prompt_tokens"] == 200
    summary = await sql_summary([complete["record"], missing["record"]])
    assert summary["cached_input_tokens"] == 260
    assert summary["input_tokens"] == 100 and summary["cache_reported_cached_tokens"] == 60
