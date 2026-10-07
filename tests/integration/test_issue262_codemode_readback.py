"""#262 with the pinned VM, real SQLite receipts and independent workspace I/O."""

import hashlib
import json
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import insert
from tests.support.codemode_cases import (
    TOOLS,
    effect_rows,
    environment,
    outer_call,
    requires_worker,
)

from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.codemode.driver import CodeCompositionYield, CodeModeDriver
from qq_ai_bot.domain.messages import ChatTool, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.services.chat import _fit_artifact_page_result
from qq_ai_bot.services.invocation_service import InvocationService
from qq_ai_bot.tool_results.access import ArtifactAccess
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.workspace.files import FileWorkspace
from qq_ai_bot.workspace.store import WorkspaceError

pytestmark = requires_worker


async def readback_env(database, tmp_path, *, tool_limit=32):
    env = await environment(database, tmp_path, tool_limit=tool_limit)
    env.control.bind_context_access(
        ArtifactAccess(env.control.lease.conversation_id, env.control.lease.generation, "reader")
    )
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    files = FileWorkspace(tmp_path)
    log = []
    env.host.api = project(
        (
            *TOOLS,
            ChatTool("workspace_read", "read", {"type": "object"}),
            ChatTool("read_tool_artifact", "readback", {"type": "object"}),
        ),
        "manifest-test",
    )
    service = InvocationService()

    async def execute_business(invocation, side_effecting):
        async def invoke():
            name, args = (
                invocation.call.function.name,
                json.loads(invocation.call.function.arguments),
            )
            log.append((name, args.copy()))
            if name == "workspace_read":
                try:
                    data = files.read(**args)
                    outcome = ToolExecutionResult(
                        ok=True, data=data, provider_id="core", tool_name=name
                    )
                except WorkspaceError as exc:
                    outcome = ToolExecutionResult(
                        ok=False, error_code=str(exc), provider_id="core", tool_name=name
                    )
            elif name == "read_tool_artifact":
                page = await store.read(
                    args["handle"],
                    operation="get",
                    path=("text",),
                    offset=args.get("offset", 0),
                    limit=32000,
                    max_characters=1000,
                    access=env.control.context_access,
                )
                assert page is not None
                outcome = _fit_artifact_page_result(page, max_characters=1000)
            else:
                outcome = ToolExecutionResult(
                    ok=True, data=args, provider_id="core", tool_name=name
                )
            return (
                await ToolResultBudgeter(
                    max_characters=1000, artifacts=store, artifact_access=env.control.context_access
                ).render(replace(outcome, mutation_committed=False))
            ).text

        return await service.invoke(invocation, invoke, side_effecting=side_effecting)

    env.host.execute_business = execute_business

    async def archive(text):
        return await store.write_artifact(
            provider_id="codemode",
            tool_name="execute_code",
            content=text,
            media_type="application/json",
            access=env.control.context_access,
        )

    env.host.archive = archive
    return SimpleNamespace(env=env, store=store, files=files, log=log, execute=execute_business)


async def test_original_large_page_survives_interruption_and_live_file_change(database, tmp_path):
    case = await readback_env(database, tmp_path)
    text = "original🙂" * 5000
    tmp_path.joinpath("source.txt").write_text(text)
    expected = case.files.read("source.txt")
    code = (
        "r = await yuki_workspace_read({'path': 'source.txt'})\n"
        "[r['complete'], r['data']['text'], r['data']['size'], r['data']['version']]"
    )
    outer = outer_call(case.env, code)
    driver = CodeModeDriver(case.env.host, outer)

    async def interrupt_before_settle(child):
        raise CodeCompositionYield(child.operation_id)

    driver._vm_value = interrupt_before_settle
    token = current_work_control.set(case.env.control)
    try:
        with pytest.raises(CodeCompositionYield):
            await driver.run()
        tmp_path.joinpath("source.txt").write_text("changed on disk")
        # Native restoration happens before answering the pending future from T3.
        resumed = json.loads(await CodeModeDriver(case.env.host, outer).run())
        assert resumed["status"] == "completed", resumed
        # The parent is deliberately bounded; inspect its saved output reference.
        assert resumed["complete"] is False
        rows, tools, root = await effect_rows(database, case.env.control.current["id"])
        child = next(row for row in rows.values() if row["kind"] == "tool")
        receipt = json.loads(child["receipt_json"])
        page = await case.store.read(
            receipt["artifact_handle"],
            operation="get",
            max_characters=262144,
            access=case.env.control.context_access,
        )
        assert page["value"] == expected
        answer = await case.store.read(
            resumed["result_ref"],
            operation="get",
            max_characters=262144,
            access=case.env.control.context_access,
        )
        assert answer["value"] == [True, expected["text"], expected["size"], expected["version"]]
        assert len(case.log) == tools == root == 1
    finally:
        current_work_control.reset(token)


