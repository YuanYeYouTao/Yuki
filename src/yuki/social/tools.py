"""Deployment-stable social schemas; execution lives in the social service."""

from typing import Any

from yuki.domain.messages import ChatTool

_ID = {"type": "string", "format": "uuid"}


def target_schema(*keys: str, description: str) -> dict[str, Any]:
    """One internal selector: exactly one of ``keys``."""
    return {
        "type": "object",
        "description": description,
        "properties": {key: _ID for key in keys},
        "minProperties": 1,
        "maxProperties": 1,
        "additionalProperties": False,
    }


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    namespace: str,
    effect: str,
    required: tuple[str, ...] = (),
    result_cacheable: bool = True,
) -> ChatTool:
    return ChatTool(
        name=name,
        namespace=namespace,
        effect=effect,
        result_cacheable=result_cacheable,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    )


def social_tool_definitions() -> tuple[ChatTool, ...]:
    cursor = {"type": "string"}
    limit = {"type": "integer", "minimum": 1}
    return (
        _tool(
            "send",
            "向当前会话或指定目标发送文字、一个文件或表情；只写最终正文不会送达。"
            "text 中换行由程序按分条规则发送；正文原位置写 @名字即可提及，"
            "指定账号复制成员目录的 @{account:UUID}。"
            "结果 status 为 unknown 或部分分条已成功时不要重发。",
            {
                "text": {"type": "string"},
                "file": {
                    "type": "object",
                    "description": "要发送的一个文件；多个文件分多次调用",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "/workspace、/inputs/media-cache 或 device 所指电脑上的路径"
                            ),
                        },
                        "artifact_id": {"type": "string", "description": "已有文件快照 ID"},
                        "device": {"type": "string", "description": "path 所在的本人电脑"},
                        "name": {"type": "string", "description": "送达时的文件名"},
                    },
                    "minProperties": 1,
                    "additionalProperties": False,
                },
                "as_file": {
                    "type": "boolean",
                    "description": "图片默认显示为聊天图片；true 按原文件发送",
                },
                "sticker": {
                    "type": "object",
                    "description": "后台配一张表情，goal 写想表达的含义；可不带 text",
                    "properties": {"goal": {"type": "string"}, "emotion": {"type": "string"}},
                    "required": ["goal"],
                    "additionalProperties": False,
                },
                "reply_to": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "引用回复的内部事件 ID",
                },
                "target": target_schema(
                    "person_id",
                    "space_id",
                    "conversation_id",
                    "binding_id",
                    description="省略为当前会话；发给其他人或群时给一个内部 ID",
                ),
            },
            namespace="social.send",
            effect="platform_send",
        ),
        _tool(
            "recall_message",
            "撤回自己发送的一条消息；只能由原发送账号撤回。",
            {"event_id": {"type": "integer", "minimum": 1}},
            namespace="social.recall",
            effect="platform_mutate",
            required=("event_id",),
        ),
        _tool(
            "read_chat",
            "读取已入账的聊天记录。省略 target 且不带筛选时读当前会话；"
            "带 query、author、has、platform、scope、since 或 until 时检索所有获准的聊天。"
            "around 按事件 ID 读取前后各 context 条邻文，此时不接受其他筛选。"
            "since/until 为带时区的时间，until 不含。只读已入账内容，不接受平台消息号。",
            {
                "target": target_schema(
                    "person_id",
                    "space_id",
                    "conversation_id",
                    "binding_id",
                    "operation_id",
                    description="一个内部 ID；operation_id 定位一次发送回执",
                ),
                "query": {"type": "string", "description": "原文或语音转写中的关键词"},
                "author": {"type": "string", "description": "作者的 Person ID，或 yuki"},
                "has": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["image", "video", "file", "audio"]},
                },
                "platform": {"type": "string"},
                "scope": {"type": "string", "enum": ["private", "group"]},
                "order": {"type": "string", "enum": ["recent", "oldest"]},
                "since": {"type": "string", "description": "带时区 ISO-8601，含"},
                "until": {"type": "string", "description": "带时区 ISO-8601，不含"},
                "around": {"type": "integer", "minimum": 1, "description": "锚点事件 ID"},
                "context": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "每条结果前后各附带的邻文条数",
                },
                "limit": limit,
                "cursor": cursor,
            },
            namespace="social.history",
            effect="read_state",
            result_cacheable=False,
        ),
        _tool(
            "find_contacts",
            "查找人物或群，返回其当前可联系入口与历史会话。name 按名称包含查询；"
            "已知内部 ID 用 target；同名返回多个候选，不猜目标。",
            {
                "name": {"type": "string", "maxLength": 128},
                "target": target_schema(
                    "person_id",
                    "space_id",
                    "conversation_id",
                    "binding_id",
                    description="一个内部 ID",
                ),
                "type": {"type": "string", "enum": ["person", "group"]},
                "platform": {"type": "string"},
                "limit": limit,
                "cursor": cursor,
            },
            namespace="social.contacts",
            effect="read_state",
        ),
        _tool(
            "get_members",
            "分页读取群里已观察到的成员账号目录；不代表平台完整名册。",
            {
                "target": target_schema(
                    "space_id",
                    "binding_id",
                    "conversation_id",
                    description="群的一个内部 ID；当前群可省略",
                ),
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": cursor,
            },
            namespace="social.members",
            effect="external_read",
        ),
    )


SOCIAL_TOOL_NAMES = frozenset(tool.name for tool in social_tool_definitions())
