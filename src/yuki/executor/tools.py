"""One static local-device contract; actual grants are checked during execution."""

from yuki.domain.messages import ChatTool


def local_device_tools() -> tuple[ChatTool, ...]:
    text = {"type": "string"}
    return (
        ChatTool(
            name="local_device",
            description=(
                "使用本人明确授权的电脑执行文件和系统操作。list 查看实际设备、在线状态与授权；"
                "execute 使用目录中的 executor_id、capability 和 params，"
                "文件路径使用授权目录别名，不传宿主绝对路径。"
                "folder.read 读取或列目录，folder.write 写入或保存已发布成果；"
                "open.file/open.url/notify 只证明系统接纳，不证明用户已查看。"
                "pending/unknown 后 get 查询原 op_id，不重做原操作；"
                "该接口不控制原生 Agent 会话或远程屏幕。"
            ),
            result_cacheable=False,
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "execute"]},
                    "executor_id": text,
                    "op_id": text,
                    "capability": text,
                    "params": {"type": "object", "additionalProperties": True},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        ),
    )
