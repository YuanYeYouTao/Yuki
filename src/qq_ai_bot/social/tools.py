"""Deployment-stable social schemas; execution lives in the social service."""

from qq_ai_bot.domain.messages import ChatTool


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
        "text": {"type": "string", "maxLength": 12000},
        "artifact_id": {"type": "string", "description": "工作区对象 ID，不是路径或 URL"},
        "attachment_kind": {
            "type": "string",
            "enum": ["image", "file"],
            "description": (
                "提供 artifact_id 时必填。普通文件用 file；可附 text，文件成功后另发说明。"
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
                "从 OneBot 实时读取明确的人或群的最近会话。主动发信后可用发送回执 operation_id "
                "查看原账号上的后续消息；回执本身不表示对方回复。也可用 kind=person/space "
                "配合 target_id/display_name/subject_ref；"
                "多 QQ Binding 或多个在线账号必须明确选择。"
                "返回有界历史和来源，不会发送消息，也不把其他私聊记进当前群。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    **selector,
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
                        "description": "读取哪个 Yuki QQ 账号的会话；歧义时从返回候选中选择",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
            result_cacheable=False,
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
            "也可以主动多次调用，发送后仍可继续工作。"
            "省略 target 默认当前群或当前私聊；发给其他人或群时用 canonical 目标，"
            "由后端选择私聊或群聊及发送账号。结果 uncertain 时不要重发。"
            "群内真正 @某人使用 mentions，正文中的 @名字不会产生提醒。",
            {
                "target": {
                    "type": "object",
                    "description": (
                        "可选；省略时发送到当前群或当前私聊。明确发给别处时须指定 kind 和唯一标识。"
                    ),
                    "properties": {
                        "kind": {"type": "string", "enum": ["person", "space"]},
                        **selector,
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                **message,
                "voice": {
                    "type": "object",
                    "description": "将 text 合成为语音并立即发送；若还要文字，请再单独发送一条。",
                    "properties": {
                        "style_hint": {"type": "string", "maxLength": 128},
                        "language": {"type": "string", "enum": ["auto", "zh", "jp"]},
                        "request_basis": {
                            "type": "string",
                            "enum": ["user_requested", "agent_initiated"],
                        },
                    },
                    "required": ["request_basis"],
                    "additionalProperties": False,
                },
                "emoji": {
                    "type": "object",
                    "description": "选择一张已采用表情并立即发送；可与 text 同一条发送。",
                    "properties": {
                        "goal": {"type": "string", "maxLength": 300},
                        "emotion": {"type": "string", "maxLength": 100},
                    },
                    "required": ["goal"],
                    "additionalProperties": False,
                },
                "reply_to_event_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "可选；当前上下文中可见的内部 #EventRecord.id，非 QQ 消息号。",
                },
                "mentions": {
                    "type": "array",
                    "maxItems": 20,
                    "description": (
                        "真实 @成员，按顺序放在正文前；每项只选一种人物标识。不能用文字 @名字代替。"
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
                                "description": "多 QQ 账号时明确指定有效 Binding",
                            },
                        },
                    },
                },
            },
        ),
        tool(
            "poke_person",
            "戳一戳认识的人。默认在当前群操作，私聊中默认私聊；"
            "scene=private 可明确选择私聊。群戳不依赖对方私聊主动路由。失败不连续重试。",
            {
                **selector,
                "binding_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "人物的 QQ Binding；多账号有歧义时必须明确",
                },
                "space_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "可选 canonical 群 UUID，不是 QQ 群号；省略使用当前群",
                },
                "scene": {"type": "string", "enum": ["current", "private"]},
            },
        ),
        tool(
            "get_group_members",
            "分页查询 Yuki 当前实际可访问群的成员，不会提供私聊历史。",
            {
                **group_selector,
                "space_binding_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "多个 QQ 群 Binding 时必须明确",
                },
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
