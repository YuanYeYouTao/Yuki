"""Explicit user-selected native targets travel with the original App input."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from yuki.client_api.errors import ClientError
from yuki.external_agents.repository import AgentRepository, session_observation
from yuki.persistence.database import Database


class AgentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str | None = None
    instance_id: str | None = None
    session_id: str | None = None
    execution_id: str | None = None
    operation_id: str | None = None
    cwd: str | None = None
    intent: Literal["prompt", "watch", "queue", "steer", "respond", "cancel", "detach"] | None = (
        None
    )
    permission_id: str | None = None
    option_id: str | None = None
    response: dict[str, JsonValue] | None = None


def target_data(target: AgentTarget | None) -> dict[str, Any] | None:
    return target.model_dump(exclude_none=True) if target is not None else None


def tool_arguments(target: AgentTarget, record: str | None = None) -> dict[str, Any]:
    """The App's selection in the one subtask tool set; the App payload is unchanged.

    ``record`` is the delegation or observation the selected operation belongs to,
    resolved from the original operation when the target was authorized.
    """
    data = target.model_dump(exclude_none=True)
    intent = data.get("intent")
    executor = {
        key: value
        for key, value in (("agent", data.get("instance_id")), ("device", data.get("device_id")))
        if value is not None
    }
    arguments: dict[str, Any]
    if intent in {None, "prompt", "watch"}:
        arguments = {"tool": "start_subtask"}
        if executor:
            arguments["executor"] = executor
        if "session_id" in data:
            arguments["session"] = data["session_id"]
        if "cwd" in data:
            arguments["cwd"] = data["cwd"]
        if intent == "watch":
            arguments["relationship"] = "observe"
    elif intent in {"queue", "steer"}:
        arguments = {"tool": "message_task", "mode": intent}
    elif intent == "respond":
        arguments = {"tool": "message_task", "reply_to": data.get("permission_id")}
        if "option_id" in data or "response" in data:
            arguments["answer"] = data.get("option_id", data.get("response"))
    else:
        arguments = {"tool": "manage_task", "action": "cancel" if intent == "cancel" else "release"}
    if intent in {"queue", "steer", "respond", "cancel", "detach"}:
        if record is None:
            # An operation recorded before contract 40 belongs to no delegation; the
            # retired tools that controlled it have no successor (tool-kernel.md).
            return {
                "note": "所选原操作不属于任何委派，现行工具不能控制它；可查看其回执或新建委派。",
                "operation_id": data.get("operation_id"),
            }
        arguments["id"] = record
    return arguments


def input_content(body: str, target: AgentTarget | None, record: str | None = None) -> str:
    if target is None:
        return body
    return (
        body
        + "\n[用户明确选择的本机 Agent 目标与操作]\n"
        + json.dumps(tool_arguments(target, record), ensure_ascii=False, sort_keys=True)
    )


def input_payload(body: str, target: AgentTarget | None) -> dict[str, Any]:
    return {"text": body, **({"agent_target": target_data(target)} if target is not None else {})}


def target_segments(target: AgentTarget | None) -> tuple[dict[str, Any], ...]:
    return ({"type": "agent_target", "data": target_data(target)},) if target is not None else ()


def stored_target(segments: list[dict[str, Any]]) -> AgentTarget | None:
    return next(
        (
            AgentTarget.model_validate(part["data"])
            for part in segments
            if part["type"] == "agent_target"
        ),
        None,
    )


async def target_record(database: Database, target: AgentTarget | None) -> str | None:
    """The record a selected original operation belongs to (a fact, not a new grant).

    A replay of an already accepted request renders with it without authorizing
    the device again (the original receipt does not depend on the device today).
    """
    if target is None or target.operation_id is None:
        return None
    from yuki.external_agents.db_models import AgentOperationModel

    async with database.read_session() as reader:
        operation = await reader.get(AgentOperationModel, target.operation_id)
    return operation.work_id if operation is not None else None


async def authorize_target(
    database: Database, actor: str, target: AgentTarget | None
) -> str | None:
    """Authorize the selection; return the record its original operation belongs to."""
    if target is None:
        return None
    repository = AgentRepository(database)
    try:
        operation = (
            await repository.operation(target.operation_id, actor) if target.operation_id else None
        )
        execution = (
            await repository.native_execution(target.execution_id, actor)
            if target.execution_id
            else None
        )
        if (
            operation is not None
            and execution is not None
            and (
                operation.session_id != execution.session_id
                or (
                    not session_observation(operation)
                    and operation.native_execution_id != execution.native_id
                )
            )
        ):
            raise ValueError("external_agent_target_mismatch")
        session_id = (
            execution.session_id
            if execution
            else operation.session_id
            if operation
            else target.session_id
        )
        if target.session_id is not None and target.session_id != session_id:
            raise ValueError("external_agent_target_mismatch")
        session = await repository.native_session(session_id, actor) if session_id else None
        instance_id = (
            session.instance_id
            if session
            else operation.instance_id
            if operation
            else target.instance_id
        )
        if instance_id is None or (
            target.instance_id is not None and target.instance_id != instance_id
        ):
            raise ValueError("external_agent_target_mismatch")
        instance = await repository.instance(instance_id, actor)
        if target.device_id is not None and target.device_id != instance.device_id:
            raise ValueError("external_agent_target_mismatch")
        async with database.read_session() as reader:
            await repository.identity.require_device(reader, instance.device_id)
    except ValueError as error:
        raise ClientError("not_found") from error
    return operation.work_id if operation is not None else None
