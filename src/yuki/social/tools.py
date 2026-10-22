"""Deployment-stable social schemas; execution lives in the social service."""

from yuki.domain.messages import ChatTool
from yuki.services.contact_query import contact_schema


def social_tool_definitions() -> tuple[ChatTool, ...]:
    message: dict[str, object] = {
        "text": {"type": "string"},
        "path": {
            "type": "string",
            "description": (
                "要发送的 /workspace 文件或 /inputs/media-cache 原件路径；"
                "程序冻结本次版本后发送。与 artifact_id 二选一。"
            ),
        },
        "artifact_id": {
            "type": "string",
            "description": "已发布不可变快照的内部 ID；与 path 二选一，不是 URL。",
        },
        "name": {"type": "string", "description": "可选；路径文件本次交付的文件名。"},
        "expected_version": {
            "type": "string",
            "description": "可选；path 所选原文件的内容 SHA256，不是目标覆盖门槛。",
        },
        "attachment_kind": {
            "type": "string",
            "enum": ["image", "file"],
            "description": (
                "提供 path 或 artifact_id 时必填。普通文件用 file；可附 text，"
                "平台分步交付时分别返回确认。"
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
            schema_version="5" if name == "send" else "1",
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
            name="find_contacts",
            schema_version="2",
            description=(
                "发现人物、群及历史入口。subjects（默认）以 kind=person/space 查可联系主体；"
                "entries 按真实 Conversation 平铺历史入口，含旧账号阶段，不要求 kind。"
                "source 可按平台和 private/group 交集筛选，display_name 按名称包含查询。"
                "entries 的 target 使用一个内部 ID；结果提供原 Binding/Conversation ID 和话题资料。"
                "limit 为页大小，按内部 ID 升序，cursor 续查；同名返回候选，不猜目标。"
            ),
            parameters=contact_schema(),
        ),
        tool(
            "send",
            "向当前会话或指定目标发送文字、图片、文件或表情；普通最终正文只是内部结果，不会送达。"
            "发文件可直接提供 path 和 attachment_kind，无须先调用发布工具；"
            "已有 artifact_id 也可发送。"
            "一次调用的纯文本会按回复分条规则自动发送为一条或多条，每条均有投递回执；"
            "确有需要时可多次调用，遵循发言节奏；发送后仍可继续工作。"
            "省略 target 默认当前群或当前私聊；发给其他人或群时用 canonical 目标，"
            "由后端选择私聊或群聊及发送账号。结果 uncertain 时不要重发。"
            "App/ACP 主会话显式指定 person 目标时需带 binding_id；省略 target 只回复本入口。"
            "正文在原位置写 @名字即可真实提及；同名默认选择目标会话最近发言的账号。"
            "要指定账号时复制目录给出的 @{account:UUID}。",
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
                        "target_id": {
                            "type": "string",
                            "format": "uuid",
                            "description": "内部 Person/Space ID",
                        },
                        "conversation_id": {
                            "type": "string",
                            "format": "uuid",
                            "description": "明确发送入口的内部 Conversation ID",
                        },
                    },
                    "required": ["kind", "target_id"],
                    "additionalProperties": False,
                },
                **message,
                "emoji": {
                    "type": "object",
                    "description": (
                        "可选的后台配图意图，goal写明想表达的完整含义。可以只发表情，不填 text；"
                        "有正文时先发正文，"
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
                    "description": "可选；实际授权会话中的内部 #EventRecord.id，非平台消息号。",
                },
            },
        ),
        tool(
            "get_group_members",
            "分页读取实际群入口已观察到的逐账号目录；coverage=observed，不代表平台完整名册。",
            {
                **group_selector,
                "binding_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "具体群入口的内部 SpaceBinding ID",
                },
                "conversation_id": {"type": "string", "format": "uuid"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        ),
        tool(
            "recall_own_message",
            "撤回真实账本中自己发送的消息；必须由原发送账号执行，新账号不能代撤回。",
            {
                "event_id": {"type": "integer", "minimum": 1},
            },
            ("event_id",),
        ),
    )


SOCIAL_TOOL_NAMES = frozenset(tool.name for tool in social_tool_definitions())