async def test_direct_and_vm_local_readback_do_not_charge_exhausted_root(database, tmp_path):
    case = await readback_env(database, tmp_path, tool_limit=1)
    env = case.env
    identity = env.control.current["id"]
    async with database.sessions() as writer, writer.begin():
        await writer.execute(insert(budgets).values(root_id=identity, tool_limit=1))
    handle = await case.store.write_artifact(
        provider_id="core",
        tool_name="workspace_read",
        media_type="application/json",
        content=json.dumps({"ok": True, "data": {"text": "x" * 9000}}),
        access=env.control.context_access,
    )
    direct = direct_invocations(
        (
            ToolCall(
                "direct-read", ToolFunction("read_tool_artifact", json.dumps({"handle": handle}))
            ),
        ),
        env.agent,
        manifest_revision="manifest-test",
    )[0]
    token = current_work_control.set(env.control)
    try:
        first = await case.execute(direct, False)
        again = await case.execute(direct, False)
        assert first == again
        body = json.loads(
            await CodeModeDriver(
                env.host,
                outer_call(
                    env,
                    "await yuki_lookup({'q': 1})\n"
                    f"a = await yuki_read_tool_artifact({{'handle': '{handle}'}})\n"
                    f"b = await yuki_read_tool_artifact({{'handle': '{handle}', "
                    "'offset': a['data']['next_offset']})\n"
                    "[a['executed'], b['executed'], a['complete'], len(a['data']['value'])]",
                ),
            ).run()
        )
        assert body["status"] == "completed", body
        assert body["result"][:3] == [True, True, True] and body["result"][3] > 0
        assert body["usage"]["business_admitted"] == 1
        rows, tools, root = await effect_rows(database, identity)
        assert tools == root == env.control.tools_started == 1
        local = [
            json.loads(r["receipt_json"])["invocation"]
            for r in rows.values()
            if r["kind"] == "tool"
            and json.loads(r["receipt_json"])["invocation"]["tool_id"] == "read_tool_artifact"
        ]
        assert len(local) == 3 and all(
            r["dispatch_started"] and not r["budget_admitted"] for r in local
        )
        assert Counter(name for name, _ in case.log) == {"lookup": 1, "read_tool_artifact": 3}
    finally:
        current_work_control.reset(token)


