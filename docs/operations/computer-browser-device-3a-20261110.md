# 本人电脑 computer / browser（阶段 3a·前台，2026-11-10）

依据[统一 Computer Use 与 Browser Use 任务书](../architecture/Yuki-统一ComputerUse与BrowserUse任务书-2026-11-09.md)
§3 决定 4–8、§4.2、§5.1（合同已于阶段 1 冻结，不改参数）、§5.2、§5.5 第 3–5 层、§5.7、§5.8、§7 与 §8“阶段 3a”。
本记录先列出写代码前的失败情况，再记录实现与验证。本人输入由“叫停”改为“让出并重试”（D8、D9 改写，D11–D15
新增，均在改代码前写出）。阶段 3b（§5.5 第 2 层按窗口后台投递）与 Yuki-Desktop 界面不在
本次范围；Desktop 与连接器之间的 stdin/stdout 合同见[本机连接器 · 叫停、活动与权限行](local-agent-connector.zh-CN.md#叫停活动与权限行)。

## 1. 写代码前列出的可能失败情况

每条写明“错误行为”，括号内为本次应有的行为。

### 7.4 本人电脑

- D1 macOS 未授予屏幕录制／辅助功能时截图得到黑图或桌面壁纸、`CGEventPost` 被静默丢弃却回执
  `submitted`（截图前查 `CGPreflightScreenCaptureAccess`、输入前查 `CGPreflightPostEventAccess`，分别返回
  `screen_permission_required`／`input_permission_required`，不投递任何事件；`permissions` 行与能力事实
  `computer.screen`／`computer.input` 为同一预检结果）。
- D2 屏幕锁定、显示器休眠、快速切换到其他用户、Windows 安全桌面（锁屏、UAC）时截图或输入报告成功
  （macOS 锁屏或显示器休眠 `display_locked`，会话不在控制台 `desktop_unavailable`；Windows 输入桌面不是
  `Default` 或无法打开 `desktop_unavailable`；均在任何输入之前判断）。
- D3 前台或点击处的窗口属于提升权限（更高完整性级别）的进程时，`SendInput` 被 UIPI 静默丢弃却回执
  `submitted`（输入前按目标窗口所属进程的完整性级别判断，返回 `target_elevated`，不投递）。
- D4 旧连接器收到 `computer`／`browser` 命令时没有得到 `invalid_resource`，或服务端把它报成含糊的
  `tool_failed`（服务端转为 `device_not_supported` 并说明需更新连接器）；`get_environment(device)` 与 Client API
  设备列表丢失 `device_capabilities.computer`；Linux 或 macOS 14 以下的连接器声称有 `computer` 能力。
- D5 `full_access` 关闭时仍截图或输入（含读类：截图同样是本机完整访问；返回 `full_access_disabled`，与
  `run_command(device)` 相同；每条命令重读配置，关闭立即生效）。
- D6 点击已投递但截图字节传回失败或图像归档失败时丢失效果或报 `unknown`（回执保持 `submitted:true`，
  `media_read:false`、`media_error` 说明原因）。
- D8 本人输入（含屏幕共享、远程控制）之后 Yuki 的下一次全局输入照常执行；Yuki 自己的合成输入被误判为本人输入
  （连续两次 Yuki 输入之间没有本人输入时第二次照常执行）；模型思考期间（截图与下一次输入之间）的本人输入被漏掉
  （截图也推进基准，基准之后的本人输入都算）。
- D9 Desktop「停止」后 `computer` 输入或 `browser` 输入（`open`、`click`、`type` 等）仍执行（都返回 `control_held`；
  截图、快照、浏览器截图照常可读）；叫停后同一次输入仍部分执行（`type` 段间复核）；恢复由计时器、重连或连接器
  重启触发（只由 Desktop「繼續」或 `computer(action=resume, device)`）；`resume` 被设备归属人以外的人触发（服务端按
  既有设备归属核验，非归属人得到 `not_found`，命令不发往连接器）。
- D10 Desktop 重启连接器后叫停丢失（Desktop 在拉起子进程后立即写当前 holder；连接器启动时打
  `reason:"start"` 的 `holder:"none"`，收到后打 `reason:"desktop"` 的 `holder:"owner"`，之后输入为 `control_held`）。
- D11 本人输入被当成叫停：置 `holder=owner`、打出 `control` 行、之后每次输入都要 `resume`（本人输入只让 Yuki 让出
  这一次输入：holder 不变、不打 `control` 行、不需要 `resume`；`holder=owner` 只来自 Desktop「停止」）。
- D12 发现本人输入后立即执行，或本人停手的瞬间就执行、点在已经变化的画面上（等本人连续 1.5 秒没有输入，然后
  **拒绝且不执行**：`owner_active`、`status:"failed"`，消息说明本人刚用过电脑、画面可能已变，先重新截图再重试；
  不打 `activity` 行，不投递任何事件）。
- D13 本人一直在打字或操作时 Yuki 无限等待、占住设备（最多等 10 秒，到时同样 `owner_active` 拒绝且不执行）。
- D14 让出后重新截图、本人已停手时重试仍被拒绝（截图推进基准，重试照常执行）；截图或其他读动作等待本人停手
  （读动作从不等待、从不让出）；`browser` 输入因本人用电脑而让出（浏览器经 CDP 投递，不碰光标，只受 Desktop
  「停止」约束）。
- D15 macOS 上本人只移动指针也让 Yuki 让出（只认有意输入：按键按下、左／右／其他鼠标键按下、三种拖拽、滚轮，
  分别读 `CGEventSourceSecondsSinceLastEventType` 取最近一次；Windows 仍为 `GetLastInputInfo`，含移动，低级钩子
  推迟）。
- D7（后台投递）属 3b。3a 中给出 `window` 且给出 `device` 时不得截整屏或全局输入冒充窗口操作
  （返回 `device_not_supported`，说明按窗口操作尚未支持、省略 `window` 使用整屏）。

### 7.2 图像

- M6 图像坐标与点击坐标不一致：Retina（点与像素 2:1）、Windows DPI（125%／150%）、副屏在主屏左侧或上方
  （负坐标）、按 profile 上限缩小后的图像；使用保存的“上一张截图”尺寸而不是执行时当下的几何；Windows
  绝对坐标没有按虚拟桌面归一化（`MOUSEEVENTF_ABSOLUTE|MOUSEEVENTF_VIRTUALDESK`，0–65535 覆盖整个虚拟桌面）。
  图像尺寸只由当下显示几何与服务端传来的 `max_dimension`／`max_pixels` 决定，Host 不再缩放。

### 7.3 浏览器

- B8 Yuki 专用 Chrome：找不到 Chrome 时去下载或报含糊错误（`browser_unavailable`）；使用本人默认 profile
  或驱动本人正在用的 Chrome（profile 在连接器数据目录，只连该 profile 的 `DevToolsActivePort`）；重复启动拿到
  无调试端口的实例（先连已有实例，失败才启动；启动前删除过期的 `DevToolsActivePort`）；缺少三项防后台节流参数
  导致被遮挡时截图停住；本人关闭窗口或退出 Chrome 后无法重新打开（下一次动作重新连接或启动）；随连接器
  重启或被杀而退出（macOS／Linux 新会话，Windows `CREATE_BREAKAWAY_FROM_JOB`）；下载落在连接器私有目录之外。

### 7.1 合同与恢复（连接器上）

- C4 同一调用键重试时重新点击／输入，而不是返回连接器 journal 中的原回执；读类动作（截图、`resume`、
  快照、浏览器截图）写 journal 或重试时得到 `unknown`。
- C5 连接器在 journal 待定与结果之间崩溃或被杀后，同一调用键报告成功或重新执行（`status:"unknown"`，服务端
  `action_unknown`，绝不重派）；连接在命令发出后断开时输入报成“没有执行”（输入 `unknown`，读类不报）。

### Desktop 合同（stdin／stdout）

- K1 启动时没有 `control` 行，或叫停状态变化时没有行、同一状态重复打行。
- K2 stdin 的 `{"type":"control","holder":"owner"|"none"}` 没有生效；其他类型的行、空行或无法解析的行
  使连接器退出；EOF 不再停止运行（EOF 仍取消运行，退出码 0）。
- K3 非读动作前后没有 `activity` 行（`phase:"start"`／`"end"`，失败与异常也有 `end`）；被拒绝而没有执行的
  输入（`control_held`、`owner_active` 等）打出 `activity` 行；读动作打出 `activity` 行。
- K4 启动时没有 `permissions` 行；预检结果变化（本人在系统设置中授权）时没有新行，或能力事实没有随之
  更新到服务端（重新发送 catalog）；Windows 上 `screen`／`input` 不是 `true`；不支持的平台打出 `permissions` 行。
- K5 普通 `run`（无 `--desktop-stdin`）打出任何 `control`／`activity`／`permissions` 行（与状态行一样只在
  `--desktop-stdin` 下打）；多线程同时写 stdout 时两行交错成一行。

### 实施补充（连接器 ↔ 服务端）

- G1 截图字节没有走 `FileTransfers` 的读／释放循环（例如塞进响应 JSON），或截图文件读完后留在连接器目录
  （释放即删除；连接器启动时清掉上次遗留的截图）。
- G2 设备离线、连接在发出前断开时报 `unknown`（`device_offline`，没有执行）；非设备归属人调用时把命令发往
  连接器（`not_found`，与 `run_command(device)` 同一规则）。
- G3 连接器侧阻塞调用（截图、输入、CDP 等待）在事件循环线程执行，使心跳超时、断线（截图与输入在线程中按
  设备串行，浏览器有自己的线程，事件循环不等待它们之外的任何东西）。
- G4 叫停标志写入配置文件、journal、服务端表或可经 CLI 设置（只在连接器内存中）。

## 2. 设计落实

### 连接器（0.4.4 → 0.5.0，协议仍为 4）

- 新资源 `computer`、`browser`（`desk.py`）：旧连接器对它们回 `invalid_resource`，所以不升协议（D4）。两资源都在
  完整访问之后（D5），由 `Connector.dispatch` 照旧处理 journal：`screenshot`、`snapshot`、`resume` 加入读动作
  （不写 journal、重试即重读），其余先 `journal.begin`，同键返回原回执，执行中崩溃为 `unknown`（C4／C5）。结果
  带 `status:"succeeded"|"failed"|"unknown"`，让 journal 的回执折叠成终态；服务端去掉这些簿记字段。
- 屏幕后端在操作系统边界之后（`screen.py` 的 `Backend`）：`screen_macos.py`（pyobjc：ScreenCaptureKit 截图、
  失败时 `CGWindowListCreateImage`；私有事件源 `CGEventPost`，每个事件带 `kCGEventSourceUserData` 标记；
  `CGSessionCopyCurrentDictionary` 判锁屏与控制台；`CGDisplayIsAsleep` 判休眠）、`screen_windows.py`
  （纯 ctypes：per-monitor-v2 DPI、`EnumDisplayMonitors`、GDI `StretchBlt` 截图并按来源尺寸缩放、GDI+ 编 JPEG、
  `SendInput` 带 `dwExtraInfo` 标记与 `MOUSEEVENTF_ABSOLUTE|VIRTUALDESK`、`OpenInputDesktop` 判安全桌面、目标
  窗口进程完整性级别判 UIPI）。依赖只加 darwin 标记的 `pyobjc-core`、`pyobjc-framework-Quartz`、
  `pyobjc-framework-ScreenCaptureKit`（13.x）；Windows 不需要 comtypes（UIA 属 3b）；不用 pyautogui／pynput。
  `build_runtime.py` 在 macOS／Windows 运行时上做 GUI 导入探针。Linux 与 macOS 14 以下没有后端：不声明
  `computer`，动作返回 `device_not_supported`。
- 坐标（M6）：`Display` 矩形在平台输入坐标中（macOS 全局点，Windows 物理像素），`frame()` 用与沙箱相同的
  `image_scale` 求图像尺寸，`to_global()` 在执行时按当下几何把图像像素换成全局坐标，Windows 再经
  `to_virtual_desk()` 归一化到 0–65535。不保存上一张截图。
- 截图字节（G1）：写入数据目录 `captures/`，结果只带路径；服务端用既有 `LocalDeviceService.download`
  （stat → 64 KiB 分块 read → release）取回，`FileTransfers` 在 release 时删除 `captures/` 中的文件；连接器启动时
  清空该目录。
- 阻塞调用（G3）：屏幕动作在 `asyncio.to_thread` 中按设备串行（一把锁）；浏览器有自己的线程与请求队列，空闲时
  `pump` CDP 事件；事件循环只等待它们的结果。

### 让出、叫停与恢复（§5.5 第 3–5 层）

本人输入不再叫停：任务书 §5.5 第 4 项把“第 3 层检测到本人输入”列为叫停来源，实机评审后本人决定改为
让出并重试（参照 cmux 的做法，manaflow-ai/cmux#36080）——本人随手动一下电脑，Yuki 只是这一步不做、重新看一眼
再做，不必每次都去聊天里说“继续”。叫停只剩 Desktop「停止」。

- **本人输入检测的选择**：两平台都用“最近一次输入时刻”与“Yuki 上一次看屏幕或输入之后的时刻”比较。
  macOS 对按键按下、左／右／其他鼠标键按下、三种拖拽与滚轮分别读
  `CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, 类型)`，取最近一次；只移动
  指针不算（本人把鼠标挪开不是在用电脑，D15）。用 CombinedSessionState 而不是任务书写的 HIDSystemState，因为
  前者包含 HID 之外投递到本登录会话的事件（远程控制工具常在会话层投递），而“本人输入”的定义正是“任何不是
  Yuki 产生的输入”；Yuki 自己的事件也会更新它，所以基准取在 Yuki 动作（含动作后截图）结束之后。Windows 读
  `GetLastInputInfo`（会话级，含远程桌面与所有 `SendInput`，指针移动也算）。没有选低级钩子：钩子需要常驻消息
  循环线程，超过 `LowLevelHooksTimeout` 会被系统静默摘除（检测从此静默失效，正是不能接受的失败方式），并给本人
  的每次按键增加延迟；macOS 的事件监听还需要第三项“输入监控”权限。Windows 区分指针移动需要钩子，推迟。代价：
  Yuki 自己正在投递事件的那几毫秒到几百毫秒内的本人输入，要到下一次动作才被发现。`dwExtraInfo`／
  `kCGEventSourceUserData` 标记照样打上，供观察者与日后需要时使用。
- 基准：Yuki 每次成功截图（含输入后附带的截图与 `resume`）之后、每次 `computer` 输入（无论成功或中途失败，只要
  投递过事件）之后、Desktop「繼續」之后（点「繼續」本身就是本人输入）。截图推进基准：模型按这张图决定下一步，
  只有图之后的输入才可能让图过时（D8 模型思考期间、D14 重试）。本进程第一次动作若是输入且之前没有截图，则没有
  可比较的基准、照常执行。
- 让出（D12–D14）：全局输入前发现基准之后有非 Yuki 输入时，不投递任何事件，等本人连续 1.5 秒没有输入
  （按后端报告的“距最近一次输入的秒数”判断，`idle()`），最多等 10 秒，然后一律拒绝：`owner_active`、
  `status:"failed"`，不打 `activity` 行，holder 不变、不打 `control` 行（D11）。等到本人停手也不执行，因为画面
  可能已被本人改变，旧图上的坐标不再可信；消息让模型先重新截图，按新图重试。不重新截图直接重试会再次立即
  被拒绝。1.5 秒与 10 秒是本人批准的交互参数，不是正确性门槛：等多久都不会让旧坐标变得可信，拒绝才是关键。
  截图与其他读动作从不等待。`browser` 不让出：它经 CDP 向 Yuki 专用 Chrome 投递，不碰光标与本人的窗口。
- 错误码选择：新增 `owner_active`，没有复用 `control_held`。`control_held` 在沙箱与本人电脑上都表示“本人持有
  控制，等本人交还或说继续”，模型对它的正确反应是停下、等 `resume`；让出要求的反应相反——立刻重新截图并重试，
  不需要任何人许可。合用一个码会让模型在让出后去调 `resume` 或在叫停时不停重试。`owner_active` 在
  `computer` 工具说明、[本机连接器](local-agent-connector.zh-CN.md#屏幕与-yuki-专用浏览器)失败表与本记录中声明，
  服务端原样透传连接器的错误码。
- 时钟：只在后端自己的时钟里比较“最近一次输入时刻”，不换算到其他时钟。Windows 返回 `GetLastInputInfo` 的
  32 位 tick 按一次 `GetTickCount64` 展开后的精确值；macOS 返回区间：调用前后各读一次 `time.monotonic()`，
  减去“已过秒数”。基准取区间上界，检查用区间下界，下界晚于基准才算本人输入——不用经验阈值。Windows CI 首轮
  实测了换算的问题：Python 3.12 的 `time.monotonic()`／`time.time()` 与 `GetTickCount` 在 Windows 上都是约
  15.6 ms 一跳，换算后 Yuki 自己刚投递的输入会被算成晚于基准而误判。
- 叫停（D9）：只有 Desktop「停止」（stdin `holder:"owner"`）置叫停。叫停后 `computer` 的所有输入、`browser` 的
  所有输入（含每次 CDP 输入前的复核，长输入中途也停）都是 `control_held`；`type` 按 32 字符一段投递，段间复核。
  读动作照常。恢复只有 Desktop「繼續」与 `computer(action=resume, device)`。
- 状态只在内存（G4）；Desktop 在拉起子进程后重发（D10）。

### Yuki 专用 Chrome（§5.8）

见[本机连接器 · 屏幕与 Yuki 专用浏览器](local-agent-connector.zh-CN.md#屏幕与-yuki-专用浏览器)。实施补充：
连接后 `Browser.setDownloadBehavior` 指向数据目录 `downloads/`；Windows 若所在 Job 不允许脱离
（`CREATE_BREAKAWAY_FROM_JOB` 得到 `PermissionError`），记录警告后在同一 Job 中启动——Desktop（libuv）为子进程建的
Job 允许脱离，此分支只在其他宿主中出现。为了让 B8 在 CI 三平台都用真实 Chrome 验证，Linux 也按 `PATH` 找 Chrome
（连接器在 Linux 仍不做屏幕）。`browser` 工具说明中的“下载保存到 /workspace/downloads”属阶段 1 冻结的合同文字，
本人电脑上的下载路径以 `download` 事件的 `path` 为准。Windows CI 实测补充：Chrome 被杀后连接被重置，关闭 CDP 连接时发送关闭帧会抛出 `OSError`，
曾让浏览器线程退出、之后每个浏览器动作都挂起；现在丢弃连接时不让关闭帧的失败逃出，空闲 `pump` 的任何异常都只
记日志并丢弃连接，下一次动作重连或重新打开 Chrome。GitHub Windows runner 所在 Job 不允许脱离
（日志 `Chrome could not leave the Connector's job object`），Chrome 仍在连接器被杀后存活（该 Job 没有
`KILL_ON_JOB_CLOSE`）；Desktop 子进程中的行为待实机。

### `window` 与 `device`（3a）

选 `device_not_supported`：冻结列表里只有它说的是“这台设备不支持这项能力”。`window_not_found` 会谎称窗口不存在，
`window_unavailable` 的含义是窗口最小化；截整屏或全局输入冒充窗口操作更不可接受。`screenshot` 的 `windows`
返回空列表，3b 填入。

### 服务端

- `LocalDeviceService.status` 透出 `computer`；连接器 hello／catalog 的 `device_capabilities` 原样进入
  Client API 设备列表。
- `LocalDeviceService.screen_action`：只用既有 `connection()`（设备归属本人）核验；命令 ID 是调用键；参数加
  `owner`（Work，用于标签页归属）与部署的 `vision_max_dimension`／`vision_max_pixels`。离线或发出前断开为
  `device_offline`；发出后断开时输入加 `status:"unknown"`；连接器的拒绝（`full_access_disabled`、
  `invalid_resource` → `device_not_supported`）原样或映射返回；journal 中只有待定的回执为 `action_unknown`；
  截图取回失败只加 `media_error`。`agent_tools._screen_action` 两条路共用同一尾部：应有图像而没有时
  `media_read:false`（D6）。

## 3. 验证

### 失败用例与测试

| 用例 | 测试 |
| --- | --- |
| D1 | `test_desk_e2e.py::test_desktop_contract…`（预检为假 → 两种 `*_permission_required`、新 `permissions` 行、catalog 更新）；`test_screen_native.py`（本机 macOS 与 CI macOS：真实预检，无授权时为 `screen_permission_required`） |
| D2 | `test_desk_e2e.py`（`display_locked` 截图与输入均拒绝，不投递）；`test_screen_native.py`（真实 `unavailable()` 与截图结果一致） |
| D3 | `test_desk_e2e.py`（`target_elevated`，不投递、无 activity 行） |
| D4 | `test_device_computer_e2e.py`（`get_environment(device)` 的 `computer`；旧连接器 → `device_not_supported`，无屏幕后端不声明 `computer`）；`test_desk_e2e.py`（hello 中的 facts） |
| D5 | `test_desk_e2e.py` 与 `test_device_computer_e2e.py`（关闭后截图、浏览器读都为 `full_access_disabled`） |
| D6 | `test_device_computer_e2e.py`（同键重放时截图已释放 → `media_read:false` 且 `submitted`；无归档能力的结果预算 → `media_read:false`，点击保留） |
| D8 | `test_desk_e2e.py::test_desktop_contract…`（连续两次 Yuki 输入照常；截图后本人输入 → 下一次输入让出；普通 `run` 同样让出）；`test_device_computer_e2e.py`（经模型工具） |
| D9 | `test_desk_e2e.py`（Desktop「停止」时 `computer`、`browser` 输入 `control_held`，截图与浏览器读不拒绝；Desktop「繼續」与 `resume` 恢复）；`test_device_computer_e2e.py`（经模型工具：「停止」后输入 `control_held`、他人 `resume` → `not_found` 叫停保持、本人 `resume` 恢复） |
| D11 | `test_desk_e2e.py`（让出前后 holder 仍为 `none`，除启动行外没有 `control` 行，也没有 `activity` 行）；`test_device_computer_e2e.py`（stdout 只有启动时的 `control` 行） |
| D12 | `test_desk_e2e.py`（本人输入后点击约 1.5 秒后返回 `owner_active`、`status:"failed"`，事件为零；不截图直接重试立即再拒绝）；`test_device_computer_e2e.py`（经模型工具：`owner_active`，非 `uncertain`、非已提交变更） |
| D13 | `test_desk_e2e.py::test_owner_who_keeps_using_the_computer…`（本人每 0.2 秒输入一次，`type` 在约 10 秒时 `owner_active`，事件为零；产物 `owner-busy-*.json`） |
| D14 | `test_desk_e2e.py`（本人仍在输入时截图立即返回；截图后重试点击执行；`browser` 输入不让出）；`test_device_computer_e2e.py`（重新截图后重试成功） |
| D15 | `test_screen_native.py::test_macos_counts_only_deliberate_input`（macOS：指针移动、松键、修饰键变化、松开鼠标不算；八种有意输入各自算；取最近一次；只读 CombinedSessionState）；`test_screen_native.py::test_native_backend…`（真实 `last_input()`／`idle()` 在本机与 CI macOS、Windows 上可读） |
| D10 | `test_desk_e2e.py::test_restart_keeps_the_desktop_stop…` |
| M6 | `test_screen_geometry.py`（Retina、副屏负坐标、150 % DPI、虚拟桌面归一化、当下几何）；`test_desk_e2e.py`（进程级：所附图像坐标 → 全局点、左上副屏）；`test_device_computer_e2e.py`（经服务端）；`test_screen_native.py`（CI Windows：真实 `SendInput` 移动后 `GetCursorPos` 与换算一致） |
| B8 | `test_chrome_e2e.py`（真实 Chrome：私有 profile 与全部参数、`DevToolsActivePort`、下载在 `downloads/`、截图经文件循环、连接器被杀后 Chrome 存活、下一个连接器连已有实例不再启动、本人退出后重新打开）；`test_desk_e2e.py`（找不到 Chrome → `browser_unavailable`） |
| C4／C5 | `test_desk_e2e.py`（同键返回原回执不再投递；执行中被杀 → 重启后同键 `unknown`，事件为零）；`test_device_computer_e2e.py`（经服务端：断开时 `device_offline`+`unknown`，重启后 `action_unknown`） |
| K1–K5 | `test_desk_e2e.py`（启动行、stdin 控制行与无关行、activity 只在真正执行时、permissions 变化、普通 `run` 不打行、EOF 退出码 0） |
| G1–G4 | `test_desk_e2e.py`、`test_device_computer_e2e.py`（截图释放即删除；设备离线与非归属人；叫停不进配置） |

真实连接器进程 + 伪屏幕：`packages/yuki-agent-connector/tests/fakes/screen_connector.py` 以真实 CLI 入口运行，
只替换操作系统屏幕（记录投递的事件、按测试脚本报告权限／锁屏／提升权限／本人输入）。产物：
`test-results/device-computer/`（`desktop-contract-*.json`、`restart-*.json`、`plain-run-*.json`、
`owner-busy-*.json`、`native-*.json(.jpg)`、`chrome-*.json`、`server-computer.json`、`server-old-connector.json`），CI 连接器三平台 job
上传为 `device-computer-<os>`，Bot 集成套件随 `tests-integration` 上传。

### 待实机（签名版 Desktop，Mac mini 与 Windows）

- macOS：授权前后 `permissions` 行与 `screen_permission_required`／`input_permission_required`；授予后真实截图、
  点击、输入、滚动、拖拽、组合键与 Retina／副屏坐标；锁屏与显示器休眠；`launchctl procinfo` 证明负责进程为
  Yuki.app；ScreenCaptureKit 在无 NSApplication 的子进程中可用（否则走 `CGWindowListCreateImage` 后备，日志可见）；
  pyobjc 回调在 hardened runtime 下是否需要 `allow-unsigned-executable-memory`；屏幕录制授权后预检是否要重启
  子进程才变为真（TCC 的已知行为，影响 `permissions` 行的及时性）。
- 让出：本人按键、点击、拖拽、滚动，屏幕共享（Screen Sharing.app）与远程控制工具的输入是否都推进
  CombinedSessionState 中对应类型的最近输入时刻；只挪鼠标不让出；Yuki 自己的事件不被误判；本人停手后 Yuki
  重新截图并重试的体验（等待 1.5 秒、最长 10 秒）是否可接受。Windows 上本人只挪鼠标也会让出（钩子推迟）。
- Windows：多屏与 125 %／150 % 缩放下的坐标；锁屏与 UAC 为 `desktop_unavailable`；以管理员运行的窗口为
  `target_elevated`；远程桌面输入被识别；Desktop 子进程启动的 Chrome 能脱离 Job 并在连接器重启后存活。
- 专用 Chrome：被遮挡与在另一桌面空间时截图不停；首次启动抢焦点一次；macOS 上 Chrome 脱离连接器进程组。
