"""Unpaid budget/oracle checks and real journal/worker benchmark assembly."""

import json

import httpx
import pytest
from scripts import benchmark_long_tasks as bench
from tests.integration.test_codemode_provider_wire import answer, wire_work_receipts
from tests.integration.test_codemode_runner import call
from tests.support.codemode_cases import requires_worker
from tests.support.parent_receipts import observation_bodies
from tests.support.work_compaction import summary_json

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


def test_auxiliary_output_uses_its_existing_bound_and_the_same_ledger():
    ledger = bench.BudgetLedger()
    identity, reserve = ledger.admit(100, 16384, maximum_output=16384)
    assert ledger.pending[identity] == reserve
    with pytest.raises(RuntimeError, match="size/output limit"):
        ledger.admit(100, 16385, maximum_output=16384)
    assert ledger.physical_calls == 1


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


@pytest.mark.parametrize("violation", ["none", "delayed", "before_read", "missing", "repeated"])
def test_audit_oracle_requires_per_node_order_even_when_final_files_are_correct(violation):
    task = bench.make_task("resumed_work", 0)
    operations = []
    for index, path in enumerate(task.required_reads[1:]):
        operations.extend(
            [
                {"ok": True, "name": "workspace_read", "arguments": {"path": path}},
                {
                    "ok": True,
                    "name": "workspace_write",
                    "arguments": {
                        "path": f"/workspace/audit/{index:02d}.txt",
                        "text": task.expected[f"audit/{index:02d}.txt"],
                    },
                },
            ]
        )
    if violation == "delayed":
        operations = operations[::2] + operations[1::2]
    elif violation == "before_read":
        operations[0], operations[1] = operations[1], operations[0]
    elif violation == "missing":
        operations.pop(1)
    elif violation == "repeated":
        operations.insert(2, operations[1])
    operations.append({"ok": True, "name": "workspace_write", "arguments": {"path": "chain.tsv"}})
    assert bench.audit_order_verified(task, operations) is (violation == "none")


@pytest.mark.parametrize(
    "default_policy,completed,collection_exit,expected_exit",
    [(True, True, 0, 0), (True, False, 0, 1), (True, None, 0, 1), (True, True, 2, 2)],
)
def test_default_policy_cli_cannot_hide_failed_or_missing_model_acceptance(
    tmp_path, monkeypatch, default_policy, completed, collection_exit, expected_exit
):
    import sys

    # No provider or worker executes here: collect a synthetic acceptance record
    # after replacing both credential loading and pytest collection.
    worker = tmp_path / "unused-worker"
    worker.write_text("synthetic")
    prior = tmp_path / "prior.json"
    prior.write_text('{"records": []}')
    output = tmp_path / "output.json"
    args = [
        "benchmark",
        "--credentials",
        str(tmp_path / "unused-credentials.md"),
        "--output",
        str(output),
        "--authorize-paid",
        "--prior-report",
        str(prior),
        "--default-code-policy",
    ]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setenv("YUKI_MONTY_BINARY", str(worker))
    monkeypatch.setattr(
        bench, "read_credentials", lambda _path: {"model": "deepseek-flash", "api_key": "fake"}
    )
    monkeypatch.setattr(bench.pytest, "main", lambda _args: collection_exit)
    monkeypatch.setattr(bench.logging, "disable", lambda _level: None)
    monkeypatch.setattr(bench, "CREDENTIALS", {})
    monkeypatch.setattr(bench, "RECORDS", [] if completed is None else [{"success": completed}])
    monkeypatch.setattr(bench, "IN_PROGRESS", None)
    for field in (
        "LEDGER",
        "OUTPUT",
        "STARTED",
        "MAX_OUTPUT",
        "SEGMENT_TOOLS",
        "REASONING_EFFORT",
        "DEFAULT_CODE_POLICY",
    ):
        monkeypatch.setattr(bench, field, getattr(bench, field))
    assert bench.main() == expected_exit
    assert json.loads(output.read_text())["default_code_policy"] is default_policy
    assert bench.CREDENTIALS == {}


