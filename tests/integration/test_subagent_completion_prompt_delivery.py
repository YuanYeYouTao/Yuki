"""Real child completion -> parent request -> local image-delivery receipts.

Failure cases are specified in the 2026-11-01 taskbook before implementation:
* V01: a child finishes with no later chat input and the parent delivers its image;
* V02: conversation wins an any wait first, the parent does independent work and
  waits on the original child again, then delivers without a second chat input;
* the actual Main/worker requests must expose the child_id/owned_run bridge and
  original-goal guidance, including the completion detail seen by the parent.

The Provider returns scripted calls. These cases exercise real ingress, Work,
SQLite, child execution, wait delivery, parent resumption and Social receipts;
they do not measure a real model's probability of choosing the right calls.
"""

import hashlib
import io
import json
import time
from dataclasses import asdict, replace

import pytest
from PIL import Image
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.support.runtime_execution import make_child_executor, make_work_resumer
from tests.support.workspace_snapshots import snapshot_bytes
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import effects, inputs, work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.runtime.work_wait_schema import waits
from yuki.services.main_agent_contract import MainAgentContract
from yuki.social.db_models import SocialOperationModel

GOAL = "委托子任务找图并把图片发送到原群；继续聊天不取消找图工作"
CHILD_RESULT = "原图已经核对，可以按原目标交付"
INDEPENDENT_PROGRESS = "群消息已处理，原图片任务仍需继续"


def _last_tool_result(request, call_id):
    message = next(
        message
        for message in reversed(request.messages)
        if message.role == "tool" and message.tool_call_id == call_id
    )
    return json.loads(message.content)


def _child_notice(request, child_id):
    for message in request.messages:
        if message.role != "user" or "\n" not in (message.content or ""):
            continue
        _, _, body = message.content.partition("\n")
        try:
            value = json.loads(body)
        except ValueError:
            continue
        if value.get("child_id") == child_id and value.get("state") == "completed":
            return value
    raise AssertionError("the original completion notice did not reach the parent request")


def _guidance(request):
    tools = {tool.name: tool for tool in request.tools}
    spawn = tools["subagent_start"].description
    control = tools["task_control"]
    properties = control.parameters["properties"]
    system = "\n".join(message.content for message in request.messages if message.role == "system")
    return {
        "explicit_delivery": "send" in system and "交付" in system,
        "spawn_identifies_waitable_child": "child_id" in spawn and "wait" in spawn,
        "wait_parameter_connects_child": "child_id" in properties["run_id"].get("description", ""),
        "condition_example_connects_child": all(
            text in properties["conditions"].get("description", "")
            for text in ("owned_run", "child_id", "conversation")
        ),
        "remaining_dependencies_after_any": "剩余" in control.description
        and "any" in control.description,
    }


