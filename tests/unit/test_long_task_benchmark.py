"""Unpaid budget/oracle checks and real journal/worker benchmark assembly."""

import json

import httpx
import pytest
from scripts import benchmark_long_tasks as bench
from tests.integration.test_codemode_provider_wire import answer, wire_work_receipts
from tests.integration.test_codemode_runner import call
from tests.support.codemode_cases import requires_worker

from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction


def test_budget_settles_usage_and_retains_unanswered_reservations():
    ledger = bench.BudgetLedger(prior_usd=0.7, ceiling_usd=1)
    first, reserve = ledger.admit(100000, 4096)
    second, unknown = ledger.admit(100000, 4096)
    cost, tokens = ledger.settle(
        first,
        {
            "prompt_tokens": 20000,
            "prompt_cache_hit_tokens": 19000,
            "completion_tokens": 100,
        },
    )
    assert cost < reserve
    assert tokens == {"input": 20000, "cached": 19000, "output": 100}
    assert ledger.summary()["cumulative_peak_exposure_usd"] == 0.7 + cost + unknown
    assert ledger.pending == {second: unknown}
    ledger.ceiling_usd = ledger.summary()["cumulative_peak_exposure_usd"]
    with pytest.raises(RuntimeError, match="cumulative paid ceiling"):
        ledger.admit(1000, 4096)
    assert ledger.physical_calls == 2


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 100, "prompt_cache_hit_tokens": 20, "completion_tokens": 10},
        {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 20}, "output_tokens": 10},
        {"input_tokens": 80, "cache_read_input_tokens": 20, "output_tokens": 10},
    ],
)
def test_cost_counts_protocol_cache_consistently(usage):
    assert bench.usage_cost(usage)[1] == {"input": 100, "cached": 20, "output": 10}


def test_invalid_usage_cannot_refund_reservation():
    ledger = bench.BudgetLedger()
    identity, reserve = ledger.admit(100, 1)
    with pytest.raises(ValueError):
        ledger.settle(
            identity, {"prompt_tokens": 1, "prompt_cache_hit_tokens": 2, "completion_tokens": 1}
        )
    assert ledger.pending[identity] == reserve
    with pytest.raises(RuntimeError, match="exceeded conservative"):
        ledger.settle(identity, {"prompt_tokens": 10000, "completion_tokens": 1000})
    assert ledger.pending[identity] > reserve


def test_observer_preserves_invalid_arguments_for_host_validation():
    recorded = bench.observe_call(ToolCall("bad", ToolFunction("execute_code", "{")))
    assert recorded["arguments"] is None
    assert recorded["arguments_raw"] == "{"
    assert recorded["arguments_valid_json"] is False
    assert bench.observe_call(ToolCall("valid", ToolFunction("lookup", '{"q":1}')))[
        "arguments"
    ] == {"q": 1}


def test_new_or_static_code_parents_are_not_resumed_program_progress():
    previous = {}
    assert not bench.advancing_code_boundaries(
        [{"operation_id": "original", "snapshot_revision": 16}], previous
    )
    assert not bench.advancing_code_boundaries(
        [
            {"operation_id": "original", "snapshot_revision": 16},
            {"operation_id": "new-orphan", "snapshot_revision": 20},
        ],
        previous,
    )
    assert bench.advancing_code_boundaries(
        [{"operation_id": "original", "snapshot_revision": 17}], previous
    ) == {("code_boundary", "original:17")}
    assert not bench.advancing_code_boundaries(
        [{"operation_id": "original", "snapshot_revision": 17}], previous
    )


def test_authorized_unlimited_mode_records_cost_without_financial_or_call_quota():
    ledger = bench.BudgetLedger(prior_usd=5, ceiling_usd=None, maximum_calls=None)
    for _ in range(401):
        identity, _ = ledger.admit(300000, 4096)
        ledger.settle(identity, {"prompt_tokens": 100, "completion_tokens": 10})
    assert ledger.physical_calls == 401
    assert ledger.summary()["cumulative_peak_exposure_usd"] > 5
    assert not ledger.pending


def test_prior_wire_usage_reconstructs_budget_without_resetting(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "wire": [
                            {"usage": {"prompt_tokens": 100, "completion_tokens": 10}},
                            {"bytes": 1000, "output_limit": 4096},
                        ]
                    }
                ]
            }
        )
    )
    ledger = bench.prior_ledger([path])
    assert ledger.prior_usd == (100 * 0.30 + 10 * 1.2 + 1000 * 0.30 + 4096 * 1.2) / 1e6
    assert ledger.prior_reports[0]["unknown_usage"] == 1
    with pytest.raises(ValueError, match="duplicate prior"):
        bench.prior_ledger([path, path])


