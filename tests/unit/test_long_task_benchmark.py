"""Unpaid budget/oracle checks and real journal/worker benchmark assembly."""

import json

import httpx
import pytest
from scripts import benchmark_long_tasks as bench
from tests.integration.test_codemode_provider_wire import answer
from tests.integration.test_codemode_runner import call
from tests.support.codemode_cases import requires_worker

from qq_ai_bot.domain.messages import ChatResponse


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
@pytest.mark.parametrize(
    "loop,mode,task",
    [
        ("new", "code", "batch_ledger"),
        ("old", "code", "dependency_chain"),
        ("new", "code", "resumed_work"),
        ("old", "direct", "resumed_work"),
    ],
)
async def test_unpaid_long_task_assembly(database, tmp_path, monkeypatch, loop, mode, task):
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
        code += "await yuki_workspace_read({'path':'chain.tsv'})\n'OK'"
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
        responses.append(call("workspace_read", {"path": "chain.tsv"}, "verify"))
        responses.append(call("task_control", {"action": "complete"}, "complete"))
        responses.append(ChatResponse("TASK_DONE", 0))
        steps = iter(responses)

    def transport(request):
        return httpx.Response(200, json=answer(next(steps), "chat_completions", 1))

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    await bench.compare_case(database, tmp_path, loop, mode, task, 0)
    result = bench.RECORDS[-1]
    assert result["success"], result
    assert result["work_completed"]
    assert result["completion_fraction"] == 1
    assert result["duplicate_operation_ids"] == 0
    assert result["duplicate_writes"] == 0
    assert result["request_shapes_fixed"]
    if task == "resumed_work":
        assert len(result["segments"]) > 1
        for previous, following in zip(result["segments"], result["segments"][1:], strict=False):
            assert previous["models_after"] == following["models_before"]
            assert previous["tools_after"] == following["tools_before"]
        if mode == "code":
            # Code result and explicit completion; quiet completion needs no
            # extra purchased final response.
            assert result["physical_http"] == 2
            assert result["business_calls"] == 27
