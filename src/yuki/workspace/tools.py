"""Original files seen natively or copied byte-for-byte."""

from __future__ import annotations

from typing import Any

from yuki.domain.messages import ChatTool
from yuki.sandbox.environment_tools import tool

WORKSPACE_READ_TOOLS = frozenset({"view_file"})
WORKSPACE_TOOLS = WORKSPACE_READ_TOOLS | {"copy_file"}


def file_source(args: dict[str, Any]) -> dict[str, Any]:
    """The one selected source; any other combination is ``choose_one_source``."""
    from yuki.workspace.store import WorkspaceError

    chosen = [
        key
        for key, present in (
            ("path", "path" in args),
            ("artifact_id", "artifact_id" in args),
            ("attachment", "event_id" in args or "attachment" in args),
        )
        if present
    ]
    if (
        len(chosen) != 1
        or ("event_id" in args) != ("attachment" in args)
        or ("device" in args and chosen != ["path"])
    ):
        raise WorkspaceError("choose_one_source")
    return {
        key: args[key]
        for key in ("path", "artifact_id", "event_id", "attachment", "device")
        if key in args
    }


def workspace_tools() -> tuple[ChatTool, ...]:
    source = {
        "path": {
            "type": "string",
            "description": "文件路径：/workspace 下的路径（可写相对路径）、"
            "/inputs/media-cache/ 下的原件，或 device 所指电脑上的路径",
        },
        "device": {
            "type": "string",
            "description": "path 所在的本人电脑设备 ID；省略为 Yuki 自己的环境",
        },
        "artifact_id": {"type": "string", "description": "已发布或已发送文件的 ID，不是 URL"},
        "event_id": {"type": "integer", "minimum": 1, "description": "聊天消息的事件 ID"},
        "attachment": {
            "type": "integer",
            "minimum": 0,
            "description": "该消息中的附件序号，从 0 开始",
        },
    }
    return (
        tool(
            "view_file",
            "直接查看一张图片或视频的画面。来源三选一：path（可带 device）、artifact_id，"
            "或 event_id 加 attachment 选聊天附件；question 写想看清什么。"
            "文件名、标签和旧摘要都不等于看过原件。",
            {**source, "question": {"type": "string", "maxLength": 2000}},
            namespace="workspace.read",
            read=True,
        ),
        tool(
            "copy_file",
            "把一个文件按字节复制到 destination，内容不经过你的上下文。来源三选一："
            "path（Yuki 环境中的文件）、artifact_id，或 event_id 加 attachment；"
            "to_device 是目标本人电脑，省略写入 Yuki 环境。成功返回 size 与 sha256；"
            "结果未知时不要重复复制，按提示核对。整理表情库前先读 /workspace/stickers/AGENT.md。",
            {
                **source,
                "destination": {"type": "string"},
                "to_device": {"type": "string", "description": "目标本人电脑的设备 ID"},
            },
            ("destination",),
            namespace="workspace.write",
        ),
    )