@pytest.mark.parametrize("task", bench.TASKS)
def test_repeat_is_same_within_groups_and_different_across_repeats(task):
    first, second = bench.make_task(task, 0), bench.make_task(task, 1)
    assert first.digest() == bench.make_task(task, 0).digest()
    assert first.digest() != second.digest()
    assert first.expected != second.expected
    if task == "batch_ledger":
        assert len(first.files) == 25
        # Independently parse fixture text and verify the oracle ignores void/duplicates.
        rows = {}
        for path in first.files["manifest.txt"].splitlines():
            for line in first.files[path].splitlines()[1:]:
                identity, region, amount, status = line.split(",")
                rows[identity] = (region, int(amount), status)
        counts = {region: 0 for region in "ABCD"}
        for region, _, status in rows.values():
            counts[region] += status == "ok"
        assert sum(counts.values()) == sum(
            int(x.split("\t")[1]) for x in first.expected["batch.tsv"].splitlines()
        )


@requires_worker
async def test_resumed_benchmark_keeps_trusted_note_in_actual_model_material(database, tmp_path):
    from tests.integration.test_codemode_runner import ACCEPT, runner_env

    from qq_ai_bot.runtime.work_context_note import visible_context_note
    from qq_ai_bot.runtime.work_session import WorkSession
    from qq_ai_bot.services.turn_transcript import TurnTranscript

    _, _, control, _, repo = await runner_env(database, tmp_path, iter(()))
    assert json.loads(await control.execute("task_control", ACCEPT, "accept"))["ok"]
    note = {
        "version": 1,
        "facts": [{"text": "Completed the first four nodes", "refs": ["goal"]}],
        "unresolved": [],
        "next_steps": [{"text": "Continue from the fifth node", "refs": ["goal"]}],
    }
    assert json.loads(
        await control.execute("task_control", {"action": "update", "context_note": note}, "note")
    )["ok"]
    original = await repo.get(control.current["id"])
    for _ in range(2):
        control = await bench.resumed_control(control)
        assert await visible_context_note(control) == note
        restored = await WorkSession(control, "benchmark-test").restore(TurnTranscript(()))
        material = [
            json.loads(message.content)
            for message in restored.request().messages
            if message.content and '"kind": "work_current_material"' in message.content
        ]
        assert material[-1]["context_note"] == note
        assert control.current["id"] == original["id"]
        assert control.source["actor_person_id"] == control.context_access.actor_person_id
        assert (await repo.get(original["id"]))["tool_calls"] == original["tool_calls"]
    control.context_access = None
    with pytest.raises(ValueError, match="benchmark_read_identity_missing"):
        await bench.resumed_control(control)


