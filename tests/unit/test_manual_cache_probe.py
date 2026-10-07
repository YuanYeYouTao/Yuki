"""Offline validation only: the manual real-API experiment is never auto-run."""

import json
from itertools import pairwise

import httpx
import pytest
from tools.cache_probe import PhysicalRequests, prefix_comparison, run_experiment, weighted_usage

from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    StructuredOutputMode,
)


def profile(*, retries=0):
    return ModelProfile(
        id="cache-mock",
        provider="gemini",
        protocol=ModelProtocol.GEMINI,
        base_url="https://gemini.invalid/v1beta",
        api_key_env="CACHE_PROBE_UNUSED",
        model="gemini-3.8-flash",
        timeout_seconds=2,
        max_retries=retries,
        default_temperature=0,
        default_max_output_tokens=1024,
        structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
        capabilities=frozenset(
            {
                ModelCapability.TOOLS,
                ModelCapability.REASONING,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        ),
    )


def reply(parts=None, usage=None):
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"role": "model", "parts": parts or [{"text": "ok"}]},
            }
        ],
        **({"usageMetadata": usage} if usage is not None else {}),
    }


def test_weighted_cache_usage_preserves_unknown_zero_and_invalid_without_double_counting():
    totals = weighted_usage(
        [
            {"prompt_tokens": 100, "cached_prompt_tokens": 80},
            {"prompt_tokens": 200, "cached_prompt_tokens": 0},
            {"prompt_tokens": 300, "cached_prompt_tokens": None},
            {"prompt_tokens": None, "cached_prompt_tokens": 20},
            {"prompt_tokens": 10, "cached_prompt_tokens": 11},
        ]
    )
    assert totals["cache_known"] == 2
    assert totals["cache_unknown_or_invalid"] == 3
    assert totals["explicit_zero"] == 1
    assert totals["weighted_cached_input_ratio"] == 80 / 300


def test_manual_work_provenance_records_sections_without_generated_text_or_arbitrary_refs():
    from tools.cache_probe import summary_reference_shape

    summary = {
        "version": 1,
        "task_directives": [{"text": "generated-private-marker", "refs": ["record:3"]}],
        "superseded_directives": [],
        "input_dispositions": [{"input_ref": "record:3", "kind": "context", "reason": "private"}],
        "completed": [{"text": "other-private-marker", "refs": ["unsafe-ref-marker"]}],
        "pending": [],
        "failures": [],
        "artifacts": [],
        "next_steps": [],
    }
    source = {
        "source_refs": ["goal", "event:8", "record:3"],
        "original_request_ref": "event:8",
        "task_material": {"directives": [{"refs": ["event:8"]}]},
        "paging": {"cursor": [2, 0]},
    }
    result = summary_reference_shape(json.dumps(summary), source)
    assert result["sections"]["task_directives"] == [["record:3"]]
    assert result["non_directive_source_refs"] == ["record:3"]
    assert result["prior_directive_refs"] == [["event:8"]]
    assert result["paging"] == [2, 0]
    assert result["input_dispositions"] == [
        {
            "input_ref": "record:3",
            "kind": "context",
            "reason_nonempty": True,
            "supplied_task_input": False,
        }
    ]
    assert len(result["outside_supplied_refs"]) == 1
    encoded = json.dumps(result)
    assert "generated-private-marker" not in encoded
    assert "other-private-marker" not in encoded and "unsafe-ref-marker" not in encoded


def test_actual_gemini_part_prefix_handles_coalesced_messages_and_fixed_tool_changes():
    first = {
        "contents": [{"role": "user", "parts": [{"text": "history"}, {"text": "q1"}]}],
        "tools": [{"functionDeclarations": [{"name": "read", "parameters": {"x": 1}}]}],
        "generationConfig": {"maxOutputTokens": 8192},
    }
    second = {
        **first,
        "contents": [{"role": "user", "parts": [{"text": "history"}, {"text": "q2"}]}],
    }
    comparison = prefix_comparison(first, second)
    assert comparison["equal_input_parts"] == 1
    assert comparison["input_first_difference"] == "$.input_parts[1][1].text"
    assert comparison["fixed_first_difference"] is None
    altered = {**second, "generationConfig": {"maxOutputTokens": 4096}}
    assert prefix_comparison(first, altered)["fixed_first_difference"] == (
        "$.fixed.generationConfig.maxOutputTokens"
    )


