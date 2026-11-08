"""Parent/child tools shared by every Work execution."""

from __future__ import annotations

import json
from typing import Any

from yuki.domain.messages import ChatTool
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_queries import WorkQueries


def subagent_tools() -> tuple[ChatTool, ...]:
    string = {"type": "string"}
    return (
        ChatTool(
            name="start_subtask",
            description=(
                "把一项独立工作交给后台子任务，立即返回子任务 id；当前任务可继续处理。"
                "goal 写清目标，context 给必要资料，acceptance 写产物要求，files 列出相关文件。"
                "子任务结果回到当前任务，不会自动发给用户。"
            ),
            result_cacheable=False,
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "minLength": 1},
                    "context": string,
                    "acceptance": string,
                    "files": {"type": "array", "items": string},
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
        ),
        ChatTool(
            name="message_task",
            description=(
                "给子任务补充要求，或在子任务中联系父任务（省略 id）。"
                "ask=true 表示提问并等待父任务回答；reply_to 填所回答问题的 message_id。"
            ),
            result_cacheable=False,
            parameters={
                "type": "object",
                "properties": {
                    "id": {**string, "description": "子任务 id；省略时发给父任务"},
                    "text": {"type": "string", "minLength": 1},
                    "ask": {"type": "boolean"},
                    "reply_to": string,
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        ),
    )


async def start_subtask(control: Any, args: dict[str, Any], key: str) -> dict[str, Any]:
    if control.current is None:
        raise ValueError("subtask_requires_current_task")
    brief = {
        name: args[name] for name in ("goal", "context", "acceptance", "files") if name in args
    }
    spawned_id = await SubagentRepository(control.repository).start(
        control.lease, control.current["id"], key, brief
    )
    return {"status": "running", "id": spawned_id}


async def message_task(control: Any, args: dict[str, Any], key: str) -> dict[str, Any]:
    identity = args.get("id")
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("message_text_required")
    to_parent = identity is None
    if to_parent:
        if control.current is None or control.current["parent_work_id"] is None:
            raise ValueError("subtask_parent_unavailable")
        identity = control.current["id"]
        parent_id = control.current["parent_work_id"]
    else:
        parent_id = await owned_child_parent(control, identity)
    ask = args.get("ask", False)
    reply_to = args.get("reply_to")
    if type(ask) is not bool or (reply_to is not None and not isinstance(reply_to, str)):
        raise ValueError("invalid_message_arguments")
    receipt = await SubagentRepository(control.repository).message(
        control.lease,
        parent_id,
        str(identity),
        key,
        text,
        ask=ask,
        reply_to=reply_to,
        to_parent=to_parent,
    )
    if receipt["waiting_parent"]:
        await control._accept("need_input", key, reason=text)
        return {"message_id": receipt["message_id"], "status": "waiting_parent"}
    return {"message_id": receipt["message_id"]}


async def owned_child_parent(control: Any, identity: Any) -> str:
    """The parent through which this execution owns child ``identity``."""
    if not isinstance(identity, str) or not identity:
        raise ValueError("task_id_required")
    child = await WorkQueries(control.repository).get(
        control.lease, control.source, identity, local=True
    )
    if child is None or child["parent_work_id"] is None:
        raise ValueError("subtask_not_owned")
    return str(control.lease.work_id or child["parent_work_id"])


async def manage_subtask(
    control: Any, action: str, identity: str, key: str, text: Any
) -> dict[str, Any]:
    """Cancel or resume (optionally with new requirements) an owned child Work."""
    parent_id = await owned_child_parent(control, identity)
    repository = SubagentRepository(control.repository)
    if action == "cancel":
        await repository.cancel(control.lease, parent_id, identity)
    else:
        if text is not None and not isinstance(text, str):
            raise ValueError("invalid_task_text")
        await repository.resume(control.lease, parent_id, identity, key=key, instruction=text)
    row = await repository.related(parent_id, identity)
    return {"id": identity, "status": child_state(row)}


async def subtask_result(control: Any, identity: str) -> dict[str, Any] | None:
    """The committed result of an owned child, as subagent results were read."""
    try:
        parent_id = await owned_child_parent(control, identity)
        row = await SubagentRepository(control.repository).related(
            parent_id, identity, include_checkpoint=True
        )
    except ValueError:
        return None
    result = {k: v for k, v in json.loads(row["result_json"]).items() if k != "cache_samples"}
    if row["archived_at"]:
        result["archived"] = True
    return result or None


def child_state(row: dict[str, Any]) -> str:
    if row["archived_at"]:
        return "archived"
    return "waiting_parent" if row["state"] == "waiting_user" else str(row["state"])
