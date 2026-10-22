"""Original payload types, batch atomicity and completion survive short writers."""

import json

import pytest
from sqlalchemy import func, select
from tests.unit.test_external_agent_repository import event, prepare, register_device
from tests.unit.test_external_agent_work import case as work_case

from yuki.external_agents.db_models import AgentEventModel
from yuki.runtime.work_schema_v1 import inputs
from yuki.sandbox.db_models import SandboxTaskRunModel

case = work_case


async def owned(case):
    env = await register_device(case.db, case.source["actor_person_id"])
    env.source = {
        **case.source,
        "work_id": case.control.current["id"],
        "conversation_id": case.control.lease.conversation_id,
    }
    env.session = (
        await env.repository.remember_sessions(env.instance, env.actor, ["native-thread"])
    )["native-thread"]
    operation, _ = await prepare(env, action="prompt", lease=case.control.lease)
    return env, operation


async def test_large_native_payload_preserves_null_arrays_booleans_and_internal_question(case):
    env, operation = await owned(case)
    detail = {"nullable": None, "rows": [None, False, ["original" * 20000]]}
    response = {
        "pending": True,
        "status": "running",
        "nullable": None,
        "detail": detail,
        "session_id": "native-thread",
        "execution_id": "native-turn",
    }
    await env.repository.response(operation.id, response)
    await env.repository.receive_events(
        env.device["device_id"],
        [
            event("progress", payload={"admitted": True, "status": None}),
            event(
                "question", kind="permission", payload={"native_request_id": "native-q", **detail}
            ),
        ],
    )
    current = await env.repository.operation(operation.id, env.actor)
    stored = json.loads(current.result_json)
    assert stored["nullable"] is None and stored["detail"] == detail
    assert stored["status"] is None and stored["pending"] is True
    async with case.db.read_session() as reader:
        internal = await reader.scalar(
            select(AgentEventModel.id).where(AgentEventModel.event_id == "question")
        )
        raw = await reader.scalar(
            select(inputs.c.payload_json).where(
                inputs.c.source_key == f"external-agent:question:{operation.id}"
            )
        )
    payload = json.loads(raw)
    assert isinstance(payload["text"], str) and payload["signal"] is True
    question = json.loads(payload["text"])
    assert question["permission_id"] == str(internal)
    assert question["payload"] == {"native_request_id": "native-q", **detail}
    await env.repository.receive_events(
        env.device["device_id"],
        [
            event(
                "completed",
                kind="completed",
                payload={"status": "completed", "detail": detail, "native_pending": True},
            )
        ],
    )
    await env.repository.response(operation.id, response)
    async with case.db.read_session() as reader:
        task = await reader.get(SandboxTaskRunModel, operation.id)
    completion = json.loads(task.completion_json)
    assert completion["status"] == "succeeded" and completion["pending"] is False
    assert completion["native_status"] == "completed" and completion["native_pending"] is True
    assert completion["detail"] == detail
    assert isinstance(completion["native_reference"], dict)


async def test_later_invalid_event_rolls_back_entire_original_batch(case):
    env, operation = await owned(case)
    original = {"pending": True, "status": "unknown", "nullable": None}
    await env.repository.response(operation.id, original)
    with pytest.raises(ValueError, match="external_agent_operation_unknown"):
        await env.repository.receive_events(
            env.device["device_id"],
            [
                event("first", payload={"admitted": True}),
                event("later", operation="missing-original"),
            ],
        )
    current = await env.repository.operation(operation.id, env.actor)
    assert json.loads(current.result_json) == original
    async with case.db.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(AgentEventModel)) == 0
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(inputs)
                .where(inputs.c.work_id == operation.work_id)
            )
            == 0
        )
