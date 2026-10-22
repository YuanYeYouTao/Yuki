"""Natural-language Agent tools for ordinary users and superusers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, ClassVar

from yuki.automation.models import (
    AutomationDirectoryEntry,
    AutomationRecord,
    AutomationStatus,
)
from yuki.automation.service import AutomationService
from yuki.capabilities.results import ToolExecutionResult
from yuki.domain.messages import ChatTool
from yuki.persistence.database import write_failure_committed, write_failure_phase
from yuki.services.agent_tools import ToolRuntime
from yuki.time.formatting import local_iso

_CREATE_DESCRIPTION = (
    "创建持久化安排。到期激活同一个主 Agent，完整处理目标与约束，包括固定文字提醒。"
    "运行时使用主 Agent 的完整工具，按创建者当前权限执行，无需选择工具或预算。"
    "需要到时发消息时明确设置 delivery.target：群聊用 current_group，私聊用 self_private；"
    "none 要求静默内部处理；其他目标选择投递对象，是否需要发言仍按任务要求判断。"
    "创建时不要提前执行任务；收到 confirmation='persisted' 和 automation_id 才表示成功。"
)


def _object_schema(
    properties: Mapping[str, object], *, required: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _task_intent_schema() -> dict[str, object]:
    """Compact TaskSpec envelope for the model; backend still validates the real type."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "goal", "trigger"],
        "description": "完整的主 Agent 任务要求与时间安排。",
        "properties": {
            "version": {"type": "integer", "const": 1},
            "name": {"type": "string", "minLength": 1},
            "goal": {"type": "string", "minLength": 1},
            "trigger": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "seconds"],
                        "properties": {
                            "type": {"const": "after"},
                            "seconds": {
                                "type": "integer",
                                "minimum": 1,
                            },
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "local_datetime"],
                        "properties": {
                            "type": {"const": "once"},
                            "local_datetime": {"type": "string"},
                            "timezone": {"type": "string"},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "hour", "minute"],
                        "properties": {
                            "type": {"const": "daily"},
                            "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                            "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                            "timezone": {"type": "string"},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "weekdays", "hour", "minute"],
                        "properties": {
                            "type": {"const": "weekly"},
                            "weekdays": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "integer", "minimum": 1, "maximum": 7},
                            },
                            "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                            "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                            "timezone": {"type": "string"},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "seconds"],
                        "properties": {
                            "type": {"const": "interval"},
                            "seconds": {
                                "type": "integer",
                                "minimum": 1,
                            },
                        },
                    },
                ]
            },
            "timezone": {"type": "string", "minLength": 1},
            "constraints": {
                "type": "array",
                "items": {"type": "string"},
            },
            "delivery": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["auto", "self_private", "current_group", "none"],
                        "description": (
                            "到时发消息用 current_group 或 self_private；none 要求静默内部处理；"
                            "auto 由创建时会话选择目标。SELF 没有私人目标。"
                        ),
                    },
                },
            },
        },
        "examples": [
            {
                "version": 1,
                "name": "五分钟后喝水提醒",
                "goal": "提醒我喝水",
                "trigger": {"type": "after", "seconds": 300},
                "constraints": [],
                "delivery": {"target": "auto"},
            }
        ],
    }


