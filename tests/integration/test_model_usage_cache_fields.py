"""Real usage queries preserve returned cache fields across supplier names."""

import httpx
import pytest
from sqlalchemy import select

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.persistence.control_activity_query import ControlActivityQueryAdapter


@pytest.mark.parametrize(
    ("creation", "expected", "ttl_missing"),
    [
        (
            {
                "cache_creation_input_tokens": 5,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 3,
                    "ephemeral_1h_input_tokens": 2,
                },
            },
            (5, 3, 2),
            0,
        ),
        (
            {
                "cache_creation_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                },
            },
            (0, 0, 0),
            0,
        ),
        ({"cache_creation_input_tokens": 0}, (0, None, None), 0),
        ({}, (None, None, None), 0),
        ({"cache_creation_input_tokens": 5}, (5, None, None), 1),
        (
            {
                "cache_creation_input_tokens": 5,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 3,
                    "ephemeral_1h_input_tokens": 1,
                },
            },
            (5, 3, 1),
            1,
        ),
    ],
)
async def test_claude_cache_statistics_use_returned_fields_across_supplier_names(
    database, creation, expected, ttl_missing
):
    body = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "done"}],
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "output_tokens": 2,
            **creation,
        },
    }
    repository = ModelInvocationRepository(database)
    async with httpx.AsyncClient(
        base_url="https://model.invalid/v1",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        for supplier in ("anthropic", "agm"):
            provider = AnthropicMessagesProvider(
                base_url="https://model.invalid/v1",
                api_key="fixture",
                timeout_seconds=10,
                max_retries=0,
                client=client,
                provider_name=supplier,
            )
            response = await provider.complete(
                ChatRequest(
                    messages=(ChatMessage("user", "fixture"),),
                    model="claude-fixture",
                    thinking_enabled=False,
                )
            )
            assert response.continuation is not None
            assert response.continuation.provider == supplier
            assert response.continuation.protocol == "anthropic_messages"
            await repository.record(
                task=ModelTask.CHAT_AGENT,
                profile_id=supplier,
                provider=supplier,
                model="claude-fixture",
                success=True,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens,
                cached_prompt_tokens=response.cached_prompt_tokens,
                cache_creation_input_tokens=response.cache_creation_input_tokens,
                cache_creation_5m_input_tokens=response.cache_creation_5m_input_tokens,
                cache_creation_1h_input_tokens=response.cache_creation_1h_input_tokens,
                latency_seconds=response.latency_seconds,
                error_category=None,
            )

    query = ControlActivityQueryAdapter(database.sessions, workspace=None)
    summary = (await query.read_model_usage_summary("24h")).fields
    write, five_minutes, one_hour = expected
    fields = {
        "cache_write_input_tokens": write,
        "cache_write_classified_input_tokens": write,
        "cache_write_5m_input_tokens": five_minutes,
        "cache_write_1h_input_tokens": one_hour,
        "cache_write_unreported_calls": int(write is None),
        "cache_write_ttl_unreported_calls": ttl_missing,
        "cache_write_5m_reported_calls": int(five_minutes is not None),
        "cache_write_1h_reported_calls": int(one_hour is not None),
    }
    assert {row["provider"] for row in summary["models"]} == {"anthropic", "agm"}
    for dimension in ("models", "profiles", "model_buckets"):
        for row in summary[dimension]:
            assert {key: row[key] for key in fields} == fields
    combined = {key: value * 2 if value is not None else None for key, value in fields.items()}
    assert {key: summary[key] for key in fields} == combined
    for dimension in ("tasks", "buckets"):
        assert len(summary[dimension]) == 1
        assert {key: summary[dimension][0][key] for key in fields} == combined

    async with database.sessions() as session:
        rows = (await session.scalars(select(ModelInvocationModel))).all()
        assert [
            (
                row.provider,
                row.cache_creation_input_tokens,
                row.cache_creation_5m_input_tokens,
                row.cache_creation_1h_input_tokens,
            )
            for row in rows
        ] == [(supplier, *expected) for supplier in ("anthropic", "agm")]


@pytest.mark.parametrize("supplier", ["gemini", "deepseek"])
async def test_absent_cache_creation_is_unknown_in_usage_statistics(database, supplier):
    await ModelInvocationRepository(database).record(
        task=ModelTask.CHAT_AGENT,
        profile_id=supplier,
        provider=supplier,
        model="fixture",
        success=True,
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        cached_prompt_tokens=0,
        latency_seconds=0,
        error_category=None,
    )
    query = ControlActivityQueryAdapter(database.sessions, workspace=None)
    summary = (await query.read_model_usage_summary("24h")).fields
    for row in (summary, *summary["models"], *summary["profiles"], *summary["buckets"]):
        assert row["cache_write_input_tokens"] is None
        assert row["cache_write_classified_input_tokens"] is None
        assert row["cache_write_5m_input_tokens"] is None
        assert row["cache_write_1h_input_tokens"] is None
        assert row["cache_write_unreported_calls"] == 1
        assert row["cache_write_5m_reported_calls"] == 0
        assert row["cache_write_1h_reported_calls"] == 0