@pytest.mark.asyncio
async def test_each_physical_attempt_including_retry_is_recorded_and_secrets_never_emitted(
    tmp_path,
):
    bodies = []

    def transport(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": {"message": "private upstream body"}})
        usage = {"promptTokenCount": 100, "candidatesTokenCount": 2, "totalTokenCount": 102}
        if len(bodies) == 3:
            usage["cachedContentTokenCount"] = 0
        elif len(bodies) == 4:
            usage["cachedContentTokenCount"] = 80
        return httpx.Response(200, json=reply(usage=usage))

    output = tmp_path / "report.json"
    report = await run_experiment(
        profile=profile(retries=1),
        api_key="secret-do-not-print",
        output=output,
        samples=2,
        prefix_characters=2048,
        transport=httpx.MockTransport(transport),
        prefix_control=False,
    )
    assert len(report["logical_calls"]) == 3
    assert len(report["physical_calls"]) == 4
    assert report["unknown_usage_requests"] == 1
    assert report["totals"]["cache_unknown_or_invalid"] == 2
    assert report["totals"]["explicit_zero"] == 1
    assert report["totals"]["weighted_cached_input_ratio"] == 80 / 200
    assert report["accounting"]["physical_match"] is True
    assert report["accounting"]["unknown_usage_match"] is True
    assert report["accounting"]["logical_match"] is True
    assert report["accounting"]["recorded_logical_invocations"] == 3
    assert len(bodies[0]["tools"][0]["functionDeclarations"]) >= 60
    assert all(body["tools"] == bodies[0]["tools"] for body in bodies)
    assert all(body["systemInstruction"] == bodies[0]["systemInstruction"] for body in bodies)
    assert report["physical_calls"][1]["comparison"]["fixed_first_difference"] is None
    serialized = output.read_text(encoding="utf-8")
    assert "secret-do-not-print" not in serialized
    assert "private upstream body" not in serialized
    assert "研究群合成历史" not in serialized


@pytest.mark.asyncio
async def test_control_keeps_settings_but_changes_first_history_part_and_counts_every_call(
    tmp_path,
):
    def transport(request):
        return httpx.Response(
            200, json=reply(usage={"promptTokenCount": 100, "cachedContentTokenCount": 50})
        )

    report = await run_experiment(
        profile=profile(),
        api_key="synthetic",
        output=tmp_path / "report.json",
        samples=1,
        transport=httpx.MockTransport(transport),
    )
    assert len(report["physical_calls"]) == 3
    control = report["physical_calls"][-1]
    assert control["stage"] == "prefix_control"
    assert control["comparison"]["equal_input_parts"] == 0
    assert control["comparison"]["fixed_first_difference"] is None
    entry = report["scenarios"][0]
    assert entry["hot"]["requests"] == 1
    assert entry["prefix_control"]["requests"] == 1
    assert report["accounting"]["physical_match"] is True


def test_private_route_pipe_preserves_profile_without_disclosing_key():
    import io

    from tools.cache_probe import endpoint_origin, route_from_stdin

    parsed, key = route_from_stdin(
        io.StringIO(
            json.dumps({"profile": profile().model_dump(mode="json"), "api_key": "private"})
        )
    )
    assert parsed == profile()
    assert key == "private"
    assert endpoint_origin("http://user:secret@127.0.0.1:18045/v1beta") == "http://127.0.0.1:18045"


@pytest.mark.asyncio
async def test_transport_failure_without_response_is_retained_as_unknown(tmp_path):
    def fail(request):
        raise httpx.ConnectError("private transport detail", request=request)

    report = await run_experiment(
        profile=profile(),
        api_key="synthetic",
        output=tmp_path / "report.json",
        samples=1,
        transport=httpx.MockTransport(fail),
    )
    assert len(report["physical_calls"]) == 1
    assert report["physical_calls"][0]["status_code"] is None
    assert report["unknown_usage_requests"] == 1
    assert report["logical_calls"][0]["success"] is False
    assert report["physical_calls"][0]["error_category"] == "LLMUnavailableError"


@pytest.mark.asyncio
async def test_physical_error_response_usage_is_preserved_without_candidates():
    observer = PhysicalRequests()
    request = httpx.Request("POST", "https://gemini.invalid", json={"contents": []})
    await observer.request(request)
    response = httpx.Response(
        400,
        request=request,
        json={
            "usageMetadata": {"promptTokenCount": 42, "totalTokenCount": 42},
            "error": {
                "message": "private error",
                "code": "private-upstream-identifier",
                "type": "invalid_request_error",
            },
        },
    )
    await observer.response(response)
    observer.finish_logical_call("LLMInvalidRequestError")
    assert observer.rows[0]["prompt_tokens"] == 42
    assert observer.rows[0]["cached_prompt_tokens"] is None
    assert observer.rows[0]["unknown_usage"] is False
    assert observer.rows[0]["provider_error_category"] == {
        "code": "other",
        "type": "invalid_request_error",
    }
    assert "private error" not in json.dumps(observer.rows)
    assert "private-upstream-identifier" not in json.dumps(observer.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["C02", "C04", "C07"])
async def test_real_signed_tool_pair_body_exit_and_image_protocol_lanes(tmp_path, scenario):
    import re

    bodies = []

    def transport(request):
        body = json.loads(request.content)
        bodies.append(body)
        parts = [{"text": "ok"}]
        if len(bodies) == 1 and scenario != "C07":
            tool_name = "get_recent_chat_history"
            arguments = {}
            if scenario == "C04":
                prompt = "\n".join(
                    part.get("text", "") for row in body["contents"] for part in row["parts"]
                )
                handle = re.search(r"handle=([a-z0-9]+)", prompt).group(1)
                tool_name, arguments = "read_tool_artifact", {"handle": handle, "limit": 100}
            parts = [
                {
                    "functionCall": {"name": tool_name, "args": arguments},
                    "thoughtSignature": "original-opaque-signature",
                }
            ]
        return httpx.Response(
            200,
            json=reply(
                parts,
                {
                    "promptTokenCount": 1000,
                    "cachedContentTokenCount": 800,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 1005,
                },
            ),
        )

    report = await run_experiment(
        profile=profile(),
        api_key="synthetic",
        output=tmp_path / "report.json",
        scenarios=(scenario,),
        samples=2,
        prefix_characters=2048,
        transport=httpx.MockTransport(transport),
    )
    assert report["scenarios"][0]["status"] == "complete"
    assert len(bodies) == 3
    assert all(body["tools"] == bodies[0]["tools"] for body in bodies)
    if scenario in {"C02", "C04"}:
        parts = [part for row in bodies[1]["contents"] for part in row["parts"]]
        assert any(part.get("thoughtSignature") == "original-opaque-signature" for part in parts)
        assert any("functionResponse" in part for part in parts)
    if scenario == "C04":
        assert report["scenarios"][0]["local_raw_read"] is True
        assert "original-opaque-signature" not in json.dumps(bodies[2])
        assert report["logical_calls"][1]["boundary_after"] == "explicit_body_exit"
    if scenario == "C07":
        assert all("inlineData" in json.dumps(body) for body in bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["C03", "C05", "C06"])
async def test_real_work_selection_disk_reopen_and_paid_compaction_driver(tmp_path, scenario):
    bodies = []

    def transport(request):
        body = json.loads(request.content)
        bodies.append(body)
        if not body.get("tools"):
            from tests.support.work_compaction import summary_json

            source = json.loads(body["contents"][-1]["parts"][-1]["text"])
            result = json.loads(summary_json(source))
            result["input_dispositions"] = [
                {
                    "input_ref": f"input:{item['input_id']}",
                    "kind": "context",
                    "reason": "synthetic context",
                }
                for item in source["task_inputs"]
            ]
            parts = [{"text": json.dumps(result)}]
        elif scenario == "C03" and len(bodies) == 1:
            parts = [
                {
                    "functionCall": {
                        "name": "task_control",
                        "args": {
                            "action": "update",
                            "context_note": {
                                "version": 1,
                                "unresolved": [{"text": "合成配色比较尚待核实", "refs": ["goal"]}],
                            },
                        },
                    },
                    "thoughtSignature": "original-note-signature",
                }
            ]
        elif scenario == "C05" and len(bodies) == 1:
            import re

            prompt = "\n".join(
                part.get("text", "") for row in body["contents"] for part in row["parts"]
            )
            handle = re.search(r"handle=([a-z0-9]+)", prompt).group(1)
            parts = [
                {
                    "functionCall": {
                        "name": "read_tool_artifact",
                        "args": {"handle": handle, "limit": 100},
                    },
                    "thoughtSignature": "original-read-signature",
                }
            ]
        else:
            parts = [{"text": "ok"}]
        return httpx.Response(
            200,
            json=reply(
                parts,
                {
                    "promptTokenCount": 1000,
                    "cachedContentTokenCount": 800,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 1005,
                },
            ),
        )

    report = await run_experiment(
        profile=profile(),
        api_key="synthetic",
        output=tmp_path / "report.json",
        scenarios=(scenario,),
        samples=2,
        prefix_characters=2048,
        transport=httpx.MockTransport(transport),
        compaction_mode="work",
    )
    entry = report["scenarios"][0]
    assert entry["status"] == "complete", entry
    evidence = entry["runtime_evidence"]
    assert len(evidence["request_work_ids"]) == 3
    if scenario == "C03":
        first, second = evidence["original_work_ids"]
        assert evidence["request_work_ids"] == [first, second, first]
        assert entry["context_note_result_ok"] is True
        assert len(evidence["appended_chat_event_ids"]) == 2
        assert len(evidence["selected_observation_ids"]) == 1
        assert "original-note-signature" not in json.dumps(bodies[1])
    elif scenario == "C05":
        assert evidence["restart"]["work_id_unchanged"] is True
        assert evidence["restart"]["model_requests_before"] == 1
        assert evidence["restart"]["model_requests_after"] == 1
        # #262: local artifact readback dispatches once but charges no business
        # budget. Disk reopen must preserve that zero, not silently charge it.
        assert evidence["restart"]["tool_calls_before"] == 0
        assert evidence["restart"]["tool_calls_after"] == 0
        assert evidence["accepted_read"]["same_receipt"] is True
        assert evidence["accepted_read"]["business_invocations"] == 1
        assert "original-read-signature" not in json.dumps(bodies[1])
    else:
        assert evidence["compaction"]["explicit_boundary"] is True
        assert any(row["stage"] == "auxiliary_compaction" for row in report["physical_calls"])
    assert report["accounting"]["physical_match"] is True
    assert report["accounting"]["unknown_usage_match"] is True
    assert report["accounting"]["logical_match"] is True


@pytest.mark.asyncio
async def test_c06_default_uses_real_ordinary_paid_summary_without_accepting_work(tmp_path):
    bodies = []

    def transport(request):
        body = json.loads(request.content)
        bodies.append(body)
        if not body.get("tools"):
            source = json.loads(body["contents"][-1]["parts"][-1]["text"])
            content = json.dumps(
                {
                    "facts": [{"text": "合成已核资料", "refs": source["source_refs"]}],
                    "pending": [],
                    "next_steps": [],
                }
            )
            parts = [{"text": content}]
        elif len(bodies) == 1:
            parts = [
                {
                    "functionCall": {"name": "get_recent_chat_history", "args": {}},
                    "thoughtSignature": "original-ordinary-signature",
                }
            ]
        else:
            parts = [{"text": "ok"}]
        return httpx.Response(
            200,
            json=reply(
                parts,
                {
                    "promptTokenCount": 1000,
                    "cachedContentTokenCount": 800,
                    "totalTokenCount": 1005,
                    "candidatesTokenCount": 5,
                },
            ),
        )

    report = await run_experiment(
        profile=profile(),
        api_key="synthetic",
        output=tmp_path / "report.json",
        scenarios=("C06",),
        samples=2,
        prefix_characters=2048,
        transport=httpx.MockTransport(transport),
    )
    entry = report["scenarios"][0]
    assert entry["status"] == "complete", entry
    assert entry["runtime_evidence"]["created_works"] == 0
    assert entry["runtime_evidence"]["compaction"]["explicit_boundary"] is True
    assert len(report["physical_calls"]) == 4
    assert report["accounting"]["logical_match"] is True
    assert report["accounting"]["physical_match"] is True
    assert "original-ordinary-signature" not in json.dumps(bodies[-1])
    auxiliary = next(
        item for item in report["logical_calls"] if item["stage"] == "auxiliary_compaction"
    )
    assert auxiliary["summary_shape"]["schema_contract"] == "ordinary"
    assert auxiliary["summary_shape"]["schema_valid"] is True
    continued = report["physical_calls"][2]
    assert continued["comparison_previous_stage"] == "auxiliary_compaction"
    assert continued["comparison"]["fixed_first_difference"] is not None
    assert continued["same_lane_previous_index"] == 1
    assert continued["same_lane_comparison"]["fixed_first_difference"] is None
    assert continued["same_lane_comparison"]["equal_input_parts"] >= 1
    assert report["parameters"]["measured_samples"] == 2
    assert report["parameters"]["warmups"] == 1


@pytest.mark.asyncio
async def test_deepseek_chat_wire_keeps_tools_prefix_and_zero_missing_cache_separate(tmp_path):
    bodies = []

    def transport(request):
        body = json.loads(request.content)
        bodies.append(body)
        usage = {"prompt_tokens": 1000, "completion_tokens": 5, "total_tokens": 1005}
        if len(bodies) == 2:
            usage["prompt_cache_hit_tokens"] = 0
        elif len(bodies) == 3:
            usage["prompt_cache_hit_tokens"] = 800
        return httpx.Response(
            200,
            json={
                "id": "synthetic-reply",
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                ],
                "usage": usage,
            },
        )

    selected = profile().model_copy(
        update={
            "provider": "deepseek",
            "protocol": ModelProtocol.CHAT_COMPLETIONS,
            "model": "deepseek-flash",
            "structured_output_mode": StructuredOutputMode.FUNCTION_TOOL,
        }
    )
    report = await run_experiment(
        profile=selected,
        api_key="synthetic",
        output=tmp_path / "report.json",
        samples=2,
        prefix_control=False,
        transport=httpx.MockTransport(transport),
    )
    assert all(body["tools"] == bodies[0]["tools"] for body in bodies)
    assert all(body["messages"][:2] == bodies[0]["messages"][:2] for body in bodies)
    assert report["totals"]["cache_unknown_or_invalid"] == 1
    assert report["totals"]["explicit_zero"] == 1
    assert report["totals"]["weighted_cached_input_ratio"] == 800 / 2000
    assert report["physical_calls"][-1]["same_lane_comparison"]["fixed_first_difference"] is None
    assert report["accounting"]["logical_match"] is True
    assert report["accounting"]["physical_match"] is True


