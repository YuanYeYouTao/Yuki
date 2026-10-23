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
    "创建持久化安排。goal 保存完整要求与真实收件人线索，到期由同一个主 Agent 执行。"
    "运行时使用主 Agent 的完整工具，按创建者当前权限执行，无需选择工具或预算。"
    "是否需要沟通由完整要求决定，实际消息使用正常 send 发送。"
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
        "required": ["goal", "trigger"],
        "description": "完整的主 Agent 任务要求与时间安排。",
        "properties": {
            "name": {
                "type": ["string", "null"],
                "minLength": 1,
                "description": "可选公开显示标签；不要从私有要求生成标签。",
            },
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
                            "local_datetime": {
                                "type": "string",
                                "format": "date-time",
                                "description": "包含 UTC offset 的绝对时刻。",
                            },
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
        },
        "examples": [
            {
                "name": "五分钟后喝水提醒",
                "goal": "提醒我喝水",
                "trigger": {"type": "after", "seconds": 300},
            }
        ],
    }


class AutomationToolService:
    """Expose global safe reads and owner-scoped mutations to the main Agent."""

    _ALLOWED_ARGUMENTS: ClassVar[dict[str, dict[str, frozenset[str]]]] = {
        "automation_query": {
            "list": frozenset({"view", "status", "limit", "cursor"}),
            "get": frozenset({"view", "automation_id"}),
            "history": frozenset({"view", "automation_id", "limit", "cursor"}),
        },
        "automation_manage": {
            "create": frozenset({"action", "task", "max_runs"}),
            "update": frozenset({"action", "automation_id", "task", "max_runs"}),
            **{
                action: frozenset({"action", "automation_id"})
                for action in ("pause", "resume", "cancel", "run_now")
            },
        },
        "time_get_current": {"time_get_current": frozenset()},
    }

    def __init__(self, service: AutomationService) -> None:
        self._service = service

    def definitions(self) -> tuple[ChatTool, ...]:
        identifier = {"type": "integer", "minimum": 1}
        task = _task_intent_schema()
        maximum = {"type": "integer", "minimum": 1}
        cursor = {"type": "string", "pattern": "^[0-9]+$"}
        runs = {
            "type": ["integer", "null"],
            "minimum": 1,
            "description": "累计发生次数上限；create 缺省不限，update 缺省保留，null 解除限制。",
        }
        return (
            ChatTool(
                name="automation_query",
                schema_version="2",
                result_cacheable=False,
                description=(
                    "查询自动化。list 为全局安全摘要（默认 active）；"
                    "get 读取本人或超管获准的完整定义；history 读取获准的原发生与结果。"
                    "目录可见不授予私有读取或修改权限。"
                ),
                parameters={
                    "type": "object",
                    "oneOf": [
                        _object_schema(
                            {
                                "view": {"const": "list"},
                                "status": {
                                    "type": "string",
                                    "enum": [status.value for status in AutomationStatus] + ["all"],
                                },
                                "limit": maximum,
                                "cursor": cursor,
                            },
                            required=("view",),
                        ),
                        _object_schema(
                            {"view": {"const": "get"}, "automation_id": identifier},
                            required=("view", "automation_id"),
                        ),
                        _object_schema(
                            {
                                "view": {"const": "history"},
                                "automation_id": identifier,
                                "limit": maximum,
                                "cursor": cursor,
                            },
                            required=("view", "automation_id"),
                        ),
                    ],
                },
            ),
            ChatTool(
                name="automation_manage",
                schema_version="2",
                result_cacheable=False,
                description=(
                    "创建、更新、暂停、恢复、取消或立即调度自动化。"
                    "create/update 提交完整 goal/trigger。"
                    "update 中 name 缺省保留，null 清除公开标签；"
                    "max_runs 缺省保留，null 解除次数限制。"
                    "按真实 owner/超管权限管理；run_now 只确认入队。"
                )
                + _CREATE_DESCRIPTION,
                parameters={
                    **_object_schema(
                        {
                            "action": {
                                "type": "string",
                                "enum": list(self._ALLOWED_ARGUMENTS["automation_manage"]),
                            },
                            "automation_id": identifier,
                            "task": task,
                            "max_runs": runs,
                        },
                        required=("action",),
                    ),
                    "oneOf": [
                        _object_schema(
                            {"action": {"const": "create"}, "task": {}, "max_runs": {}},
                            required=("action", "task"),
                        ),
                        _object_schema(
                            {
                                "action": {"const": "update"},
                                "automation_id": {},
                                "task": {},
                                "max_runs": {},
                            },
                            required=("action", "automation_id", "task"),
                        ),
                        *[
                            _object_schema(
                                {"action": {"const": action}, "automation_id": {}},
                                required=("action", "automation_id"),
                            )
                            for action in ("pause", "resume", "cancel", "run_now")
                        ],
                    ],
                },
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
        operations = self._ALLOWED_ARGUMENTS.get(name)
        if operations is None:
            return _result(error="unknown_tool", detail=f"未知自动化工具：{name}")
        operation = (
            "time_get_current"
            if name == "time_get_current"
            else arguments.get("view" if name == "automation_query" else "action")
        )
        if not isinstance(operation, str) or operation not in operations:
            return _result(error="invalid_arguments", detail="未知 action/view")
        allowed = operations[operation]
        unexpected = set(arguments) - allowed
        if unexpected:
            error = ValueError(f"不接受参数：{', '.join(sorted(unexpected))}")
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
                maximum, offset = _pagination(arguments)
                fetched = await self._service.list_directory(
                    status=str(arguments.get("status") or "active"),
                    limit=maximum + 1,
                    offset=offset,
                )
                return _result(
                    data={
                        "tasks": [_directory_record(entry) for entry in fetched[:maximum]],
                        "next_cursor": str(offset + maximum) if len(fetched) > maximum else None,
                    }
                )
            if operation == "time_get_current":
                return _result(data=self._service.current_time())
            automation_id = _automation_id(arguments)
            if operation == "get":
                row = await self._service.require_manageable(automation_id, actor)
                return _result(data={**_record(row), "task": row.script})
            if operation == "update":
                row = await self._service.update_task(
                    automation_id,
                    arguments.get("task"),
                    actor=actor,
                    conversation_key=runtime.conversation_key,
                    **({"max_runs": arguments["max_runs"]} if "max_runs" in arguments else {}),
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
                maximum, offset = _pagination(arguments)
                task, history_fetched = await self._service.history(
                    automation_id, actor=actor, limit=maximum + 1, offset=offset
                )
                history_rows = history_fetched[:maximum]
                return _result(
                    data={
                        "runs": [
                            {
                                "id": row.id,
                                "status": row.status.value,
                                "version_id": row.version_id,
                                "definition": row.definition,
                                "result": row.result_summary,
                                "delivery": row.delivery,
                                "legacy_messages_sent": row.messages_sent,
                                "scheduled_for_local": local_iso(row.scheduled_for, task.timezone),
                                "finished_at_local": local_iso(row.finished_at, task.timezone),
                                "timezone": task.timezone,
                                "error_category": row.error_category,
                            }
                            for row in history_rows
                        ],
                        "next_cursor": str(offset + maximum)
                        if len(history_fetched) > maximum
                        else None,
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
            return _result(error=type(exc).__name__, detail=str(exc))

    @staticmethod
    def _valid_runtime(runtime: ToolRuntime) -> bool:
        try:
            actor = runtime.require_actor()
            _ = actor.source_key
        except (PermissionError, ValueError):
            return False
        return True


def _pagination(arguments: dict[str, Any]) -> tuple[int, int]:
    maximum = arguments.get("limit", 50)
    if type(maximum) is not int or maximum < 1:
        raise ValueError("limit 必须是正整数")
    cursor = arguments.get("cursor", "0")
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise ValueError("cursor 必须是非负整数字符串")
    return maximum, int(cursor)


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
        "max_runs": row.max_runs,
        "version_id": row.version_id,
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
        "status": row.status.value,
    }
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
