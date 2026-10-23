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


def input_content(body: str, target: AgentTarget | None) -> str:
    if target is None:
        return body
    return (
        body
        + "\n[用户明确选择的本机 Agent 目标与操作]\n"
        + json.dumps(target_data(target), ensure_ascii=False, sort_keys=True)
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


async def authorize_target(database: Database, actor: str, target: AgentTarget | None) -> None:
    if target is None:
        return
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
