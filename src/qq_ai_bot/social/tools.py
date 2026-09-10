"""Deployment-stable social schemas; execution lives in the social service."""

from qq_ai_bot.domain.messages import ChatTool


def social_tool_definitions() -> tuple[ChatTool, ...]:
    selector: dict[str, object] = {
        "target_id": {"type": "string", "description": "find_contacts 返回的 canonical ID"},
        "display_name": {"type": "string", "maxLength": 128},
        "subject_ref": {"type": "string", "description": "真实当前发送者、mention 或 reply 引用"},
    }
    message: dict[str, object] = {
        **selector,
        "text": {"type": "string", "maxLength": 4000},
        "artifact_id": {"type": "string", "description": "工作区对象 ID，不是路径或 URL"},
        "attachment_kind": {"type": "string", "enum": ["image", "file"]},
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
            "Yuki 可自主私聊有真实互动且路由可用的人。目标只选一种；uncertain 不要重发。",
            message,
        ),
        tool(
            "send_group_message",
            "Yuki 可自主向启用且允许主动发言的群发送。目标只选一种；不解暂停、不换号试发。",
            message,
        ),
        tool(
            "poke_person",
            "戳一戳认识的人。省略 space_id 表示私聊；遵守后端限频，失败不连续重试。",
            {
                **selector,
                "space_id": {"type": "string"},
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