class AutomationToolService:
    """Expose global safe reads and owner-scoped mutations to the main Agent."""

    _ALLOWED_ARGUMENTS: ClassVar[dict[str, frozenset[str]]] = {
        "automation_query": frozenset(
            {"view", "match_task", "max_runs", "status", "limit", "cursor", "automation_id"}
        ),
        "automation_manage": frozenset({"action", "task", "max_runs", "automation_id"}),
        "time_get_current": frozenset(),
    }

    def __init__(self, service: AutomationService) -> None:
        self._service = service

    def definitions(self) -> tuple[ChatTool, ...]:
        identifier = {"type": "integer", "minimum": 1}
        task = _task_intent_schema()
        return (
            ChatTool(
                name="automation_query",
                result_cacheable=False,
                description=(
                    "查询自动化：list 为全局安全目录（默认 active），get 为单项安全摘要，"
                    "completed 为当前主体终态历史，history 为指定任务执行历史，"
                    "diagnose 为当前主体创建结果。目录可见不授予修改权限。"
                    "list 可用 match_task 精确查结构等价任务，无需先查询再创建。"
                ),
                parameters=_object_schema(
                    {
                        "view": {
                            "type": "string",
                            "enum": ["list", "get", "completed", "history", "diagnose"],
                        },
                        "automation_id": identifier,
                        "match_task": task,
                        "max_runs": identifier,
                        "status": {
                            "type": "string",
                            "enum": ["active", "paused", "current", "terminal", "all"],
                        },
                        "limit": {"type": "integer", "minimum": 1},
                        "cursor": {"type": "string"},
                    },
                    required=("view",),
                ),
            ),
            ChatTool(
                name="automation_manage",
                result_cacheable=False,
                description=(
                    "创建、更新、暂停、恢复、取消或立即调度自动化。create/update 提供完整 task；"
                    "其他操作用原 automation_id。按创建者当前权限执行，超级管理员可管理全部。"
                    "run_now 只确认入队。"
                )
                + _CREATE_DESCRIPTION,
                parameters=_object_schema(
                    {
                        "action": {
                            "type": "string",
                            "enum": ["create", "update", "pause", "resume", "cancel", "run_now"],
                        },
                        "automation_id": identifier,
                        "task": task,
                        "max_runs": identifier,
                    },
                    required=("action",),
                ),
            ),
            ChatTool(
                name="time_get_current",
                result_cacheable=False,
                description="读取可信当前 UTC、本地时间、日期和星期。",
                parameters=_object_schema({}),
            ),
        )

    async def execute(
        self, name: str, arguments_json: str, runtime: ToolRuntime
    ) -> ToolExecutionResult:
        if not self._valid_runtime(runtime):
            return _result(error="permission_context_mismatch", detail="自动化工具缺少可信执行主体")
        actor = runtime.require_actor()
        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            return _result(error="invalid_arguments", detail=str(exc))
        allowed = self._ALLOWED_ARGUMENTS.get(name)
        if allowed is None:
            return _result(error="unknown_tool", detail=f"未知自动化工具：{name}")
        operation = (
            "time_get_current"
            if name == "time_get_current"
            else arguments.get("view" if name == "automation_query" else "action")
        )
        operations = (
            {"list", "get", "completed", "history", "diagnose"}
            if name == "automation_query"
            else {"create", "update", "pause", "resume", "cancel", "run_now"}
            if name == "automation_manage"
            else {"time_get_current"}
        )
        if not isinstance(operation, str) or operation not in operations:
            return _result(error="invalid_arguments", detail="未知 action/view")
        unexpected = set(arguments) - allowed
        if unexpected:
            error = ValueError(f"不接受参数：{', '.join(sorted(unexpected))}")
            if operation == "create":
                await self._service.record_creation_failure(
                    actor=actor,
                    conversation_key=runtime.conversation_key,
                    error=error,
                )
            return _result(error="invalid_arguments", detail=str(error))
        try:
            if operation == "create":
                row = await self._service.create_task(
                    arguments.get("task"),
                    actor=actor,
                    conversation_key=runtime.conversation_key,
                    max_runs=arguments.get("max_runs"),
                )
                return _result(
                    data=_record(row, persisted=True),
                    public_message=f"自动化任务已创建（ID {row.id}）。",
                    mutation_committed=True,
                )
            if operation == "list":
                maximum = arguments.get("limit", 50)
                if isinstance(maximum, bool) or not isinstance(maximum, int):
                    raise ValueError("limit 必须是整数")
                cursor = arguments.get("cursor") or "0"
                if not isinstance(cursor, str) or not cursor.isdigit():
                    raise ValueError("cursor 必须是非负整数字符串")
                offset = int(cursor)
                matched = arguments.get("match_task") is not None
                if matched:
                    automations = await self._service.find_equivalent_directory_entries(
                        arguments["match_task"],
                        actor=actor,
                        max_runs=arguments.get("max_runs"),
                    )
                    page = automations[offset : offset + maximum]
                    has_more = offset + maximum < len(automations)
                else:
                    fetched = await self._service.list_directory(
                        status=str(arguments.get("status") or "active"),
                        limit=maximum + 1,
                        offset=offset,
                    )
                    page = fetched[:maximum]
                    has_more = len(fetched) > maximum
                return _result(
                    data={
                        "default_status": "active",
                        "tasks": [_directory_record(entry) for entry in page],
                        "next_cursor": str(offset + maximum) if has_more else None,
                    }
                )
            if operation == "completed":
                maximum = arguments.get("limit", 50)
                if isinstance(maximum, bool) or not isinstance(maximum, int):
                    raise ValueError("limit 必须是整数")
                completed = await self._service.list_completed(actor.person_id or "")
                return _result(
                    data={
                        "completed_history": [_record(row) for row in completed[:maximum]],
                    }
                )
            if operation == "time_get_current":
                return _result(data=self._service.current_time())
            if operation == "diagnose":
                return _result(
                    data={
                        "recent_creation_outcomes": await self._service.diagnose_creation(
                            actor.person_id or ""
                        )
                    }
                )
            automation_id = _automation_id(arguments)
            if operation == "get":
                return _result(
                    data=_directory_record(await self._service.get_visible(automation_id))
                )
            if operation == "update":
                row = await self._service.update_task(
                    automation_id,
                    arguments.get("task"),
                    actor=actor,
                    conversation_key=runtime.conversation_key,
                )
                return _result(
                    data=_record(row, persisted=True),
                    public_message=f"自动化任务已更新（ID {row.id}）。",
                    mutation_committed=True,
                )
            if operation == "pause":
                changed = await self._service.pause(
                    automation_id, actor=actor, conversation_key=runtime.conversation_key
                )
            elif operation == "resume":
                changed = await self._service.resume(
                    automation_id, actor=actor, conversation_key=runtime.conversation_key
                )
            elif operation == "cancel":
                changed = await self._service.cancel(
                    automation_id, actor=actor, conversation_key=runtime.conversation_key
                )
            elif operation == "run_now":
                changed = await self._service.run_now(
                    automation_id, actor=actor, conversation_key=runtime.conversation_key
                )
            elif operation == "history":
                task, history_rows = await self._service.history(automation_id, actor=actor)
                return _result(
                    data={
                        "runs": [
                            {
                                "id": row.id,
                                "status": row.status.value,
                                "scheduled_for_local": local_iso(row.scheduled_for, task.timezone),
                                "finished_at_local": local_iso(row.finished_at, task.timezone),
                                "timezone": task.timezone,
                                "error_category": row.error_category,
                            }
                            for row in history_rows
                        ]
                    }
                )
            else:
                return _result(error="unknown_tool", detail=f"未知自动化工具：{name}")
            public_message = {
                "pause": "任务已暂停。" if changed else "任务状态没有改变。",
                "resume": "任务已恢复。" if changed else "该任务不能恢复。",
                "cancel": "任务已取消。" if changed else "任务状态没有改变。",
                "run_now": ("任务已进入待执行队列。" if changed else "该任务不能立即执行。"),
            }[operation]
            satisfied: bool | None = None
            if operation == "cancel":
                # A repeated cancel of an already cancelled task meets its request.
                satisfied = changed or (
                    (await self._service.require_manageable(automation_id, actor)).status
                    is AutomationStatus.CANCELLED
                )
            elif operation == "resume":
                satisfied = changed
            return replace(
                _result(
                    data={"automation_id": automation_id, "changed": changed},
                    public_message=public_message,
                    mutation_committed=changed,
                ),
                request_postcondition_satisfied=satisfied,
            )
        except (PermissionError, ValueError) as exc:
            if write_failure_phase(exc) is not None and write_failure_committed(exc) is not False:
                raise
            if operation == "create":
                await self._service.record_creation_failure(
                    actor=actor,
                    conversation_key=runtime.conversation_key,
                    error=exc,
                )
            return _result(error=type(exc).__name__, detail=str(exc))

    @staticmethod
    def _valid_runtime(runtime: ToolRuntime) -> bool:
        try:
            actor = runtime.require_actor()
            _ = actor.source_key
        except (PermissionError, ValueError):
            return False
        return True