@requires_worker
@pytest.mark.parametrize(
    "loop,mode,task,repeated_reads,invalid_json,verify_report",
    [
        ("new", "code", "batch_ledger", 0, False, True),
        ("old", "code", "dependency_chain", 0, False, True),
        ("new", "code", "resumed_work", 0, False, True),
        ("old", "code", "resumed_work", 0, False, True),
        ("old", "direct", "resumed_work", 0, False, True),
        ("new", "code", "resumed_work", 70, False, True),
        ("new", "code", "batch_ledger", 0, True, True),
        # Correct files and completed Work cannot substitute for the required
        # successful report readback, on either assembly or tool mode.
        ("new", "direct", "resumed_work", 0, False, False),
        ("new", "code", "resumed_work", 0, False, False),
        ("old", "direct", "resumed_work", 0, False, False),
        ("old", "code", "resumed_work", 0, False, False),
    ],
)
async def test_unpaid_long_task_assembly(
    database, tmp_path, monkeypatch, loop, mode, task, repeated_reads, invalid_json, verify_report
):
    monkeypatch.setattr(
        bench,
        "CREDENTIALS",
        {
            "api_key": "synthetic",
            "model": "deepseek-flash",
            "base_url (openai)": "https://benchmark.invalid",
        },
    )
    monkeypatch.setattr(bench, "RECORDS", [])
    monkeypatch.setattr(bench, "LEDGER", bench.BudgetLedger())
    monkeypatch.setattr(bench, "OUTPUT", None)
    fixture = bench.make_task(task, 0)
    if mode == "code":
        # This scripted plan only verifies the unpaid harness. The paid benchmark
        # never supplies code or oracle answers to the model.
        code = (
            "r = await yuki_workspace_read({'path':'start.txt'})\np = r['data']['text'].strip()\n"
        )
        code += "values = []\npaths = []\nwhile p != 'END':\n"
        code += "    r = await yuki_workspace_read({'path':p})\n"
        code += "    d = dict(line.split('=') for line in r['data']['text'].splitlines())\n"
        code += "    v = int(d['value'])\n    paths.append(p)\n    values.append(v)\n"
        if task == "resumed_work":
            code += (
                "    await yuki_workspace_write({'path':'audit/' + "
                "str(len(values)-1).zfill(2) + '.txt', 'text':str(v)+'\\n'})\n"
            )
        code += "    p = d['left'] if v % 2 == 0 else d['right']\n"
        code += (
            "out = '\\n'.join(paths[i]+'\\t'+str(values[i]) for i in range(len(paths)))"
            "+'\\nTOTAL\\t'+str(sum(values))+'\\n'\n"
        )
        code += "await yuki_workspace_write({'path':'chain.tsv','text':out})\n"
        if verify_report:
            code += "await yuki_workspace_read({'path':'chain.tsv'})\n"
        code += "'OK'"
        if task == "batch_ledger":
            code = "r = await yuki_workspace_read({'path':'manifest.txt'})\n"
            code += "rows = {}\nfor p in r['data']['text'].splitlines():\n"
            code += "    r = await yuki_workspace_read({'path':p})\n"
            code += "    for line in r['data']['text'].splitlines()[1:]:\n"
            code += "        f = line.split(',')\n        rows[f[0]] = f\n"
            code += "totals = {k:[0,0] for k in 'ABCD'}\nfor f in rows.values():\n"
            code += (
                "    if f[3] == 'ok':\n        totals[f[1]][0] += 1\n"
                "        totals[f[1]][1] += int(f[2])\n"
            )
            code += (
                "out = '\\n'.join(k+'\\t'+str(totals[k][0])+'\\t'+str(totals[k][1]) "
                "for k in 'ABCD')+'\\n'\n"
            )
            code += "await yuki_workspace_write({'path':'batch.tsv','text':out})\n"
            code += "await yuki_workspace_read({'path':'batch.tsv'})\n'OK'"
        if repeated_reads:
            # Repeated paths can still advance one bounded, durable program.
            # A path-only observer must not kill it before its later writes.
            code = (
                f"for _ in range({repeated_reads}):\n"
                "    await yuki_workspace_read({'path':'start.txt'})\n" + code
            )
        steps = iter(
            [
                call("execute_code", {"code": code}, "code"),
                call("task_control", {"action": "complete"}, "complete"),
                ChatResponse("TASK_DONE", 0),
            ]
        )
    else:
        responses = []
        for index, path in enumerate(fixture.required_reads):
            responses.append(call("workspace_read", {"path": path}, f"read-{index}"))
        for path, text in fixture.expected.items():
            responses.append(call("workspace_write", {"path": path, "text": text}, f"write-{path}"))
        if verify_report:
            responses.append(call("workspace_read", {"path": "chain.tsv"}, "verify"))
        responses.append(call("task_control", {"action": "complete"}, "complete"))
        responses.append(ChatResponse("TASK_DONE", 0))
        steps = iter(responses)

    if invalid_json:
        # Malformed model arguments are ordinary Host refusals. Instrumentation
        # must retain them and let the real loop present the refusal for repair.
        steps = iter(
            [
                ChatResponse(
                    "", 0, tool_calls=(ToolCall("invalid", ToolFunction("execute_code", "{")),)
                ),
                *steps,
            ]
        )

    def transport(request):
        messages = json.loads(request.content)["messages"]
        if loop == "new" and any(
            m["role"] == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("{")
            and json.loads(m["content"]).get("kind") == "work_segment_handoff"
            for m in messages
        ):
            # A scripted model must respect the zero-business closing request.
            # Do not consume a planned write that the real Host will refuse.
            return httpx.Response(
                200,
                json=answer(
                    call(
                        "task_control",
                        {
                            "action": "update",
                            "context_note": {
                                "version": 1,
                                "facts": [],
                                "unresolved": [],
                                "next_steps": [
                                    {
                                        "text": "Continue the remaining scripted plan",
                                        "refs": ["goal"],
                                    }
                                ],
                            },
                        },
                        "handoff",
                    ),
                    "chat_completions",
                    1,
                ),
            )
        response = next(steps)
        if mode == "code" and response.tool_calls[0].function.name == "task_control":
            # An oracle completion cannot hide an orphan VM or a lost receipt.
            receipts = {m["tool_call_id"]: m["content"] for m in messages if m.get("tool_call_id")}
            receipts.update({r["call_id"]: r["result"] for r in wire_work_receipts(messages)})
            assert json.loads(receipts["code"])["result"] == "OK"
        return httpx.Response(200, json=answer(response, "chat_completions", 1))

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    await bench.compare_case(database, tmp_path, loop, mode, task, 0)
    result = bench.RECORDS[-1]
    assert result["success"] is verify_report, result
    assert result["final_report_verified"] is verify_report
    assert result["correct_artifacts"]
    assert result["work_completed"]
    assert result["completion_fraction"] == 1
    assert result["duplicate_operation_ids"] == 0
    assert result["duplicate_writes"] == 0
    assert result["request_shapes_fixed"]
    if invalid_json:
        assert result["model_turns"][0]["tool_calls"][0]["arguments_valid_json"] is False
        assert "invalid" in result["wire"][1]["receipt_error_call_ids"]
        assert not result["failure_details"]
    if task == "resumed_work":
        assert len(result["segments"]) > 1
        for previous, following in zip(result["segments"], result["segments"][1:], strict=False):
            assert previous["models_after"] == following["models_before"]
            assert previous["tools_after"] == following["tools_before"]
        if mode == "code":
            # Code result and explicit completion; quiet completion needs no
            # extra purchased final response.
            assert result["physical_http"] == 2
            assert result["business_calls"] == 26 + int(verify_report) + repeated_reads
