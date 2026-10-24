"""Opt-in Main model script; native transport, Work and receipts remain real."""

import json
from uuid import uuid4

from sqlalchemy import select

from yuki.client_api.agent_target import stored_target
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.social.db_models import SocialOperationModel


def call(name, arguments):
    return ChatResponse(
        content="",
        latency_seconds=0,
        tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
    )


class DesktopNativeWorkScenario:
    """Choose tools from the original App input and wait for its actual native run."""

    def __init__(self, container):
        self.container = container
        self.marker = None
        self.event_id = None
        self.work_id = None
        self.operation_id = None
        self.finish_phase = 0
        self.responded = set()

    async def respond(self, request):
        control = current_work_control.get()
        if self.marker is None or control is None:
            return None
        async with self.container.database.read_session() as reader:
            original = await reader.get(ChatEventModel, control.source.get("trigger_event_id"))
            if original is None:
                return None
            segments = json.loads(original.segments_json)
            target = stored_target(segments)
            if target is None:
                return None
            text = segments[0]["data"]["text"]
            operation = (
                await reader.get(AgentOperationModel, self.operation_id)
                if self.operation_id is not None
                else None
            )
            if target.intent == "respond" and target.operation_id == self.operation_id:
                if original.id not in self.responded:
                    self.responded.add(original.id)
                    arguments = target.model_dump(exclude_none=True, exclude={"intent"})
                    return call("external_agent", {"action": "respond", **arguments})
                if control.current is None:
                    return ChatResponse(content="原問題回覆已提交。", latency_seconds=0)
            elif self.marker not in text or target.intent != "prompt":
                return None
            else:
                if self.event_id is None:
                    self.event_id = original.id
                assert original.id == self.event_id
                if control.current is None:
                    return call("task_control", {"action": "accept", "goal": self.marker})
                self.work_id = control.current["id"]
                operation = (
                    await reader.scalars(
                        select(AgentOperationModel).where(
                            AgentOperationModel.work_id == self.work_id,
                            AgentOperationModel.action == "prompt",
                        )
                    )
                ).one_or_none()
                if operation is None:
                    arguments = target.model_dump(exclude_none=True, exclude={"intent"})
                    return call("external_agent", {"action": "prompt", **arguments, "prompt": text})
                self.operation_id = operation.id
            assert operation is not None and operation.run_id is not None
            task = await reader.get(SandboxTaskRunModel, operation.id)
            assert task is not None
            if task.status != "completed":
                return call(
                    "task_control",
                    {
                        "action": "wait",
                        "conditions": [{"kind": "owned_run", "run_id": task.run_id}],
                    },
                )
            completion = json.loads(task.completion_json)
        self.finish_phase += 1
        if self.finish_phase == 1:
            return call("external_agent", {"action": "get", "operation_id": operation.id})
        if self.finish_phase == 2:
            assert operation.id in request.messages[-1].content
            return call(
                "send",
                {
                    "text": json.dumps(
                        {
                            "status": completion["status"],
                            "text": completion.get("text"),
                            "operation_id": operation.id,
                        }
                    )
                },
            )
        if self.finish_phase == 3:
            return call("task_control", {"action": "complete", "result": completion["status"]})
        return ChatResponse(content="原生任務回執已結算。", latency_seconds=0)

    def attach(self, app):
        @app.post("/__test/native-scenario")
        async def setup(body: dict[str, str]):
            self.marker = body["marker"]
            return {"marker": self.marker}

        @app.post("/__test/native-disconnect")
        async def disconnect(body: dict[str, str]):
            connection = self.container.external_agents.connections.get(body["device_id"])
            await connection.socket.close(code=1012, reason="isolated_native_acceptance")
            return {"device_id": connection.device_id, "connection_id": connection.id}

        @app.get("/__test/native-facts")
        async def facts():
            async with self.container.database.read_session() as reader:
                rows = (
                    (
                        await reader.scalars(
                            select(AgentOperationModel).where(
                                AgentOperationModel.work_id == self.work_id
                            )
                        )
                    ).all()
                    if self.work_id
                    else []
                )
                current = (
                    (
                        (await reader.execute(select(work).where(work.c.id == self.work_id)))
                        .mappings()
                        .one_or_none()
                    )
                    if self.work_id
                    else None
                )
                mailbox = (
                    (
                        await reader.execute(
                            select(inputs)
                            .where(inputs.c.work_id == self.work_id)
                            .order_by(inputs.c.id)
                        )
                    )
                    .mappings()
                    .all()
                    if self.work_id
                    else []
                )
                operations = []
                events = []
                deliveries = []
                for row in rows:
                    task = await reader.get(SandboxTaskRunModel, row.id) if row.run_id else None
                    operations.append(
                        {
                            "id": row.id,
                            "action": row.action,
                            "actor_person_id": row.actor_person_id,
                            "work_id": row.work_id,
                            "run_id": row.run_id,
                            "session_id": row.session_id,
                            "execution_id": row.native_execution_id,
                            "command": json.loads(row.command_json),
                            "result": json.loads(row.result_json) if row.result_json else None,
                            "task_status": task.status if task else None,
                            "completion": json.loads(task.completion_json)
                            if task and task.completion_json
                            else None,
                        }
                    )
                    events.extend(
                        {
                            "event_id": item.event_id,
                            "operation_id": item.operation_id,
                            "kind": item.kind,
                            "payload": json.loads(item.payload_json),
                        }
                        for item in await reader.scalars(
                            select(AgentEventModel)
                            .where(AgentEventModel.operation_id == row.id)
                            .order_by(AgentEventModel.id)
                        )
                    )
                if current is not None and self.operation_id is not None:
                    for receipt, event in await reader.execute(
                        select(SocialOperationModel, ChatEventModel)
                        .join(ChatEventModel, ChatEventModel.id == SocialOperationModel.event_id)
                        .where(
                            ChatEventModel.canonical_conversation_id == current["conversation_id"],
                            ChatEventModel.direction == "outbound",
                            ChatEventModel.content.contains(self.operation_id),
                        )
                    ):
                        deliveries.append(
                            {
                                "operation_id": receipt.id,
                                "status": receipt.status,
                                "source_conversation_id": receipt.source_conversation_id,
                                "original_event_id": receipt.original_event_id,
                                "original_conversation_id": receipt.original_conversation_id,
                                "event_id": event.id,
                                "content": event.content,
                                "author_kind": event.author_kind,
                                "direction": event.direction,
                                "conversation_id": event.canonical_conversation_id,
                            }
                        )
            return {
                "event_id": self.event_id,
                "work": dict(current) if current else None,
                "operations": operations,
                "events": events,
                "deliveries": deliveries,
                "inputs": [dict(row) for row in mailbox],
                "connections": [
                    {"device_id": row.device_id, "connection_id": row.id}
                    for row in self.container.external_agents.connections.active()
                ],
            }
