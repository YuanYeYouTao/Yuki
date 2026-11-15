"""One public contract shared by every Yuki entrypoint and delegated automation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuki.domain.messages import ChatTool

# Manager RPC methods and job kinds of submitted executions, imported by the
# Manager (persistent.py). Wire protocol: it does not follow model tool names.
EXECUTION_TOOLS = frozenset({"terminal_exec"})
# Model tools served by the persistent environment or a personal computer.
SANDBOX_TOOLS = frozenset(
    {"run_command", "read_command", "control_command", "get_environment", "computer", "browser"}
)
# Actions that only observe. They write no pending receipt and take no input lease;
# a retry simply reads again. Every other action is an input with a durable receipt.
READ_ACTIONS = {
    "computer": frozenset({"screenshot", "resume", "zoom", "elements"}),
    "browser": frozenset({"snapshot", "screenshot"}),
}
# ``computer`` actions on Yuki's own computer as a whole; they never reach a driver.
LIFECYCLE_ACTIONS = frozenset({"wake", "sleep", "snapshot", "restore"})
# ``computer`` actions that exist only on Yuki's own computer; with ``device`` the Bot
# refuses them (``own_computer_only``) before anything reaches a Connector.
OWN_COMPUTER_ACTIONS = LIFECYCLE_ACTIONS | {"paste"}
_CLICKS = frozenset({"click", "double_click", "triple_click", "right_click", "middle_click"})


def computer_features(args: dict[str, Any]) -> frozenset[str]:
    """Connector features (``device_capabilities.computer.features``) a call relies on.

    An older Connector reads arguments it does not know with ``get`` and would silently
    ignore them, so such a call is refused before it is sent (task book §6.10).
    """
    action = args.get("action")
    needed = set()
    if args.get("scale") is not None:
        needed.add("scale")
    if action == "zoom":
        needed.add("zoom")
    if action == "elements" or args.get("element") is not None:
        needed.add("elements")
    if action in _CLICKS | {"drag", "scroll"} and args.get("text") is not None:
        needed.add("modifiers")
    if action in {"triple_click", "middle_click"}:
        needed.add(str(action))
    if args.get("repeat") is not None:
        needed.add("key_repeat")
    return frozenset(needed)


def tool(
    name: str,
    description: str,
    fields: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    namespace: str = "",
    read: bool = False,
) -> ChatTool:
    from yuki.domain.messages import ChatTool

    return ChatTool(
        name=name,
        namespace=namespace or ("sandbox.read" if read else "sandbox.run"),
        effect="read_state" if read else "write_state",
        description=description,
        result_cacheable=False,
        parameters={
            "type": "object",
            "properties": fields,
            "required": list(required),
            "additionalProperties": False,
        },
    )


def environment_tools() -> tuple[ChatTool, ...]:
    text = {"type": "string"}
    device = {
        "device": {
            "type": "string",
            "description": "本人电脑的设备 ID；省略时在 Yuki 自己的 Linux 环境执行。",
        }
    }
    identity = {"id": {"type": "string", "description": "run_command 返回的 id"}}
    return (
        tool(
            "run_command",
            "运行一条 Shell 命令。默认在 Yuki 自己的持久 Linux 环境（HOME=/home/yuki，"
            "文件在 /workspace，可用 Bash、Python、Node、apt、pip/npm 与公网）；"
            "给 device 则在该本人电脑的系统 Shell 执行。约 5 秒后返回，"
            "仍在运行时用 read_command 按 id 继续读，不要重新提交；tty=true 支持交互输入。"
            "不用它在本人电脑上模拟键鼠或截屏，界面操作用 computer。",
            {
                "command": text,
                **device,
                "cwd": text,
                "tty": {"type": "boolean"},
                "timeout_seconds": {"type": "integer", "minimum": 0},
            },
            ("command",),
        ),
        tool(
            "read_command",
            "按 id 读取命令的新输出、状态和退出码。wait_seconds 只等待观察，不停止命令；"
            "用结果的 next_cursor 继续读；空输出不代表已经结束。",
            {
                **identity,
                "wait_seconds": {"type": "number", "minimum": 0},
                "cursor": text,
            },
            ("id",),
            read=True,
        ),
        tool(
            "control_command",
            "控制运行中的命令：input 发送 text 原文（回车需写换行），interrupt 相当于 Ctrl+C，"
            "cancel 停止命令，close 关闭终端。已写出的文件保留。",
            {
                **identity,
                "action": {"type": "string", "enum": ["input", "interrupt", "cancel", "close"]},
                "text": text,
            },
            ("id", "action"),
        ),
        tool(
            "get_environment",
            "查看 Yuki 自己环境的容量、后台服务、浏览器模式、画面与会话状态、闲置休眠设置、"
            "整机快照列表与最近一次还原；给 device 则查看该本人电脑的系统、Shell 与授权。",
            {**device},
            read=True,
        ),
        tool(
            "computer",
            "看和操作电脑屏幕。省略 device 时操作 Yuki 自己 Linux 环境的桌面；"
            "device 指本人电脑。"
            "坐标：x,y（以及 x2,y2、region）是本次结果所附截图的像素，左上为原点，"
            "范围看结果的 width、height；若你习惯 0–1000 归一化坐标，必须传 scale=1000"
            "（两个轴都按 0–1000），不要在一次调用里混用两种约定；超出范围返回 "
            "coordinate_out_of_range 且没有任何输入，按错误里的坐标系改正再试。"
            "给 window（screenshot 返回的 windows[].id）时只截该窗口、坐标相对该窗口图。"
            "动作：screenshot 截屏；zoom 返回 region=[x0,y0,x1,y1] 的高清局部图"
            "（region 和之后的坐标都仍按整张截图计）；elements 列出 window 的无障碍节点"
            "（[节点 id] 与该窗口图上的边界，text 为过滤词），之后点击、移动、拖拽起点、滚动、"
            "type、key 可传 element=节点 id 代替 x,y"
            "（节点 id 带着窗口，省略 window 时按该窗口执行），"
            "element_stale 时按附带的新列表重选；"
            "click、double_click、triple_click（选中整行）、right_click、middle_click、move "
            "作用于 x,y；drag 从 x,y 拖到 x2,y2；scroll 在 x,y 滚动 dx,dy 格（dy 正数向下）；"
            "点击类、drag、scroll 的 text 是这一次按住的修饰键（shift、ctrl、alt、cmd，"
            "用 + 连接，如 ctrl+shift）；type 输入 text；key 按组合键 text"
            "（如 ctrl+l、Return），repeat 为连按次数（1–100）；resume 解除本人的叫停。"
            "只对 Yuki 自己的电脑：paste 把 text 经她自己的剪贴板粘贴到当前输入焦点"
            "（长文本比 type 快，会改写她自己的剪贴板）；wake 唤醒画面和浏览器，"
            "sleep 让画面层休眠（终端和后台进程照常运行）；snapshot 给整台电脑拍快照"
            "（rootFS 与 HOME，text 为备注，不含记忆和聊天，只保留最近几份、"
            "新的会挤掉最旧的），restore 把整台电脑还原到 snapshot（快照 ID）："
            "快照之后的改动全部丢失、正在运行的命令被中断，需要时先拍一份；"
            "结果未知时先看 get_environment 再决定。"
            "回执：point（以及 point2）是实际使用的本图像素，scale 是你声明的坐标系，"
            "route 是投递途径（accessibility 无障碍动作、window_events 后台窗口事件、"
            "global_input 全局键鼠、clipboard 剪贴板），hit 是投递前落点上的控件，"
            "cursor 是全屏截图里指针的位置（指针也画在图里）；submitted 只表示已投递，"
            "是否生效以动作后的新截图为准。"
            "本人电脑上给 window 即在后台操作该窗口，不动本人的光标和前台应用；"
            "type、key 给 x,y 或 element 时先让该处输入框成为焦点（前台为在该处点击），"
            "否则落在当前的输入焦点；"
            "投递不了时返回 device_not_supported 并在 cause 写明原因"
            "（如 no_background_route 需要时省略 window 改在前台操作，"
            "connector_outdated 请本人更新桌面端）。"
            "本人接管或叫停时输入返回 control_held（截图、zoom、elements 照常可用）；"
            "本人电脑上本人刚用过电脑（给 window 时只算本人正在用该窗口所属的应用）时"
            "输入不执行、返回 owner_active，重新截图后再试。"
            "本人电脑的界面（打开或切换应用、点击、按键、输入、截屏）只用本工具，"
            "不用 run_command 跑 osascript、System Events、screencapture 等脚本代替。"
            "网页优先用 browser。",
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "screenshot",
                        "zoom",
                        "elements",
                        "click",
                        "double_click",
                        "triple_click",
                        "right_click",
                        "middle_click",
                        "move",
                        "drag",
                        "type",
                        "paste",
                        "key",
                        "scroll",
                        "resume",
                        "wake",
                        "sleep",
                        "snapshot",
                        "restore",
                    ],
                },
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "x2": {"type": "integer"},
                "y2": {"type": "integer"},
                "dx": {"type": "integer"},
                "dy": {"type": "integer"},
                "text": text,
                "repeat": {"type": "integer", "minimum": 1, "maximum": 100},
                "scale": {
                    "type": "integer",
                    "enum": [1000],
                    "description": "坐标用 0–1000 归一化时传 1000；省略时是截图像素",
                },
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "zoom 的 [x0,y0,x1,y1]，整张截图的坐标",
                },
                "element": {"type": "string", "description": "elements 返回的节点 id"},
                "window": {"type": "string", "description": "screenshot 返回的 windows[].id"},
                "display": text,
                "snapshot": {
                    "type": "string",
                    "description": "restore 的快照 ID（见 get_environment 的 snapshots）",
                },
                **device,
            },
            ("action",),
        ),
        tool(
            "browser",
            "使用 Yuki 专用浏览器，按页面节点操作、不动鼠标。open 打开 url"
            "（给 tab 在该标签页跳转，否则新开）；snapshot 读页面结构，text 为过滤词；"
            "click、hover 节点；type 以 text 替换输入框内容（文件输入框写文件路径）；"
            "press 按键 text（如 Enter、ctrl+a）；select 按 text 选定选项；scroll 滚到 "
            "node 或 text=down|up|像素；screenshot 截取页面；close 关闭标签页；"
            "accept_dialog、dismiss_dialog 处理弹窗（prompt 的输入写 text）。"
            "node 用快照中的 [e…] 编号，node_stale 时按附带的新快照重选。"
            "下载保存到 /workspace/downloads。",
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "open",
                        "snapshot",
                        "click",
                        "type",
                        "press",
                        "select",
                        "hover",
                        "scroll",
                        "screenshot",
                        "close",
                        "accept_dialog",
                        "dismiss_dialog",
                    ],
                },
                "url": text,
                "node": text,
                "text": text,
                "tab": text,
                **device,
            },
            ("action",),
        ),
    )


def _cursor(value: object, name: str = "cursor") -> int:
    """An opaque cursor string issued by a previous result: its byte offset."""
    from yuki.external_agents.errors import ExternalAgentArgumentError

    if not isinstance(value, str) or not value.isdigit():
        raise ExternalAgentArgumentError(
            "invalid_cursor", f"{name} 使用上一次结果返回的 next_cursor 原值"
        )
    return int(value)


def command_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Translate a model call into the unchanged Manager/Connector wire arguments.

    The wire arguments are also the original task-row payload, so equal calls
    keep the same request fingerprint as before the tool rename.
    """
    wire: dict[str, Any] = {}
    for key, value in args.items():
        if key == "device":
            wire["device_id"] = value
        elif key == "id":
            wire["run_id"] = value
        elif key == "cursor":
            wire["cursor"] = _cursor(value)
        else:
            wire[key] = value
    return wire


# Wire and receipt bookkeeping kept in typed data, never shown to the model.
_COMMAND_INTERNAL = frozenset(
    {
        "run_id",
        "request_id",
        "state_sequence",
        "admitted",
        "cursor",
        "output_offset",
        "completion",
        "retryable",
        "uncertain",
        "pending",
        "error",
    }
)


def command_view(data: dict[str, Any]) -> dict[str, Any]:
    """Model view of a command receipt: ``id`` plus the observed facts."""
    view: dict[str, Any] = {}
    if isinstance(data.get("run_id"), str):
        view["id"] = data["run_id"]
    view.update(
        {
            key: value
            for key, value in data.items()
            if key not in _COMMAND_INTERNAL and value is not None
        }
    )
    if "status" not in view and data.get("pending") is True:
        view["status"] = "running"
    if isinstance(data.get("next_cursor"), int) and not isinstance(data["next_cursor"], bool):
        view["next_cursor"] = str(data["next_cursor"])
    return view
