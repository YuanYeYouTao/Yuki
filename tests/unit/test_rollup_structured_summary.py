"""Structured continuity retains correction provenance without claiming truth."""

import json
from dataclasses import replace

import httpx
import pytest
from tests.unit.test_rollup_complete_sources import _seed_private, candidate, event

from qq_ai_bot.conversation.rollup.errors import RollupSourceChangedError
from qq_ai_bot.conversation.rollup.models import RollupKind, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.conversation.rollup.summary import parse_summary, summary_references
from qq_ai_bot.domain.messages import ChatResponse, ReasoningEffort
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.gemini import GeminiProvider


def summary(*, ids=(42,), continuity="等待确认日期", issues=(), corrections=()):
    return json.dumps(
        {
            "schema": "conversation_rollup_v1",
            "continuity": continuity,
            "source_event_ids": list(ids),
            "open_issues": list(issues),
            "corrections": list(corrections),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [GeminiProvider, AnthropicMessagesProvider])
async def test_rollup_schema_reaches_real_gemini_and_claude_wire_without_tools(provider_type):
    seen = []

    def transport(request):
        body = json.loads(request.content)
        seen.append(body)
        assert not body.get("tools") and not body.get("toolConfig")
        if provider_type is GeminiProvider:
            config = body["generationConfig"]
            assert config["responseMimeType"] == "application/json"
            schema = config["responseJsonSchema"]
            payload = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": summary()}]},
                    }
                ]
            }
        else:
            schema = body["output_config"]["format"]["schema"]
            assert body["output_config"]["format"]["type"] == "json_schema"
            payload = {"content": [{"type": "text", "text": summary()}], "stop_reason": "end_turn"}
        assert schema["properties"]["schema"]["enum"] == ["conversation_rollup_v1"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider_type(
            base_url="https://wire.invalid",
            api_key="fake-key",
            client=client,
            timeout_seconds=1,
            max_retries=0,
        )

        class Model:
            async def execute(self, _task, request, **_kwargs):
                assert request.structured_output
                assert not request.tools and not request.native_tools
                return await adapter.complete(
                    replace(
                        request,
                        model="gemini-3.8-flash"
                        if provider_type is GeminiProvider
                        else "claude-test",
                        thinking_enabled=True,
                        reasoning_effort=ReasoningEffort.LOW,
                    )
                )

        service = ConversationRollupService(
            models=Model(), config=RollupPolicyConfig(), timeout_seconds=2
        )
        output, kind = await service.summarize_candidate(replace(candidate(), events=(event(),)))
        assert kind is RollupKind.MODEL and parse_summary(output)["source_event_ids"] == [42]
        assert len(seen) == 1


@pytest.mark.parametrize(
    "change,error",
    [
        ({"source_event_ids": [True]}, "invalid_references"),
        ({"source_event_ids": [42, 42]}, "invalid_references"),
        ({"open_issues": [{"text": "x", "source_event_ids": [42]}] * 17}, "too_many_items"),
        ({"corrections": [{"text": "x", "source_event_ids": [42]}]}, "invalid_item"),
    ],
)
def test_structural_limits_reject_invalid_or_unbounded_views(change, error):
    value = json.loads(summary())
    value.update(change)
    with pytest.raises(ValueError, match=error):
        parse_summary(json.dumps(value))


@pytest.mark.asyncio
async def test_new_model_output_cannot_invent_reference_or_remain_free_text():
    class Model:
        output = summary(ids=(999,))

        async def execute(self, *_args, **_kwargs):
            return ChatResponse(content=self.output, latency_seconds=0)

    model = Model()
    service = ConversationRollupService(
        models=model, config=RollupPolicyConfig(), timeout_seconds=2
    )
    with pytest.raises(ValueError, match="unsupplied_reference"):
        await service.summarize_candidate(candidate())
    model.output = "Looks like a plausible but unstructured summary."
    with pytest.raises(ValueError, match="invalid_json"):
        await service.summarize_candidate(candidate())
    assert service.metrics.model_summaries == 0


@pytest.mark.asyncio
async def test_recursive_correction_replaces_old_claim_and_keeps_source_and_open_issue():
    correction = {
        "text": "日期已更正为周一；周五是旧错误。",
        "source_event_ids": [43],
        "supersedes_event_ids": [42],
    }
    issue = {"text": "用户尚未批准发布。", "source_event_ids": [42]}

    class Model:
        calls = 0

        async def execute(self, _task, request, **_kwargs):
            self.calls += 1
            assert not request.tools and not request.native_tools
            assert "New corrections supersede" in request.messages[0].content
            body = request.messages[-1].content
            if self.calls == 1:
                assert "Legacy narrative; source references unverified" in body
                output = summary(continuity="暂定周五；未批准发布。", issues=(issue,))
            else:
                if self.calls == 2:
                    assert "更正：改为周一" in body
                else:
                    carried = body.split("Previous summary:\n", 1)[1].split("\n\n", 1)[0]
                    value = parse_summary(carried)
                    assert value["corrections"] == [correction]
                    assert value["open_issues"] == [issue]
                    assert value["continuity"] == "当前日期周一；未批准发布。"
                output = summary(
                    ids=(42, 43),
                    continuity="当前日期周一；未批准发布。",
                    issues=(issue,),
                    corrections=(correction,),
                )
            return ChatResponse(content=output, latency_seconds=0)

    service = ConversationRollupService(
        models=Model(), config=RollupPolicyConfig(), timeout_seconds=2
    )
    previous = "此前群里讨论日期，需要保留未批准发布的约束。"
    for index, body in enumerate(("日期周五；尚未批准发布", "更正：改为周一", "另一话题")):
        source = replace(event(), id=42 + index, content=body, segments=())
        current = replace(
            candidate(),
            previous_summary=previous,
            source_coverage=41 + index,
            source_rollup_revision=index,
            events=(source,),
        )
        previous, kind = await service.summarize_candidate(current)
        assert kind is RollupKind.MODEL
    final = parse_summary(previous)
    assert final["corrections"] == [correction]
    assert final["open_issues"] == [issue]
    assert summary_references(final) == {42, 43}


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_commit_checks_real_reference_owner_before_coverage_write(database, missing):
    policy = RollupPolicyConfig(context_token_budget=100)
    await _seed_private(database, peer="foreign-source", count=1, policy=policy)
    scope = await _seed_private(database, peer="correct-source", count=8, policy=policy)
    repository = ConversationRollupRepository(database, config=policy)
    claim = await repository.claim_scope_for_foreground(
        scope, lease_owner="reference-test", lease_seconds=30
    )
    assert claim is not None
    current = await repository.candidate_for_claim(claim)
    assert current is not None
    invalid_id = 999999 if missing else 1
    with pytest.raises(RollupSourceChangedError, match="reference_missing_or_out_of_scope"):
        await repository.commit_candidate(
            claim, current, summary_text=summary(ids=(invalid_id,)), summary_kind=RollupKind.MODEL
        )
    state, checkpoint, job = await repository.status(scope)
    assert state is not None and checkpoint is None
    assert job is not None and job["status"] == "processing"
    # The rejected write did not consume the claim or advance the prefix.
    valid = await repository.commit_candidate(
        claim,
        current,
        summary_text=summary(ids=(current.events[0].id,)),
        summary_kind=RollupKind.MODEL,
    )
    assert valid.rollup.covered_through_event_id == current.events[-1].id