@pytest.mark.asyncio
async def test_deepseek_responses_cumulative_chat_keeps_actual_reply_and_one_chain(tmp_path):
    bodies = []

    def transport(request):
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        bodies.append(body)
        usage = {"input_tokens": 1000, "output_tokens": 5, "total_tokens": 1005}
        if len(bodies) == 2:
            usage["input_tokens_details"] = {"cached_tokens": 0}
        elif len(bodies) == 3:
            usage["input_tokens_details"] = {"cached_tokens": 800}
        return httpx.Response(
            200,
            json={
                "id": f"synthetic-response-{len(bodies)}",
                "status": "completed",
                "output": [
                    {
                        "id": f"message-{len(bodies)}",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"actual reply {len(bodies)}"}],
                    }
                ],
                "usage": usage,
            },
        )

    selected = profile().model_copy(
        update={
            "provider": "deepseek",
            "protocol": ModelProtocol.RESPONSES,
            "model": "deepseek-flash",
            "structured_output_mode": StructuredOutputMode.TEXT_JSON,
        }
    )
    report = await run_experiment(
        profile=selected,
        api_key="synthetic",
        output=tmp_path / "report.json",
        scenarios=("C08",),
        samples=2,
        prefix_control=False,
        transport=httpx.MockTransport(transport),
    )
    assert report["provider_class"] == "DeepSeekResponsesProvider"
    assert report["scenarios"][0]["status"] == "complete"
    assert len(bodies) == 3
    for before, after in pairwise(bodies):
        assert after["input"][: len(before["input"])] == before["input"]
        assert after["tools"] == before["tools"]
    assert "actual reply 1" in json.dumps(bodies[-1])
    assert "actual reply 2" in json.dumps(bodies[-1])
    assert len({row["request_chain_id"] for row in report["logical_calls"]}) == 1
    assert report["totals"]["cache_unknown_or_invalid"] == 1
    assert report["totals"]["explicit_zero"] == 1
    assert report["totals"]["weighted_cached_input_ratio"] == 800 / 2000
    assert all(
        row["same_lane_comparison"]["fixed_first_difference"] is None
        for row in report["physical_calls"][1:]
    )
    assert report["accounting"]["physical_match"] is True