def _automation_id(arguments: dict[str, Any]) -> int:
    value = arguments.get("automation_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("automation_id 必须是正整数")
    return value


def _record(
    row: AutomationRecord,
    *,
    persisted: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "automation_id": row.id,
        "name": row.name,
        "status": row.status.value,
        "timezone": row.timezone,
        "schedule": row.schedule.model_dump(mode="json"),
        "next_run_at_local": local_iso(row.next_run_at, row.timezone),
        "run_count": row.run_count,
    }
    if persisted:
        payload["confirmation"] = "persisted"
    return payload


def _directory_record(entry: AutomationDirectoryEntry) -> dict[str, Any]:
    row = entry.record
    creator = entry.creator.model_dump(mode="json")
    if creator["kind"] == "person":
        creator.pop("kind")
    payload: dict[str, Any] = {
        "automation_id": row.id,
        "task": row.name,
        "next_run_at_local": local_iso(row.next_run_at, row.timezone),
        "creator": creator,
    }
    if row.status.value != "active":
        payload["status"] = row.status.value
    return payload


def _result(
    *,
    data: object = None,
    error: str | None = None,
    detail: str = "",
    public_message: str | None = None,
    mutation_committed: bool | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=error is None,
        data=data,
        error_code=error,
        public_message=public_message or detail or None,
        mutation_committed=mutation_committed if error is None else False,
    )
