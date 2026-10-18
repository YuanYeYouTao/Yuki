"""Deployment-stable social schemas; execution lives in the social service."""

from yuki.domain.messages import ChatTool


def social_tool_definitions() -> tuple[ChatTool, ...]:
    selector: dict[str, object] = {
        "target_id": {"type": "string", "description": "find_contacts 返回的 canonical ID"},
        "display_name": {"type": "string", "maxLength": 128},
        "subject_ref": {
            "type": "string",
            "description": (
                "当前发送者用 current_speaker；也可用 mentioned_user 或 replied_message_author"
            ),
        },
    }
    message: dict[str, object] = {
        "text": {"type": "string"},
        "artifact_id": {"type": "string", "description": "工作区对象 ID，不是路径或 URL"},
        "attachment_kind": {
            "type": "string",
            "enum": ["image", "file"],
            "description": (
                "提供 artifact_id 时必填。普通文件用 file；可附 text，平台分步交付时分别返回确认。"
                "图片用 image。部分成功不要重发文件。"
            ),
        },
    }

    group_selector: dict[str, object] = {
        "target_id": {"type": "string", "description": "目标群的 canonical UUID；当前群省略"},
        "display_name": {
            "type": "string",
            "maxLength": 128,
            "description": "目标群名称，不是成员名",
        },
    }

    def tool(
        name: str, description: str, fields: dict[str, object], required: tuple[str, ...] = ()
    ) -> ChatTool:
        return ChatTool(
            name=name,
            schema_version="3" if name == "send_message" else "1",
            description=description,
            parameters={
                "type": "object",
                "properties": fields,
                "required": list(required),
                "additionalProperties": False,
            },
        )

    return (
        ChatTool(
            name="read_conversation_history",
            description=(
                "读取已入账的会话历史，返回内部事件和人物编号。默认当前入口；"
                "跨入口使用内部 conversation_id 或明确人物/群，多个入口时从候选中选择。"
                "operation_id 沿原发送回执读取真实出站会话，event_id 只读当前会话原事件。"
                "coverage=observed 仅代表已观察资料，不冒充平台完整历史或转发内容。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    **selector,
                    "conversation_id": {"type": "string", "format": "uuid"},
                    "event_id": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "当前会话原外层内部事件 ID，与 operation_id 二选一",
                    },
                    "kind": {"type": "string", "enum": ["person", "space"]},
                    "operation_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "当前来源会话的发送回执；不能同时指定其他目标或账号",
                    },
                    "binding_id": {"type": "string", "format": "uuid"},
                    "presence_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "原私聊入口的内部 Presence ID；歧义时选择内部 Conversation ID"
                        ),
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
            result_cacheable=False,
            schema_version="2",
        ),
        tool(
            "find_contacts",
            "查找 Yuki 可联系的人或群。名称歧义必须澄清；成员名单出现不等于认识。",
            {
                **selector,
                "kind": {"type": "string", "enum": ["person", "space"]},
            },
            ("kind",),
        ),
        tool(
            "send_message",
            "回复当前用户或向外发消息时调用此工具；普通最终正文只是内部结果，不会送达。"
            "一次调用的纯文本会按回复分条规则自动发送为一条或多条，每条均有投递回执；"
            "确有需要时可多次调用，遵循发言节奏；发送后仍可继续工作。"
            "省略 target 默认当前群或当前私聊；发给其他人或群时用 canonical 目标，"
            "由后端选择私聊或群聊及发送账号。结果 uncertain 时不要重发。"
            "App/ACP 主会话显式指定 person 目标时需带 binding_id；省略 target 只回复本入口。"
            "群内真正 @某人使用 mentions，正文中的 @名字不会产生提醒。",
            {
                "target": {
                    "type": "object",
                    "description": (
                        "可选；省略时发送到当前群或当前私聊。明确发给别处时须指定 kind 和唯一标识。"
                    ),
                    "properties": {
                        "kind": {"type": "string", "enum": ["person", "space"]},
                        "binding_id": {
                            "type": "string",
                            "format": "uuid",
                            "description": (
                                "person 的指定入口绑定；从 App/ACP 向其他入口发送必须明确指定，"
                                "不能以主动路由猜测"
                            ),
                        },
                        **selector,
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                **message,
                "emoji": {
                    "type": "object",
                    "description": (
                        "可选的后台配图意图，goal写明想表达的完整含义。正文先发送，"
                        "后台从图库选图并单独发送；accepted只表示已接纳，无须等待或重发。"
                    ),
                    "properties": {
                        "goal": {"type": "string"},
                        "emotion": {"type": "string"},
                    },
                    "required": ["goal"],
                    "additionalProperties": False,
                },
                "reply_to_event_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "可选；当前上下文中可见的内部 #EventRecord.id，非平台消息号。",
                },
                "mentions": {
                    "type": "array",
                    "maxItems": 20,
                    "description": (
                        "真实提及成员，按顺序放在正文前；每项只选一种人物标识。"
                        "不能用文字 @名字代替。"
                        "@当前发言人用 mentions=[{subject_ref:current_speaker}]，顶层不填人物。"
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            **selector,
                            "binding_id": {
                                "type": "string",
                                "format": "uuid",
                                "description": "同平台多入口有歧义时明确指定内部 Binding",
                            },
                        },
                    },
                },
            },
        ),
        tool(
            "get_group_members",
            "分页读取本群已观察到的内部人物目录；coverage=observed，不代表平台完整名册。",
            {
                **group_selector,
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        ),
        tool(
            "recall_own_message",
            "撤回真实账本中的 Yuki 消息；必须由原发送账号执行，新账号不能代撤回。",
            {
                "event_id": {"type": "integer", "minimum": 1},
            },
            ("event_id",),
        ),
    )
