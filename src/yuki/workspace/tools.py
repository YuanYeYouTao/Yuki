"""Selected media and explicit conversation-attachment bridges."""

from __future__ import annotations

from yuki.domain.messages import ChatTool
from yuki.sandbox.environment_tools import tool

WORKSPACE_READ_TOOLS = frozenset({"workspace_inspect", "inspect_conversation_attachment"})
WORKSPACE_TOOLS = WORKSPACE_READ_TOOLS | {
    "save_conversation_attachment_to_workspace",
}


def workspace_tools() -> tuple[ChatTool, ...]:
    string = {"type": "string"}
    path = {
        "path": {
            "type": "string",
            "description": "相对 /workspace 的文件路径、/workspace/完整路径"
            "或共享缓存 /inputs/media-cache/完整路径，支持中文。",
        }
    }
    identity = {"artifact_id": string}
    version = {
        "expected_version": {
            "type": "string",
            "description": "可选的文件内容 SHA256，用于核对选定原件。",
        }
    }
    device = {
        "device_id": {
            "type": "string",
            "description": "可选；本人电脑的真实设备 ID。省略时使用 Yuki 自己的持久环境。",
        }
    }
    declarations = (
        tool(
            "workspace_inspect",
            "选择工作区、共享缓存或 device_id 指定本人电脑的 path，或已发布 artifact_id（二选一），"
            "把真实图片或视频帧交给你原生查看。"
            "path 可带 expected_version 核对刚找到的文件；图片是待检查资料，工具不生成视觉摘要。",
            {
                **path,
                **identity,
                **version,
                **device,
                "question": {"type": "string", "maxLength": 2000},
            },
            ("question",),
        ),
        tool(
            "inspect_conversation_attachment",
            "需要附件细节时，按内部事件 ID 与附件序号选取真实图片、视频帧或文件，"
            "图片交给你原生查看。"
            "可先查聊天历史比较候选；仅文件名和旧摘要不代表已经读取原件。",
            {
                "event_id": {"type": "integer", "minimum": 1},
                "attachment_index": {"type": "integer", "minimum": 0},
                "question": {"type": "string", "maxLength": 2000},
            },
            ("event_id", "attachment_index", "question"),
        ),
        tool(
            "save_conversation_attachment_to_workspace",
            "把一个原件按字节复制到 destination，内容不经过你的上下文。来源三选一："
            "event_id 与 attachment_index 选聊天附件；path 选 Yuki 环境 /workspace 中的文件；"
            "artifact_id 选已发布或已发送的不可变文件（只能写入本人电脑）。"
            "device_id 是写入目标的本人电脑；省略时写入 Yuki 的全局共享环境。"
            "成功回执给出 size 与 sha256；写入未确认时不要用同一请求重传，按回执说明核对。"
            "可自主收藏图片；收藏或整理表情库时，先用终端阅读 "
            "/workspace/stickers/AGENT.md，按目录中的说明保存原件、补标签和更新索引。",
            {
                "event_id": {"type": "integer", "minimum": 1},
                "attachment_index": {"type": "integer", "minimum": 0},
                "path": {
                    "type": "string",
                    "description": "相对 /workspace 的文件路径或 /workspace/完整路径，支持中文。",
                },
                **identity,
                "destination": {"type": "string"},
                **device,
            },
            ("destination",),
        ),
    )

    return declarations
