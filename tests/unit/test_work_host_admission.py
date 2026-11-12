"""The Host's own admission of a private request's tool execution is a recorded fact."""

import json
from dataclasses import replace
from types import SimpleNamespace

from tests.support.work_runner_helpers import case, run, tool

from yuki.client_api.works import ClientWorks
from yuki.domain.messages import ChatResponse, ChatTool
from yuki.runtime.work_control import work_control_tools
from yuki.social.tools import social_tool_definitions


async def neutral_private(database, tmp_path, calls):
    """A private Main with no Work yet, whose first batch is ``calls``."""
    test_case = await case(
        database, tmp_path, [ChatResponse("", 0, tool_calls=calls), ChatResponse("", 0)]
    )
    test_case.runtime = replace(
        test_case.runtime,
        fixed_tools=(
            *work_control_tools(),
            *social_tool_definitions(),
            ChatTool("save_memory", "remember", {"type": "object"}),
        ),
    )
    test_case.control.current = None
    test_case.control.source_key = "neutral-private-source"
    await run(test_case)
    return test_case


async def admitted(test_case):
    row = await test_case.repository.get(test_case.control.current["id"])
    return row, json.loads(row["checkpoint_json"])


async def projection(database, row):
    async with database.read_session() as session:
        works = ClientWorks(SimpleNamespace(database=database), {})
        return (await works.public_many(session, [row]))[0]


async def test_host_execution_admission_is_recorded_and_survives_a_goal_update(database, tmp_path):
    test_case = await neutral_private(
        database,
        tmp_path,
        (
            tool("save_memory", {}, "remember"),
            tool("manage_task", {"action": "update", "goal": "整理本週訪談摘要"}, "update"),
        ),
    )
    row, checkpoint = await admitted(test_case)
    # 1: the Host admitted this Work for the private request's own tool execution.
    assert checkpoint["admission"] == "host_execution"
    # 2: rewriting the goal keeps how the Work was admitted.
    assert row["goal"] == "整理本週訪談摘要"
    # 4: clients read the fact as it is recorded.
    assert (await projection(database, row))["admission"] == "host_execution"


async def test_an_explicit_start_is_not_a_host_admission(database, tmp_path):
    test_case = await neutral_private(
        database,
        tmp_path,
        (
            tool("manage_task", {"action": "start", "goal": "每天整理科技新聞"}, "start"),
            tool("save_memory", {}, "remember"),
        ),
    )
    row, checkpoint = await admitted(test_case)
    # 3 and 4: a manage_task start records no admission fact, and clients see null.
    assert "admission" not in checkpoint
    assert (await projection(database, row))["admission"] is None
