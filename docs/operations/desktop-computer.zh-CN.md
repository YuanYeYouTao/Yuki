# 桌面电脑与有头环境

Yuki 的持久 Linux 环境、远程画面和本人电脑的文件执行器是三项真实能力。原生 Agent Connector 继续管理 Codex／Claude／ACP 会话，不替代 VNC 或本机文件、URL、通知操作。

画面和浏览器由沙箱档位决定：`desktop` 档有 Xvnc 桌面与有头 Chromium，`headless` 档只有无头 Chromium，`lite` 档没有浏览器。档位、换档、资源与安装见[持久工作环境](persistent-environment.zh-CN.md#档位)；Manager 按"配置的模式 + 实际安装的软件"报告 `desktop` 与 `browser_mode`，并把生效的模式写入运行记录卷的 `computer/browser-mode` 供桌面服务读取。

`-desktop` 镜像安装真实的 TigerVNC/Xvnc、Openbox、Chromium、socat、xdotool、ImageMagick、at-spi2-core（无障碍总线与 registry）、xclip 和中文／Emoji 字体（`environment_programs.py headed`），并记录实际软件包版本；已有环境由 Manager 启动时的 `apply_programs()` 按档位补装。`yuki_computer.py`、`yuki_accessibility.py`（ctypes 调 `libatspi`）、`desktop_service.py` 与连接器包中只依赖标准库的共享模块 `yuki_agent_connector/cdp.py`、`yuki_agent_connector/actions.py`（按键、修饰键与坐标约定，两条路径共用）由 Manager 从代码卷只读挂入，随 Bot 版本更新。VNC 和浏览器 CDP 仅监听容器 loopback，不发布 5901 或 9222。桌面服务的子进程分三类。显示器 Xvnc `:1` 属于系统，退出即结束整个服务，由 Supervisor 按 on-failure 重启，会话随之重新运行。会话属于她：有 `~/.xsession` 时以 `/bin/sh ~/.xsession` 运行，否则运行 `openbox-session`（执行 `/etc/xdg/openbox/autostart` 与 `~/.config/openbox/autostart`）；环境为 `DISPLAY=:1`、`HOME=/home/yuki`，输出写 `~/.xsession-errors`。会话退出（换窗口管理器、注销、出错）不结束显示器和 Yuki 的浏览器，也不自动重启，状态文件记 `session: exited:<退出码>`；要重开整个画面就休眠再唤醒。自动化浏览器 Chromium 与驱动属于系统：本人关掉 Chromium 窗口时只重新打开 Chromium 和持有其 CDP 连接的驱动，画面和桌面上其他窗口不受影响。启动顺序为 Xvnc 就绪 → 会话 D-Bus（`dbus-daemon --session`）与 AT-SPI 总线启动器 → 会话 → Chromium，让窗口管理器先在；总线启动器在显示器根窗口发布 `AT_SPI_BUS`，她从终端启动、没有会话 D-Bus 地址的 GTK／Qt 应用也会注册无障碍信息。`desktop-status.json` 的 `session` 为 `running`（她的 `~/.xsession`）、`default`（`openbox-session`）或 `exited:<code>`，经 `get_environment` 与桌面端返回。`headless` 档没有 X，不运行会话。镜像只预装 Openbox，换 xfwm、LXQt 之类由她自己安装并写 `~/.xsession`。

Manager 创建环境时以原环境名称设置稳定 hostname，使持久 HOME 中 Chromium 的会话和配置在容器重建后仍属于同一主机。浏览器配置和锁文件不作为安装缓存清理。

本地构建未发布版本的环境镜像：`docker compose -f deploy/sandbox/compose.build.yaml build`，再在部署 `.env` 设 `YUKI_ENVIRONMENT_IMAGE=yuki-environment:local`。

## 使用与核验

桌面端使用电脑页的唤醒、休眠、接管、交还、快照与还原和文件入口；窗口隐藏或最小化时断开画面流。Yuki 用模型工具 `computer`（截屏、`zoom` 局部高清图、`elements` 无障碍节点、点击／双击／三击／右键／中键、移动、拖拽、输入、`paste`、按键与连按、滚动；点击类、拖拽、滚动可按住修饰键；坐标是本次结果所附截图的像素，传 `scale=1000` 时为 0–1000，越界在任何输入前返回 `coordinate_out_of_range`；给 `window` 时只截该窗口、坐标相对该窗口；规则见下文“Computer Use”）和 `browser`（按快照节点操作 Yuki 专用 Chromium，含对话框、下载、文件输入与跨站 iframe）操作这台电脑；桌面服务未运行时，第一次动作沿原 `display_wake` 路径唤醒。`computer` 另有只作用于她自己电脑的 `wake`、`sleep`、`snapshot`（`text` 为备注）与 `restore`（`snapshot` 为快照 ID），不进驱动：`wake`／`sleep` 以工具调用键写 `environment_mutations`，同键返回原回执，`sleep` 回执附休眠时的标签页及其 Work，本人持有控制权时 `control_held`；`snapshot`／`restore` 按调用键登记到 Manager 的快照与还原记录（见[持久工作环境 · 整机快照与还原](persistent-environment.zh-CN.md#整机快照与还原)）。给出 `device` 时这四个动作返回 `device_not_supported`。

画面层闲置 `YUKI_SANDBOX_DISPLAY_IDLE_MINUTES` 分钟（默认 30）后自动休眠：停止 Xvnc、会话、Chromium 与驱动，X 客户端随显示器退出，环境、终端与后台进程照常运行；下次使用自动唤醒，Chromium 恢复上次的标签页。工具调用、任何一路画面流（含紧凑预览）、本人持有控制权和唤醒都算在用，计时只在 Manager 内存。闲置休眠必定在没有动作进行时发生，不因存在未完成 Work 的标签页而跳过；本人从桌面端休眠时原 `confirm_sleep()` 确认流程不变。

动作经 Manager RPC `computer_action`／`browser_action` 按环境串行执行：输入类先写 `environment_mutations` 待定回执，在 Manager 进程内按调用键取 agent 租约，经 runtime 挂载 `computer/driver/requests/` 把请求交给桌面服务托管的长驻驱动（`yuki_computer.py serve`，浏览器部分为 `cdp.py`），驱动以改名领取请求，每次输入（含 xdotool）前复核 `control.json` 中的租约，结果与图像写回 `computer/driver/results/` 后由 Manager 读取删除，最后写结果回执并释放租约。同一调用键重试返回原结果；只剩待定回执（Manager 中途结束）时报 `unknown`，并撤回尚未被领取的请求，绝不再执行；Manager 等待到期时请求仍未被领取则确定未执行。截图、快照与沙箱 `resume` 是读取，不写回执、不取租约，本人接管期间照常可读。沙箱没有“本人叫停”状态，`resume` 只返回与截图相同的读取结果；本人接管与交还仍在桌面端完成。驱动在 `computer/driver/tabs.json` 写出标签页及其 Work 归属（新标签页按 `openerId` 归属），电脑页据此显示。

截图由环境按当前显示几何与部署的 `COMPUTER_SCREENSHOT_MAX_DIMENSION`／`COMPUTER_SCREENSHOT_MAX_PIXELS`（默认 1568／115 万像素，可热改 `computer.screenshot_max_*`，与聊天图片的 `VISION_MAX_*` 分开）直接产出最终尺寸（Host 不再缩放，1440×900 的画面出图 1356×848），下载保存到 `/workspace/downloads`，文件输入框写入环境内路径，不弹出原生选择框。需要本人协助时仍在终端运行 `yuki-computer help-request --note '请协助当前画面'` 或 `yuki-computer wait-control`；本人接管后 Yuki 的下一次输入返回 `control_held`，原工作等待原终端回执，断开桌面连接不创建第二个工作。`yuki-computer status` 显示控制状态与标签页。

### Computer Use（[任务书](../architecture/Computer-Use任务书-2026-11-15.md)）

- 回显：每个带图结果有 `width`、`height`、`coordinates`（坐标系说明），全屏截图有 `cursor`（指针在本图上的位置；指针由 XFixes 读取并画进图里，读不到时照常出图、省略该字段）；输入另有 `point`（以及 drag 的 `point2`，均为本图像素）、`scale`、`route`（`global_input`、`accessibility`、`clipboard`）和 `hit`（投递前落点上的 AT-SPI 节点，取不到时省略，不阻止输入）。
- `zoom`：`region=[x0,y0,x1,y1]` 按整张截图坐标（受 `scale` 约束），驱动按原生像素截取该区域再按同一上限只缩不放；读动作，本人接管期间可用。
- `elements`／`element`：`elements(window)` 经 `_NET_WM_PID` 找到该窗口的 AT-SPI 应用，按几何选中对应窗口节点并列出可见节点，id 形如 `<window>.<tag>e<n>`，只认该窗口最近一次列表，驱动重启后全部失效；`element` 让点击优先走 AT-SPI 动作、`type`／`key` 先 `grab_focus`。Chromium 的网页内容不经 AT-SPI 暴露（不打开 `org.a11y.Status.IsEnabled`），网页用 `browser`；总线、`libatspi` 不可用或应用未注册时返回 `device_not_supported`（`cause: no_accessibility`），其他动作不受影响。
- 按键：`key` 的 `text` 先经共享语法 `actions.parse_keys` 解析，再映射为 xdotool keysym；未知键在任何输入前返回 `invalid_arguments`。`repeat`（1–100）每次都先复核租约。拖拽在按下与抬起之间分 12 步移动。修饰键用 `keydown`／`keyup` 包住动作，失败时也释放。
- `paste`（只在她自己的电脑上）：驱动用 `xclip` 把 `text` 同时写入她自己 X 显示的 CLIPBOARD 与 PRIMARY，再按 `shift+Insert`（GTK／Qt／Chromium 读 CLIPBOARD，xterm 类终端读 PRIMARY）；回显 `route:["clipboard","global_input"]`。写入选区前复核租约，本人接管时选区不变；没有 `xclip` 时返回 `device_not_supported`（`cause: no_clipboard`）。桌面端只向 Xvnc 发送本人主动粘贴的文字，不订阅 Xvnc 的剪贴板事件，Yuki 的粘贴不会进入本人电脑的剪贴板。给 `device` 时 Bot 直接返回 `device_not_supported`（`cause: own_computer_only`）。
- E2E：`tests/integration/test_computer_use_e2e.py`（CI `desktop-headed`）以真实 Manager、Xvnc、Chromium、驱动、AT-SPI 与 xclip 核对上述行为，本地页面记录 Chromium 实际收到的指针与键盘事件；产物在 `test-results/computer-use/`（`steps.json`、每步截图及画出 `point` 的标注图、`pointer-lab-events.json`）。

核验应分别记录：实际 manifest、`display_status`、Xvnc/RFB 画面、浏览器真实输入与截图、控制权撤销、原请求重试和原工作接续。会话、闲置休眠与 `wake`／`sleep` 在有头镜像中由 `tests/integration/test_desktop_session_e2e.py` 以真实 Xvnc、`openbox-session`、Chromium 与驱动验证，产物在 `test-results/desktop-session/`（每步截图与 `steps.json`）。`tests/integration/test_computer_browser_e2e.py` 在有头镜像中以脚本化模型驱动完整应用、真实 Manager、Xvnc、Chromium 与驱动，产物在 `test-results/computer-browser/`。仅有一个返回成功的 fixture 不能证明现场有头环境健康。源码测试中的协议 fixture 使用可控 RFB 端点验证 UI 和授权；独立的 `test_real_headed_service_renders_unicode_exposes_rfb_and_stops_children` 启动真实 Xvnc、`openbox-session`、Chromium，读取真实 RFB banner、中文页面和 PNG，验证进程退出。完整容器与生产接入另外验收。

本机 Codex、Claude 由桌面的原生 Agent 连接入口管理，云端复用固定 `use_coding_agent` 操作。连接本身不要求项目目录；服务器角色电脑详细环境、画面、文件及控制供 Person 当前超管管理，基础可用性仍向有效设备公开，本人 Connector 的设备、原生会话和操作仍核真实 Person 归属。旧 `local_device` 文件执行器及活 API 已退出；原表、授权记录、操作 UUID 和回执字节保留，旧 Work 仅查询原事实，不重派、不转成 Native 操作，`unknown` 不转成成功或重做。
