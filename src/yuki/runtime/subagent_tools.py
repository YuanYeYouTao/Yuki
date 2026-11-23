"""Parent/child tools shared by every Work execution."""

from __future__ import annotations

import json
from typing import Any

from yuki.domain.messages import ChatTool
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_queries import WorkQueries


def subagent_tools() -> tuple[ChatTool, ...]:
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    return (
        ChatTool(
            name="start_subtask",
            description=(
                "把一项独立工作交给后台子任务，立即返回子任务 id；当前任务可继续处理。"
                "goal 写清目标，context 给必要资料，acceptance 写产物要求，files 列出相关文件。"
                "省略 executor 时由你自己的子任务执行；给 executor 时交给本人电脑上的编码 "
                "Agent（agent 用 get_tasks 的 agents 目录中的 ID），session 指定已有会话，"
                "cwd 是新会话的目录。scope 在委派时固定范围：write_paths 可写目录、network "
                '是否联网、commands 无需确认的命令前缀（"*" 为全部命令）。命令去掉首尾空白后，'
                '须等于某个前缀或以“前缀加一个空格”开头（"git log" 匹配 "git log -5"，不匹配 '
                '"git logx"、"git push"，前缀后跟制表符也不算）；去掉首尾空白后仍含 '
                "; & | < > $ ` ( ) 、换行或回车的（引号内、2>&1 也算），只有 \"*\" 才算范围内。"
                "命令前缀判断不检查命令会写哪些路径，write_paths 约束不了已批准的命令：Claude "
                "的命令批准后以本人权限运行，连接器不加沙箱；Codex 沙箱内的命令本来不询问，批准"
                "只是同意那一次请求，不保证命令执行或越出沙箱；ACP Agent 自己执行的命令看 Agent "
                "自己的沙箱，连接器代开的终端由连接器以本人权限执行。省略 scope 即完全访问 "
                '{"write_paths": ["/"], "network": true, "commands": ["*"]}，本机 Agent 以'
                "原生完全访问模式运行，只在用户要求限制时给更小的范围。范围内的请求通常由连接器"
                "自动批准（ACP Agent 没给一次性允许选项时仍作为卡片回来）；范围外的请求作为卡片"
                "回到当前任务（ACP 连接器代开的终端和文件写入除外：范围外直接拒绝，不出卡片），"
                "由你批准、拒绝或问用户，批准只对那一个请求有效。"
                "relationship 默认 delegate；observe 只看用户自己的会话：绝不发送输入、绝不"
                "回答或拒绝它的请求，只在需要时提醒用户，不需要当前任务，notify_turn_end "
                "仅在用户说做完告诉他时设为 true；takeover 接手用户已有的会话，只在用户明确"
                "要求时使用。会话在别处活着时会被拒绝（session_live_elsewhere），用户同意后可用 "
                "fork=true 从它分叉出新会话，原会话不动。子任务结果回到当前任务，不会自动发给用户。"
            ),
            result_cacheable=False,
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "minLength": 1},
                    "context": string,
                    "acceptance": string,
                    "files": strings,
                    "executor": {
                        "type": "object",
                        "properties": {"device": string, "agent": string},
                        "required": ["agent"],
                        "additionalProperties": False,
                    },
                    "session": string,
                    "cwd": string,
                    "relationship": {
                        "type": "string",
                        "enum": ["delegate", "observe", "takeover"],
                    },
                    "scope": {
                        "type": "object",
                        "properties": {
                            "write_paths": strings,
                            "network": {"type": "boolean"},
                            "commands": strings,
                        },
                        "additionalProperties": False,
                    },
                    "notify_turn_end": {"type": "boolean"},
                    "fork": {"type": "boolean"},
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
                "对编码 Agent 子任务：text 追加要求，mode 默认 queue（排入原生队列，空闲时开"
                "新回合），steer 追加到运行中的回合；回答它的权限请求或提问时 reply_to 填卡片"
                "里的 permission_id，answer 填所选选项 ID 或原生回答对象，text 写理由。"
                "观察关系不能发消息或回答。"
            ),
            result_cacheable=False,
            parameters={
                "type": "object",
                "properties": {
                    "id": {**string, "description": "子任务 id；省略时发给父任务"},
                    "text": {"type": "string", "minLength": 1},
                    "ask": {"type": "boolean"},
                    "reply_to": string,
                    "answer": {"type": ["string", "object"]},
                    "mode": {"type": "string", "enum": ["queue", "steer"]},
                },
                "additionalProperties": False,
            },
        ),
    )


async def start_subtask(control: Any, args: dict[str, Any], key: str) -> dict[str, Any]:
    if "executor" in args or args.get("relationship", "delegate") != "delegate":
        from yuki.external_agents.subtasks import start_external

        return await start_external(control, args, key)
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
    if "id" in args:
        from yuki.external_agents.subtasks import message_external, record_executor

        executor = await record_executor(control, args["id"])
        if executor is not None:
            from yuki.external_agents.subtasks import authorize_record

            await authorize_record(control, args["id"])
            return await message_external(control, args, key, args["id"], executor)
    if "text" not in args:
        raise ValueError("invalid_subagent_message")
    text = args["text"]
    to_parent = "id" not in args
    if to_parent:
        if control.current is None or control.current["parent_work_id"] is None:
            raise ValueError("subtask_parent_unavailable")
        identity = control.current["id"]
        parent_id = control.current["parent_work_id"]
    else:
        identity = args["id"]
        parent_id = await owned_child_parent(control, identity)
    receipt = await SubagentRepository(control.repository).message(
        control.lease,
        parent_id,
        identity,
        key,
        text,
        ask=args.get("ask", False),
        reply_to=args.get("reply_to"),
        to_parent=to_parent,
    )
    if receipt["waiting_parent"]:
        await control._accept("need_input", key, reason=text)
        return {"message_id": receipt["message_id"], "status": "waiting_parent"}
    return {"message_id": receipt["message_id"]}


async def owned_child_parent(control: Any, identity: str) -> str:
    """The parent through which this execution owns child ``identity``."""
    child = await WorkQueries(control.repository).get(
        control.lease, control.source, identity, local=True
    )
    if child is None or child["parent_work_id"] is None:
        raise ValueError("subtask_not_owned")
    return str(control.lease.work_id or child["parent_work_id"])


async def manage_subtask(
    control: Any,
    action: str,
    identity: str,
    key: str,
    text: str | None,
    *,
    revision: int | None = None,
) -> dict[str, Any]:
    """Cancel or resume (optionally with new requirements) an owned child Work.

    Both expect ``revision`` when the caller read one, else act on the current one.
    """
    from yuki.runtime.work_management import WorkManagementError

    parent_id = await owned_child_parent(control, identity)
    repository = SubagentRepository(control.repository)
    try:
        if action == "cancel":
            await repository.cancel(control.lease, parent_id, identity, revision=revision)
        else:
            await repository.resume(
                control.lease, parent_id, identity, key=key, instruction=text, revision=revision
            )
    except WorkManagementError as exc:
        raise ValueError(exc.code) from exc
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