async def test_hundred_file_analysis_matches_independent_oracle_across_segments(database, tmp_path):
    case = await readback_env(database, tmp_path, tool_limit=17)
    expected = []
    for index in range(100):
        text = f"{index}\n" + "資料🙂" * (900 + index)
        tmp_path.joinpath(f"f{index}.txt").write_text(text)
        expected.append(
            [
                index,
                len(text),
                hashlib.sha256(text.encode()).hexdigest(),
                sum(position * ord(character) for position, character in enumerate(text, 1)),
            ]
        )
    code = (
        "out = []\n"
        "for i in range(100):\n"
        "    r = await yuki_workspace_read({'path': 'f' + str(i) + '.txt'})\n"
        "    assert r['complete'] and r['data']['eof']\n"
        "    text = r['data']['text']\n"
        "    checksum = 0\n"
        "    for position, character in enumerate(text):\n"
        "        checksum += (position + 1) * ord(character)\n"
        "    out.append([int(text.split('\\n')[0]), len(text), r['data']['version'], checksum])\n"
        "out"
    )
    # Check every VM character while keeping the parent model result bounded.
    case.env.host.result_limit = 16000
    outer = outer_call(case.env, code)
    token = current_work_control.set(case.env.control)
    segments = 0
    try:
        while True:
            try:
                body = json.loads(await CodeModeDriver(case.env.host, outer).run())
                break
            except CodeCompositionYield:
                segments += 1
                assert segments < 10
                case.env.control.tools_started = 0
        assert body["status"] == "completed", body
        assert (
            len(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
            <= case.env.host.result_limit
        )
        if body["complete"]:
            actual = body["result"]
        else:
            saved = await case.store.read(
                body["result_ref"],
                operation="get",
                max_characters=262144,
                access=case.env.control.context_access,
            )
            actual = saved["value"]
        assert actual == expected
        assert segments == 5
        _, tools, root = await effect_rows(database, case.env.control.current["id"])
        assert tools == root == 100
        assert len(case.log) == 100 and len({args["path"] for _, args in case.log}) == 100
    finally:
        current_work_control.reset(token)


@pytest.mark.parametrize("failure", ["corrupt", "missing", "wrong_reference", "denied"])
async def test_vm_readback_failure_preserves_receipt_without_rereading_file(
    database, tmp_path, failure
):
    from sqlalchemy import select

    from qq_ai_bot.persistence.models import ToolArtifactModel

    case = await readback_env(database, tmp_path)
    tmp_path.joinpath("source.txt").write_text("original" * 6000)
    execute = case.env.host.execute_business

    async def damage_after_accept(invocation, side_effecting):
        result = await execute(invocation, side_effecting)
        handle = json.loads(result)["artifact_handle"]
        async with database.sessions() as reader:
            relative = await reader.scalar(
                select(ToolArtifactModel.relative_path).where(ToolArtifactModel.handle_id == handle)
            )
        if failure == "corrupt":
            (tmp_path / "results" / relative).write_text("corrupt archive")
        elif failure == "missing":
            (tmp_path / "results" / relative).unlink()
        elif failure == "denied":
            case.env.control.context_access = replace(
                case.env.control.context_access, actor_person_id="other"
            )
        else:
            # A valid but unbound handle cannot replace the accepted child's ref.
            wrong = await case.store.write_artifact(
                provider_id="core",
                tool_name="workspace_read",
                media_type="application/json",
                content=json.dumps({"ok": True, "data": {"text": "wrong"}}),
                access=case.env.control.context_access,
            )
            body = json.loads(result)
            body["artifact_handle"] = wrong
            result = json.dumps(body)
        tmp_path.joinpath("source.txt").write_text("new live content must never be read")
        return result

    case.env.host.execute_business = damage_after_accept
    token = current_work_control.set(case.env.control)
    try:
        body = json.loads(
            await CodeModeDriver(
                case.env.host,
                outer_call(
                    case.env,
                    "r = await yuki_workspace_read({'path': 'source.txt'})\n"
                    "[r['executed'], r['complete'], r['data']['read_state'], r['data']['text']]",
                ),
            ).run()
        )
        assert body["status"] == "completed", body
        assert body["result"] == [True, False, "externalized", None]
        assert len(case.log) == 1
    finally:
        current_work_control.reset(token)


async def test_local_readback_still_has_cumulative_native_resource_limit(database, tmp_path):
    case = await readback_env(database, tmp_path, tool_limit=1)
    handle = await case.store.write_artifact(
        provider_id="core",
        tool_name="workspace_read",
        media_type="application/json",
        content=json.dumps({"ok": True, "data": {"text": "data"}}),
        access=case.env.control.context_access,
    )
    case.env.host.limits = replace(case.env.host.limits, max_total_suspensions=8)
    token = current_work_control.set(case.env.control)
    try:
        body = json.loads(
            await CodeModeDriver(
                case.env.host,
                outer_call(
                    case.env,
                    "for i in range(100):\n"
                    f"    await yuki_read_tool_artifact({{'handle': '{handle}'}})\n"
                    "'should not finish'",
                ),
            ).run()
        )
        assert body["status"] != "completed"
        assert "suspension" in body["detail"]
        assert 0 < len(case.log) < 100
        _, tools, root = await effect_rows(database, case.env.control.current["id"])
        assert tools == root == 0
    finally:
        current_work_control.reset(token)
