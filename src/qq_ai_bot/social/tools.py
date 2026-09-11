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
        **selector,
        "text": {"type": "string", "maxLength": 4000},
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
            "send_private_message",
            "回复当前私聊对象使用 subject_ref=current_speaker，经当前接收账号发送；"
            "联系其他人须主动路由可用。target_id 只接受 canonical UUID，不接受 QQ 号。"
            "目标只选一种；uncertain 不要重发。",
            message,
        ),
        tool(
            "send_group_message",
            "Yuki 可自主向启用且允许主动发言的群发送。目标只选一种；不解暂停、不换号试发。",
            message,
        ),
        tool(
            "poke_person",
            "戳一戳认识的人。默认在当前群操作，私聊中默认私聊；"
            "scene=private 可明确选择私聊。群戳不依赖对方私聊主动路由。失败不连续重试。",
            {
                **selector,
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
                **selector,
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