@pytest.mark.parametrize("conversation_first", [False, True], ids=["child-only", "chat-then-child"])
async def test_child_result_reaches_original_parent_request_and_image_receipt(
    database, tmp_path, tool_delivery_evidence, conversation_first
):
    directory, evidence = tool_delivery_evidence
    evidence.update(
        scenario="chat-before-child" if conversation_first else "child-without-new-chat",
        provider="scripted; validates mechanisms and visible guidance, not model choice rate",
    )
    provider = FakeLLMProvider()
    env, harness, chat, short_state, message = await _scene(database, tmp_path, provider)
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    chat.runtime.runner.main_contract = MainAgentContract(chat, short_state)
    database.subagents_enabled = True
    image = io.BytesIO()
    Image.new("RGB", (3, 2), color="blue").save(image, format="PNG")
    image_bytes = image.getvalue()
    (directory / "original.png").write_bytes(image_bytes)
    artifact = snapshot_bytes(env.store, "original.png", image_bytes)
    child_result = json.dumps(
        {"summary": CHILD_RESULT, "artifact_id": artifact["artifact_id"]}, ensure_ascii=False
    )
    script = {"phase": "accept", "child_id": None, "child_done": False}
    parent_requests = []
    worker_requests = []

    def respond(request):
        if any(
            message.role == "user" and '"cwd": "/workspace/tasks/' in message.content
            for message in request.messages
        ):
            worker_requests.append(request)
            return _tool(
                "task_control", {"action": "complete", "result": child_result}, "child-complete"
            )
        parent_requests.append(request)
        phase = script["phase"]
        if phase == "accept":
            script["phase"] = "spawn"
            return _tool("task_control", {"action": "accept", "goal": GOAL}, "parent-accept")
        if phase == "spawn":
            script["phase"] = "wait"
            return _tool(
                "subagent_start",
                {"goal": "核对原图片并回传已发布的 artifact 引用", "acceptance": "返回核对结果"},
                "spawn-original-child",
            )
        if phase == "wait":
            child_id = _last_tool_result(request, "spawn-original-child")["child_id"]
            script["child_id"] = child_id
            script["phase"] = "await-child"
            arguments = {"action": "wait", "run_id": child_id}
            if conversation_first:
                arguments = {
                    "action": "wait",
                    "wait_mode": "any",
                    "conditions": [
                        {"kind": "owned_run", "run_id": child_id},
                        {"kind": "conversation"},
                    ],
                }
            return _tool("task_control", arguments, "wait-original-child")
        if phase == "await-child" and not script["child_done"]:
            assert conversation_first, "the parent resumed without a completion or conversation"
            script["phase"] = "rearm-child"
            return _tool(
                "update_short_state", {"slot": 2, "text": INDEPENDENT_PROGRESS}, "independent-work"
            )
        if phase == "rearm-child":
            script["phase"] = "await-child"
            return _tool(
                "task_control",
                {"action": "wait", "run_id": script["child_id"]},
                "wait-remaining-original-child",
            )
        assert phase == "await-child" and script["child_done"], script
        notice = _child_notice(request, script["child_id"])
        assert json.loads(notice["text"])["artifact_id"] == artifact["artifact_id"]
        evidence["model_observed_completion"] = notice
        script["phase"] = "finished"
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(
                    "deliver-original-image",
                    ToolFunction(
                        "send",
                        json.dumps(
                            {
                                "artifact_id": artifact["artifact_id"],
                                "attachment_kind": "image",
                                "text": "原图片任务的结果",
                            }
                        ),
                    ),
                ),
                ToolCall(
                    "parent-complete",
                    ToolFunction("task_control", '{"action":"complete","result":"已交付原图"}'),
                ),
            ),
        )

    provider._responder = respond
    repository = WorkRepository(database)
    wait_repository = WorkWaitRepository(repository)
    resumer = make_work_resumer(
        repository,
        ledger=harness.ledger,
        scopes=chat._conversation_scopes,
        turns=chat._turn_coordinator,
        router=env.router,
        config=chat._runtime_config,
        generate_self=chat.generate_self_initiative,
        generate_wakeup=chat.generate_main_agent_wakeup,
        bindings=chat.runtime.bindings,
        settings=harness.settings,
    )
    scheduler = WorkScheduler(repository, resumer.resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    try:
        await harness.processor.handle(replace(message, text=GOAL), MemorySender())
        async with database.read_session() as reader:
            parent = dict(
                (await reader.execute(select(work).where(work.c.parent_work_id.is_(None))))
                .mappings()
                .one()
            )
        child_id = script["child_id"]
        original_source = json.loads(parent["source_json"])
        evidence.update(parent_id=parent["id"], child_id=child_id, original_source=original_source)
        assert parent["state"] == "waiting_external"
        assert (await repository.get(child_id))["parent_work_id"] == parent["id"]
        first_wait = await wait_repository.describe(parent["id"])
        evidence["first_wait"] = first_wait
        assert first_wait["conditions"][0]["run_id"] == child_id
        if conversation_first:
            await harness.processor.handle(
                replace(
                    message,
                    ingress_key="chat-before-child",
                    native_message_id="chat-before-child",
                    text="先继续聊，原图片任务仍然要完成",
                ),
                MemorySender(),
            )
            await scheduler.drain_once()
            second_wait = await wait_repository.describe(parent["id"])
            evidence["remaining_wait"] = second_wait
            assert second_wait["wait_id"] != first_wait["wait_id"]
            assert second_wait["status"] == "active"
            assert second_wait["conditions"][0]["run_id"] == child_id
            assert any(row["text"] == INDEPENDENT_PROGRESS for row in short_state.snapshot())

        async with database.read_session() as reader:
            inbound_before = set(
                await reader.scalars(
                    select(ChatEventModel.id).where(ChatEventModel.direction == "inbound")
                )
            )
        executor = make_child_executor(
            repository,
            chat=chat,
            config=chat._runtime_config,
            runner=chat.runtime.runner,
            load_tools=chat.runtime.runner.main_contract.definitions,
            router=env.router,
        )
        assert await executor.run(child_id) is None
        script["child_done"] = True
        assert (await repository.get(child_id))["state"] == "completed"
        assert await wait_repository.deliver_due() == 1
        await scheduler.drain_once()
        final_parent = await repository.get(parent["id"])
        assert final_parent["state"] == "completed"
        assert json.loads(final_parent["source_json"]) == original_source
        async with database.read_session() as reader:
            inbound_after = set(
                await reader.scalars(
                    select(ChatEventModel.id).where(ChatEventModel.direction == "inbound")
                )
            )
            receipts = list(
                await reader.scalars(
                    select(SocialOperationModel).where(
                        SocialOperationModel.action == "send_message"
                    )
                )
            )
            assert inbound_after == inbound_before
            assert len(receipts) == 1 and receipts[0].status == "succeeded"
            delivery = await reader.get(ChatEventModel, receipts[0].event_id)
            assert artifact["artifact_id"] in delivery.segments_json
            assert delivery.canonical_conversation_id == parent["conversation_id"]
            assert delivery.native_message_id == receipts[0].platform_reference
        assert hashlib.sha256(env.store.read_bytes(artifact["artifact_id"])[1]).hexdigest() == (
            hashlib.sha256(image_bytes).hexdigest()
        )
        assert script["phase"] == "finished"
        notice = evidence["model_observed_completion"]
        guidance = {
            "parent": _guidance(parent_requests[0]),
            "worker": _guidance(worker_requests[0]),
            "completion_continues_original_goal": "原" in notice["detail"]
            and "交付" in notice["detail"],
        }
        evidence["visible_guidance"] = guidance
        missing = {
            f"{actor}.{key}": value
            for actor in ("parent", "worker")
            for key, value in guidance[actor].items()
            if not value
        }
        if not guidance["completion_continues_original_goal"]:
            missing["completion_continues_original_goal"] = False
        assert not missing, missing
    finally:
        evidence["artifact"] = {**artifact, "sha256": hashlib.sha256(image_bytes).hexdigest()}
        evidence["script_state"] = script
        evidence["transport_calls"] = env.bot.calls
        (directory / "model-requests.json").write_text(
            json.dumps(
                [asdict(request) for request in provider.requests], ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        evidence["model_requests_artifact"] = str(directory / "model-requests.json")
        async with database.read_session() as reader:
            for name, table in (
                ("work", work),
                ("waits", waits),
                ("inputs", inputs),
                ("effects", effects),
                ("children", children),
            ):
                evidence[name] = [
                    dict(row) for row in (await reader.execute(select(table))).mappings()
                ]
            evidence["delivery_receipts"] = [
                {
                    "id": row.id,
                    "action": row.action,
                    "source_turn_id": row.source_turn_id,
                    "tool_call_id": row.tool_call_id,
                    "status": row.status,
                    "event_id": row.event_id,
                    "platform_reference": row.platform_reference,
                }
                for row in await reader.scalars(select(SocialOperationModel))
            ]
