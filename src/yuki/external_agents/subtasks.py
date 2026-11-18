"""The one subtask tool set, when the executor is a native agent on a device (D1, D5).

``start_subtask``/``message_task``/``manage_task``/``get_tasks`` call here when the
target Work is a delegation or observation record. Everything goes through the
existing ``ExternalAgentService`` registration (non-blocking dispatch, receipts,
capability refusals at the point of execution); no vendor-specific branch exists.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from sqlalchemy import func, select, update

from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel
from yuki.external_agents.delegation import (
    DelegationConflict,
    Delegations,
    executor_of,
    native_prompt,
)
from yuki.external_agents.errors import ExternalAgentArgumentError
from yuki.runtime.work_repository import TERMINAL
from yuki.runtime.work_schema_v1 import work

_BRIEF = ("goal", "context", "acceptance", "files")


def _service(control: Any) -> Any:
    service = control.repository.database.external_agents
    if service is None:
        raise ValueError("external_agent_unavailable")
    return service


def _request_id(work_id: str, key: str, purpose: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"delegation:{work_id}:{purpose}:{digest}"


def _actor(control: Any) -> str:
    actor = control.source.get("actor_person_id")
    if not isinstance(actor, str):
        raise ValueError("external_agent_person_source_required")
    return actor


async def start_external(control: Any, args: dict[str, Any], key: str) -> dict[str, Any]:
    """Register a delegation (or observation) record, then dispatch its native action."""
    relationship = args.get("relationship", "delegate")
    executor_arg = args.get("executor")
    if not isinstance(executor_arg, dict):
        raise ExternalAgentArgumentError(
            "executor_required",
            "观察或接手需要 executor：agent 用 get_tasks 的 agents 目录中的 ID。",
        )
    if relationship != "delegate" and not args.get("session"):
        raise ExternalAgentArgumentError(
            "session_required", "observe 与 takeover 需要 session：用 agents 目录列出的会话 ID。"
        )
    if relationship != "observe" and control.current is None:
        raise ValueError("subtask_requires_current_task")
    service = _service(control)
    actor = _actor(control)
    instance = await service.repository.instance(executor_arg["agent"], actor)
    if executor_arg.get("device") not in (None, instance.device_id):
        raise ValueError("external_agent_target_mismatch")
    session_id = args.get("session")
    if session_id is not None:
        resource = await service.repository.native_session(session_id, actor)
        if resource.instance_id != instance.id:
            raise ValueError("external_agent_target_mismatch")
    executor: dict[str, Any] = {
        "device_id": instance.device_id,
        "instance_id": instance.id,
        "relationship": relationship,
    }
    for name in ("cwd", "scope"):
        if name in args:
            executor[name] = args[name]
    if session_id is not None:
        executor["session_id"] = session_id
    if relationship == "observe" and args.get("notify_turn_end"):
        executor["notify_turn_end"] = True
    brief = {name: args[name] for name in _BRIEF if name in args}
    if control.current is not None and relationship != "observe":
        async with control.repository.database.read_session() as reader:
            parent_source = await reader.scalar(
                select(work.c.source_json).where(work.c.id == control.current["id"])
            )
        source = json.loads(parent_source)
        parent_id: str | None = control.current["id"]
    else:
        source = dict(control.source)
        parent_id = None
    delegations = Delegations(control.repository.database)
    try:
        identity = await delegations.create(
            control.lease,
            parent_id=parent_id,
            key=key,
            goal=args["goal"],
            brief=brief,
            executor=executor,
            source=source,
        )
    except DelegationConflict as conflict:
        raise ExternalAgentArgumentError(
            "conflict",
            f"该会话已由委派 {conflict.owner} 拥有（原生会话同时只有一个回合）；"
            "用 message_task 追加，或等它结束。",
        ) from conflict
    native: dict[str, Any] = {"instance_id": instance.id}
    if session_id is not None:
        native["session_id"] = session_id
    if relationship == "observe":
        native.update(action="watch", observation="observe")
    else:
        native.update(action="prompt", prompt=native_prompt(brief))
        if session_id is not None:
            native["resume"] = True
        for name in ("cwd", "scope"):
            if name in args:
                native[name] = args[name]
    try:
        result = await service.execute(
            native, _request_id(identity, key, "start"), control.source, owner_work_id=identity
        )
    except Exception as error:
        await _end_unstarted(control, identity, error)
        raise
    if result.get("error") and result.get("executed") is False:
        await _end_unstarted(control, identity, ValueError(str(result["error"])))
        return {"id": identity, "status": "failed", "error": result["error"]}
    return {
        "id": identity,
        "status": "observing" if relationship == "observe" else "running",
        "relationship": relationship,
        "dispatch": {
            key: result.get(key)
            for key in ("status", "pending", "operation_id")
            if result.get(key) is not None
        },
    }


async def _end_unstarted(control: Any, identity: str, error: BaseException) -> None:
    """Nothing native was registered: the record fails with that fact, never retried."""
    code = getattr(error, "code", None) or str(error) or type(error).__name__
    async with control.repository.database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == identity, work.c.state.not_in(TERMINAL))
            .values(
                state="failed",
                reason=str(code)[:128],
                revision=work.c.revision + 1,
                updated=time.time(),
            )
        )


async def _latest_operation(control: Any, identity: str) -> AgentOperationModel | None:
    async with control.repository.database.read_session() as reader:
        operation: AgentOperationModel | None = await reader.scalar(
            select(AgentOperationModel)
            .where(
                AgentOperationModel.work_id == identity,
                AgentOperationModel.action.in_(("prompt", "queue", "watch")),
            )
            .order_by(AgentOperationModel.created_at.desc(), AgentOperationModel.id.desc())
            .limit(1)
        )
        return operation


async def message_external(
    control: Any, args: dict[str, Any], key: str, identity: str, executor: dict[str, Any]
) -> dict[str, Any]:
    """Text joins the native session; reply_to + answer settles one native request."""
    if executor["relationship"] == "observe":
        raise ExternalAgentArgumentError(
            "observe_only", "观察关系只看不答：不发送输入，也不回答或拒绝请求。"
        )
    service = _service(control)
    text = args.get("text")
    if "answer" in args or (args.get("reply_to") and "answer" in args):
        try:
            event_key = int(str(args.get("reply_to")))
        except ValueError as error:
            raise ExternalAgentArgumentError(
                "permission_id_required", "reply_to 填卡片里的 permission_id。"
            ) from error
        async with control.repository.database.read_session() as reader:
            event = await reader.get(AgentEventModel, event_key)
            operation = (
                await reader.get(AgentOperationModel, event.operation_id)
                if event is not None and event.operation_id is not None
                else None
            )
        if event is None or operation is None or operation.work_id != identity:
            raise ExternalAgentArgumentError(
                "external_agent_question_not_found", "reply_to 不是这个子任务的待回答请求。"
            )
        answer = args["answer"]
        native: dict[str, Any] = {
            "action": "respond",
            "operation_id": operation.id,
            "permission_id": str(event_key),
        }
        if isinstance(answer, dict):
            native["response"] = answer
        else:
            native["option_id"] = str(answer)
        payload = json.loads(event.payload_json).get("payload") or {}
        source = {
            **control.source,
            "answered_via": "yuki",
            "answer_reason": text,
            "answer_request": {"kind": event.kind, "tool": payload.get("tool")},
        }
        result = await service.execute(
            native, _request_id(identity, key, "answer"), source, owner_work_id=identity
        )
        return {"id": identity, "answered": str(event_key), "status": result.get("status")}
    if not text:
        raise ValueError("invalid_subagent_message")
    latest = await _latest_operation(control, identity)
    if latest is None or latest.session_id is None:
        raise ExternalAgentArgumentError(
            "session_not_ready", "原生会话还没有确认；等待回执后再追加。"
        )
    mode = args.get("mode", "queue")
    native = {"action": mode, "prompt": text}
    if mode == "steer":
        native["operation_id"] = latest.id
    else:
        native["session_id"] = latest.session_id
    result = await service.execute(
        native, _request_id(identity, key, mode), control.source, owner_work_id=identity
    )
    return {
        "id": identity,
        "status": result.get("status"),
        "operation_id": result.get("operation_id"),
        "pending": result.get("pending"),
    }


async def manage_external(
    control: Any,
    action: str,
    identity: str,
    key: str,
    text: str | None,
    executor: dict[str, Any],
) -> dict[str, Any]:
    """cancel / resume / release a delegation; cancel or release stops an observation."""
    from yuki.runtime.work_management import WorkManagementError, manage_work

    database = control.repository.database
    service = _service(control)
    observe = executor["relationship"] == "observe"
    if action == "resume":
        if observe:
            raise ExternalAgentArgumentError("observe_only", "观察不能恢复；需要时重新开始观察。")
        if not text:
            raise ExternalAgentArgumentError(
                "resume_text_required", "恢复编码 Agent 子任务需要 text：在同一会话继续的要求。"
            )
        latest = await _latest_operation(control, identity)
        async with database.immediate_session() as writer:
            await control.repository._assert_lease(writer, control.lease)
            await writer.execute(
                update(work)
                .where(work.c.id == identity, work.c.state.in_(("completed", "failed")))
                .values(
                    state="waiting_external",
                    reason="native_executor",
                    revision=work.c.revision + 1,
                    updated=time.time(),
                )
            )
        native: dict[str, Any] = {
            "action": "prompt",
            "instance_id": executor["instance_id"],
            "prompt": text,
            "resume": True,
        }
        session = executor.get("session_id") or (latest.session_id if latest else None)
        if session is not None:
            native["session_id"] = session
        result = await service.execute(
            native, _request_id(identity, key, "resume"), control.source, owner_work_id=identity
        )
        return {"id": identity, "status": "running", "dispatch": result.get("status")}
    if action == "release" or observe:
        # Release (or stopping an observation) detaches Yuki's operations first; a
        # releasing connector closes Yuki's own native process at the turn boundary.
        await service.release(
            identity,
            control.source,
            request_id=_request_id(identity, key, action),
            close_owner=action == "release" and not observe,
        )
    async with database.immediate_session() as writer:
        await control.repository._assert_lease(writer, control.lease)
        row = (await writer.execute(select(work).where(work.c.id == identity))).mappings().one()
        if row["state"] not in TERMINAL:
            if action == "release" or observe:
                # Ending Yuki's relationship only: the operations are already detached,
                # so no native cancel follows (taskbook §4.11).
                await writer.execute(
                    update(work)
                    .where(work.c.id == identity)
                    .values(
                        state="cancelled",
                        reason="released" if action == "release" else "observation_stopped",
                        revision=work.c.revision + 1,
                        updated=time.time(),
                    )
                )
            else:
                try:
                    await manage_work(writer, identity, row["revision"], "cancel", reason=text)
                except WorkManagementError as exc:
                    raise ValueError(exc.code) from exc
    return {"id": identity, "status": "cancelled", "relationship": executor["relationship"]}


async def record_executor(control: Any, identity: str) -> dict[str, Any] | None:
    async with control.repository.database.read_session() as reader:
        return await executor_of(reader, identity)


async def browse(control: Any, agents: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """get_tasks(agents=…): devices and agents, an agent's sessions, or a session's history."""
    from yuki.external_agents.views import agent_view

    service = _service(control)
    actor = _actor(control)
    # Reads are not registered; the id only names this one device RPC.
    request_id = f"browse:{time.time_ns()}"
    if "session" in agents:
        wire: dict[str, Any] = {"action": "get", "session_id": agents["session"]}
        if args.get("history"):
            wire["include_history"] = True
        if "cursor" in args:
            wire["native_cursor"] = args["cursor"]
        return agent_view(await service.execute(wire, request_id, {"actor_person_id": actor}))
    if "agent" in agents:
        wire = {"action": "list", "instance_id": agents["agent"]}
        if "cursor" in args:
            wire["native_cursor"] = args["cursor"]
        return agent_view(await service.execute(wire, request_id, {"actor_person_id": actor}))
    catalog = await service.catalog(actor, agents.get("device"))
    running = await _running_records(control, actor)
    for device in catalog["devices"]:
        device["records"] = running.get(device["id"], [])
    return agent_view(catalog)


async def _running_records(control: Any, actor: str) -> dict[str, list[dict[str, Any]]]:
    """Facts on the card: what each device is running or waiting on (no cap, D3)."""
    from yuki.identity.person_identity import person_family_condition
    from yuki.runtime.subagent_schema import children

    rows: dict[str, list[dict[str, Any]]] = {}
    async with control.repository.database.read_session() as reader:
        records = (
            await reader.execute(
                select(work.c.id, work.c.state, work.c.reason, children.c.executor_json)
                .join(children, children.c.work_id == work.c.id)
                .where(
                    children.c.executor_json.is_not(None),
                    work.c.state.not_in(TERMINAL),
                    person_family_condition(
                        func.json_extract(work.c.source_json, "$.actor_person_id"), actor
                    ),
                )
                .order_by(work.c.created, work.c.id)
            )
        ).all()
    from yuki.external_agents.delegation import delegation_state

    for identity, state, reason, executor_json in records:
        executor = json.loads(executor_json)
        rows.setdefault(executor["device_id"], []).append(
            {
                "id": identity,
                "agent": executor["instance_id"],
                "relationship": executor["relationship"],
                "state": delegation_state(state, reason),
            }
        )
    return rows