@requires_worker
async def test_resumed_benchmark_keeps_trusted_note_in_actual_model_material(database, tmp_path):
    from tests.integration.test_codemode_runner import ACCEPT, runner_env
    from tests.support.work_session import WorkSession

    from qq_ai_bot.runtime.work_context_note import visible_context_note
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
            body
            for message in restored.request().messages
            for body in observation_bodies(message.content)
            if body.get("kind") == "work_current_material"
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
    "loop,mode,task,repeated_reads,invalid_json,verify_report,large_reasoning",
    [
        ("new", "code", "batch_ledger", 0, False, True, False),
        ("old", "code", "dependency_chain", 0, False, True, False),
        ("new", "code", "resumed_work", 0, False, True, False),
        ("new", "code", "resumed_work", 0, False, True, True),
        ("old", "code", "resumed_work", 0, False, True, False),
        ("old", "direct", "resumed_work", 0, False, True, False),
        ("new", "code", "resumed_work", 70, False, True, False),
        ("new", "code", "batch_ledger", 0, True, True, False),
        # Correct files and completed Work cannot substitute for the required
        # successful report readback, on either assembly or tool mode.
        ("new", "direct", "resumed_work", 0, False, False, False),
        ("new", "code", "resumed_work", 0, False, False, False),
        ("old", "direct", "resumed_work", 0, False, False, False),
        ("old", "code", "resumed_work", 0, False, False, False),
    ],
)
async def test_unpaid_long_task_assembly(
    database,
    tmp_path,
    monkeypatch,
    loop,
    mode,
    task,
    repeated_reads,
    invalid_json,
    verify_report,
    large_reasoning,
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
            if task == "resumed_work" and index:
                # Match the existing task's per-node audit order. The old
                # fixture delayed all audits until after the chain was read.
                audit_path = f"audit/{index - 1:02d}.txt"
                responses.append(
                    call(
                        "workspace_write",
                        {"path": audit_path, "text": fixture.expected[audit_path]},
                        f"write-{audit_path}",
                    )
                )
        for path, text in fixture.expected.items():
            if task == "resumed_work" and path.startswith("audit/"):
                continue
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

    compaction_sources = []
    primary_payloads = []

    def transport(request):
        payload = json.loads(request.content)
        messages = payload["messages"]
        if not payload.get("tools"):
            # An actual structured, separately paid compaction request must
            # survive the same profile/route admission as production.
            source = messages[-1]["content"]
            compaction_sources.append(source)
            assert "synthetic private reasoning" not in source
            return httpx.Response(
                200, json=answer(ChatResponse(summary_json(source), 1), "chat_completions", 1)
            )
        primary_payloads.append(payload)
        if loop == "new" and any(
            m["role"] == "user"
            and any(
                body.get("kind") == "work_segment_handoff"
                for body in observation_bodies(m.get("content"))
            )
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
            if large_reasoning:
                # At this explicit compaction boundary the paired raw round is
                # retired. Require the original result in its authorized public
                # summary source and check its durable owner below instead.
                records = [
                    record
                    for source in compaction_sources
                    for record in json.loads(source)["records"]
                ]
                receipts = [
                    record["content"] for record in records if record.get("tool_call_id") == "code"
                ] + [record["output"] for record in records if record.get("call_id") == "code"]
                receipts.extend(
                    row["result"] for row in wire_work_receipts(records) if row["call_id"] == "code"
                )
                # F9 may legally retire the signed protocol by business rebase
                # before capacity compaction. The original result must appear in
                # either the actual summary source or current portable evidence.
                receipts.extend(
                    row["result"]
                    for row in wire_work_receipts(messages)
                    if row["call_id"] == "code"
                )
                assert any(json.loads(receipt)["result"] == "OK" for receipt in receipts), [
                    list(record) for record in records
                ]
            else:
                receipts = {
                    m["tool_call_id"]: m["content"] for m in messages if m.get("tool_call_id")
                }
                receipts.update({r["call_id"]: r["result"] for r in wire_work_receipts(messages)})
                assert json.loads(receipts["code"])["result"] == "OK"
        body = answer(response, "chat_completions", 1)
        # This transport is unpaid. Return explicit synthetic usage so the
        # real ledger settles each mock reservation; previously omitted usage
        # accumulated as unknown exposure near the paid ceiling after 31 calls.
        # Unknown-response reservation protection has separate budget tests.
        body["usage"] = {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        if large_reasoning and response.tool_calls[0].function.name == "execute_code":
            body["choices"][0]["message"]["reasoning_content"] = (
                "synthetic private reasoning " * 6000
            )
        return httpx.Response(200, json=body)

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    await bench.compare_case(database, tmp_path, loop, mode, task, 0)
    result = bench.RECORDS[-1]
    assert result["success"] is verify_report, result
    assert result["goal_completed"] is verify_report
    assert result["final_report_verified"] is verify_report
    assert result["correct_artifacts"]
    assert result["work_completed"]
    assert result["completion_fraction"] == 1
    assert result["duplicate_operation_ids"] == 0
    assert result["duplicate_writes"] == 0
    assert result["request_shapes_fixed"]
    assert result["audit_order_verified"]
    for wire in result["wire"]:
        assert 0 < wire["message_bytes"] < wire["request_bytes"]
        assert wire["tool_receipt_characters"] >= 0
    # Receipt accounting includes both protocol rows and F9 portable evidence.
    # A compacted round is instead proven in its actual summary source above.
    assert any(wire["tool_receipt_characters"] > 0 for wire in result["wire"]) or (
        large_reasoning and compaction_sources
    )
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
            assert result["physical_http"] == 2 + len(compaction_sources)
            assert result["business_calls"] == 26 + int(verify_report) + repeated_reads
    if large_reasoning:
        from sqlalchemy import select

        from qq_ai_bot.runtime.work_schema_v1 import effects

        if not compaction_sources:
            # F9 business rebase can retire large old opaque after parent
            # settlement, avoiding a now-unnecessary compaction purchase.
            assert task == "resumed_work" and mode == "code" and loop == "new"
            assert len(primary_payloads) == 2
            assert "synthetic private reasoning" not in json.dumps(primary_payloads[-1])
            portable = wire_work_receipts(primary_payloads[-1]["messages"])
            assert sum(row["call_id"] == "code" for row in portable) == 1
        assert result["logical_models"] == result["physical_http"]
        assert len([w for w in result["wire"] if w["purpose"] == "main"]) == 2
        assert all(
            w["tools_count"] == 0 for w in result["wire"] if w["purpose"] == "work_compaction"
        )
        async with database.sessions() as reader:
            parents = (
                (
                    await reader.execute(
                        select(effects).where(
                            effects.c.work_id == result["work_id"],
                            effects.c.kind == "code_composition",
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert len(parents) == 1 and parents[0]["state"] == "accepted"
        original = json.loads(parents[0]["receipt_json"])["result"]
        assert json.loads(original)["result"] == "OK"
