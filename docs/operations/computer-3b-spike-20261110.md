# 本人电脑按窗口后台操作（阶段 3b spike，2026-11-10）

依据[统一 Computer Use 与 Browser Use 任务书](../architecture/Yuki-统一ComputerUse与BrowserUse任务书-2026-11-09.md)
§5.5 第 2 层（标 [spike]）、§5.7，以及[阶段 3a 记录](computer-browser-device-3a-20261110.md)（3a 中 `window` 加
`device` 返回 `device_not_supported`；本人输入检测按类型读取 `CGEventSourceSecondsSinceLastEventType(CombinedSessionState)`）。

**本次没有修改产品代码。** 只新增本记录、证据目录
[`evidence/computer-3b-spike-20261110/`](evidence/computer-3b-spike-20261110/)（macOS 实验脚本、测试宿主、
9 张代表性截图共 176 KB）和 Windows 待跑脚本
[`computer_3b_windows.py`](evidence/computer-3b-spike-20261110/computer_3b_windows.py)。

**结论**：macOS 上，可以在后台操作被遮挡的原生窗口：只截这个窗口，不移动本人光标，不抢焦点，也不提升窗口层级。
四类操作各自的情况：

- **截图**：用 ScreenCaptureKit 的单窗口过滤器截取。
- **控件**：在**目标窗口自己的** AX 子树中按坐标找到元素，用 `AXPress`、设置 `AXValue`、`AXFocused` 或
  `AXScrollToVisible` 操作。AppKit、SwiftUI、Electron、Chrome、WKWebView 都通过。
- **键盘**：用 `CGEventPostToPid` 直接投递即可。
- **点击与滚轮**：普通的 `CGEventPostToPid` 一律被丢弃，只有按 Cua 的做法给事件补上窗口字段（下称“盖章”）
  才能送达。WKWebView 的像素点击走任何后台途径都送不到。

这些后台事件不会推进 3a 本人输入检测读取的计时。Windows 本机没有机器，只给出实验计划和待跑脚本。

## 1. 失败情况与风险（先列）

每条写明实测到的错误行为或风险，括号内是对 3b 设计的要求。

- **R1 进程直接中止。** 在任何 CoreGraphics 调用之前创建 `SCContentFilter(desktopIndependentWindow:)`，进程会以
  `Assertion failed: (did_initialize), function CGS_REQUIRE_INIT` 中止。这是 abort，Python 无法捕获，连接器整个退出，
  正在执行的输入只能落成 C5 的 `unknown`。3a 的整屏路径碰巧先调用了 `CGGetActiveDisplayList`，所以没有触发。
  （后端构造时先调用一次 `CGMainDisplayID()`；见 `macos/capture.py` 开头。）
- **R2 系统级命中测试点错 App。** 系统级 `AXUIElementCopyElementAtPosition` 返回的是最上层窗口的元素：
  - 在被遮挡的 AppKit 窗口上，它返回遮挡它的另一个窗口；
  - 在被遮挡的 WKWebView 和 Electron 窗口上，它返回上层窗口的关闭按钮；
  - 在被遮挡的 Chrome 窗口上，它返回**另一个 App**（WKWebView 宿主）里同名的 “Press me” 按钮。

  按它操作会点到别的 App。（只在目标窗口自己的子树里找：遍历该窗口元素，取包含该点的最小 frame，不用任何命中测试。）
- **R3 WKWebView 的网页点击没有后台途径。** 普通 post-to-pid、补窗口字段、盖章三种方式都送不到，与
  cmux #36068 的结论一致；滚轮和键盘可以送到。Safari 很可能相同，未测。
  （网页内容先走 AX；AX 也找不到元素时不得回执“已执行”，见 §6 的失败处理。）
- **R4 最小化窗口的截图可能是旧画面。** AppKit 宿主最小化后截到的是新画面；Chrome 最小化后截到的是旧画面：
  标题已变成 `P3`，图里仍是 `P2`，即使带着三项防节流参数也一样（`images/chrome-min2-sck.png`）。
  （最小化仍返回 `window_unavailable`，以 `AXMinimized` 为准，不靠图像判断。）
- **R5 普通 `CGEventPostToPid` 静默丢弃点击和滚轮。** 在 AppKit、SwiftUI、Electron、Chrome、WKWebView 上都是 0 送达，
  只补 91/92 两个窗口字段也一样，调用本身照常返回。只有盖章做法能送达，而盖章依赖未公开的字段号和
  `CGEventSetWindowLocation`（SPI）。Cua 记录 macOS 27 把窗口位置改成了窗口内坐标。
  （回执只写 `submitted`，以截图核实；系统升级后需要回归，见 §6。）
- **R6 键盘事件不按坐标找窗口。** 键盘事件交给 App 当前的焦点元素。AppKit 宿主有两个窗口时，按键落在最后被点击的
  窗口 A，而 A 并不是 key window。（输入前先对目标输入框设置 `AXFocused`；实测不会激活 App。）
- **R7 后台动作对本人输入检测完全不可见，反过来也一样。** 56 次 AX 与 post-to-pid 动作都没有推进 Combined 或
  HIDSystem 的任何类型计时。好处是不会被误判为本人输入；坏处是本人正在同一个 App 里打字时，Yuki 的按键会和本人的
  交错。（需要“冲突”规则，见 §6 与 §8 第 3 问。）
- **R8 有两个动作会抢焦点。**
  - 让 App 自己取消最小化（`deminiaturize:`）会激活该 App。本次实验中测试宿主因此被激活约 1.5 秒，杀掉宿主后焦点回到
    本人原来的 App。
  - 首次启动 Chrome 即使用 `open -g` 也会激活自己（Cua 的 `focus_steal.rs` 记录过同样的行为）。本次随后把本人原来的
    App 激活回前台。

  （3b 的窗口动作不包含还原或启动；最小化时直接返回 `window_unavailable`。）
- **R9 系统会弹出捕获确认。** 首次用 ScreenCaptureKit 时，即使 `CGPreflightScreenCaptureAccess()` 为真，系统仍弹出
  “Terminal is requesting to bypass the system private window picker and directly access your screen and audio”
  （`images/unc-dialog-sck.png`）。弹窗挂着时截图照常成功。Yuki.app 也会遇到，macOS 15 起会定期再确认。
  （Desktop 的权限说明要提到这一项；连接器不能代替本人点它。）
- **R10 自绘控件没有 AX 元素。** 自绘 `NSView`（测试宿主的黄色区域）只暴露所在的 `AXWindow`。画布、游戏同理，只能用
  盖章点击，结果靠截图判断。
- **R11 合成按键会被系统改写。** WKWebView 输入框收到合成按键 `abcd` 后，系统文字替换把它改成了 `Abcd`
  （`images/web-after-sck.png`）。（输入文本优先设置 `AXValue`；按键输入不保证逐字。）
- **R12 Chromium 的 AX 树是异步建立的。** 对 Electron 设置 `AXManualAccessibility` 后立即读取，只有窗口框架的 12 个
  元素；约 1 秒后才有 419 个。这会让本人的 Electron 和 Chrome App 进入完整无障碍模式，有 CPU 和内存开销。
  （开启后等待并重读；Yuki 设置的这个属性在整个会话期间都保留。）
- **R13 本机没有测到的条件。**
  - 本机只有一块 1920×1080、1 倍缩放的显示器，Retina 和多显示器坐标没有实测。
  - 只测了 SLS 私有调用造成的“非可见 Space”，没有测真实的 Mission Control 桌面。Cua 的 CUA-1351 记录过，真实的另一
    Space 上 ScreenCaptureKit 返回空内容。
  - Catalyst App 未测。

## 2. 环境与方法

- **机器**：Mac mini，macOS 27.0（26A425），Apple Silicon，单显示器 1920×1080、1 倍缩放。
- **权限**：本人把屏幕录制和辅助功能授予 Terminal.app，由 Terminal.app 作为负责进程。授权之前已实测：没有辅助功能时，
  `CGPreflightPostEventAccess()` 为假，post-to-pid 0 送达。
- **运行方式**：Python 3.12 加 pyobjc 13.0。所有脚本都是普通子进程，**没有 NSApplication，也没有 run loop**，与连接器
  相同。
- **目标窗口**：全部由实验自己打开，不触碰本人的 App。前台 App 始终是本人正在使用的 Alma。
  - 测试宿主 `hosts/host_appkit.py`：AppKit 窗口 A、B，内含 NSButton、NSTextField、NSScrollView 和一个自绘区域；
    加 `--web` 时改为 WKWebView。宿主把收到的每个事件和激活变化写入日志，作为“是否真的送达”的依据。
  - Electron 42.11.15 宿主 `hosts/eapp`，两个窗口，用 `showInactive` 显示。
  - Chrome 157.0.8103.50：临时 profile，带 `--force-renderer-accessibility` 和 3a 的三项防节流参数。
  - Calculator（SwiftUI）。
  - 网页测试页 `hosts/page.html` 把点击、按键、滚轮次数和输入值写进标题。
- **遮挡**：用一个忽略鼠标的浮动遮挡窗口（`hosts/occluder.py`），再让同一 App 的另一个窗口压在目标上面。
- **每个动作前后测量**：
  - `CGEventGetLocation` 光标位置；
  - `lsappinfo front` 前台 App；
  - 目标窗口在屏幕窗口中的层级；
  - Combined 与 HIDSystem 两种状态下 7 种事件类型的“距今秒数”；
  - 耗时。

  结果写在 `out/*.json`（草稿目录）。全部 56 个动作的汇总见
  [`summary-idle.txt`](evidence/computer-3b-spike-20261110/summary-idle.txt)。
- **复现**：在证据目录的 `macos/` 下执行 `uv venv` 并安装
  `pyobjc-framework-{Quartz,ScreenCaptureKit,ApplicationServices,Cocoa,WebKit}`，按各脚本 docstring 运行。提交前已用
  提交版本重跑了一遍冒烟：启动宿主、截窗口、`AXPress`、盖章点击，全部通过，光标和前台都没变。

## 3. macOS 结果

### Q1 窗口截图

用 `SCShareableContent(onScreenWindowsOnly:false)` 取到窗口，再用 `SCContentFilter(desktopIndependentWindow:)`
加 `SCScreenshotManager` 截图。完成回调在 ScreenCaptureKit 自己的队列上执行，所以普通进程用 `threading.Event`
等待即可，**不需要 run loop**（前提是先满足 R1）。取窗口列表约 25 ms，截图约 43–75 ms。

| 窗口状态 | ScreenCaptureKit | `CGWindowListCreateImage` |
| --- | --- | --- |
| 可见 | 完整 | 完整 |
| 被遮挡（AppKit、WKWebView、Electron、Chrome、SwiftUI） | 完整、实时（`images/appkitA-occluded-sck.png`、`electronA-occluded-sck.png`） | 与左边逐像素一致 |
| 大部分移出屏幕（AppKit 会约束 `setFrameOrigin`，只剩 40 px 在屏上） | 完整 | 完整 |
| 隐藏 App（`NSApp hide`） | 完整 | 无图（返回 None） |
| 不可见 Space（SLS 私有调用把宿主窗口移到新 Space，期间修改按钮文字） | 完整、实时（`images/appkitA-otherspace-sck.png`） | 完整 |
| 最小化 | AppKit 实时（`appkitB-mini-changed-sck.png`）；Chrome 是旧画面（R4） | 无图 |

- **`CGWindowListCreateImage` 在 macOS 27 上仍能截窗口。** 有论坛报告说 macOS 27 beta 4 起它返回灰图，本机没有复现。
  它是废弃 API，3b 不用它截窗口，主路径只用 ScreenCaptureKit。
- **坐标换算。**
  - ScreenCaptureKit 的 `SCWindow.frame` 与 CGWindowList 的 bounds 相同：左上原点、单位为点、包含标题栏。
  - 设置 `ignoreShadowsSingleWindow=true` 后，图像尺寸等于 frame × `pointPixelScale`（本机为 1）。
  - 换算公式：全局点 = bounds.origin + 图像像素 × bounds.width / 图像宽度。§5.2 的缩放只改变图像宽度，公式不变。
  - 实测吻合：
    - 对窗口内图像像素 (200, 294) 盖章点击，AppKit 收到的 `locationInWindow` 为 (200, 54)；窗口高 348，
      348 − 294 = 54，吻合。
    - Electron 页面收到的 `clientX/Y` 为 (50, 23)，等于全局点减去 Web 区域原点。
  - Retina 未实测（R13），按 `pointPixelScale` 换算。

### Q2 AX 动作（目标窗口不在前台且被遮挡）

用 `_AXUIElementGetWindow`（HIServices 私有函数，Cua、Hammerspoon、yabai 都在用）从 App 的 `AXWindows` 中找出
CGWindowID 对应的 AX 窗口，再在它自己的子树里按坐标找元素。WebKit 的输入框里还有一层更小的编辑组，命中后要上溯到
`AXTextField`。

- 下表各项光标都没有移动，前台 App 都没有变，窗口层级都没有变（全部 56 个动作见
  [`summary-idle.txt`](evidence/computer-3b-spike-20261110/summary-idle.txt)）。
- 表中的“✓”以宿主日志、页面事件或截图为准，不以 AX 返回码为准。

| App 类型 | `AXPress` | 设置 `AXValue` | 滚动 | `AXFocused` |
| --- | --- | --- | --- | --- |
| AppKit（NSButton、NSTextField、NSScrollView） | ✓ 触发按钮动作，App 保持非激活 | ✓ | ✓ 设置滚动条的 `AXValue`；滚动区域另有 `AXScrollDownByPage` 等动作 | ✓ 不激活 |
| SwiftUI（Calculator） | ✓ 显示从 0 变为 7 | — | — | — |
| Electron 42 | ✓ 页面收到可信的 `mousedown`/`click`，且 `document.hasFocus()` 为假 | ✓ 触发 `input` 事件 | ✓ `AXScrollToVisible`，`scrollTop` 从 0 变为 2933 | ✓ |
| WKWebView | ✓ | ✓ 触发 `input` 事件 | ✓ `AXScrollToVisible` | ✓ |
| Chrome 157 | ✓ | ✓ | ✓ `AXScrollToVisible` | ✓ |
| 最小化的 AppKit 窗口 | ✓ 窗口仍保持最小化，App 不激活 | — | — | — |
| 自绘 NSView | 无 AX 元素（R10） | — | — | — |

**耗时**：AppKit 的 `AXPress` 首次约 100 ms，其他动作都在 3 ms 以内。

**命中测试对比**：

| 被遮挡的目标 | 只在窗口自己的子树中找 | App 级 `AXUIElementCopyElementAtPosition` | 系统级 |
| --- | --- | --- | --- |
| AppKit | 正确 | 遮挡它的同 App 窗口 B | 窗口 B |
| Electron、WKWebView | 正确 | 正确（Chromium 和 WebKit 自己做命中） | 上层窗口的关闭按钮 |
| Chrome | 正确 | 正确 | **另一个 App 的按钮**（R2） |

### Q3 `CGEventPostToPid` 投递给后台 App

| 投递方式 | 点击 | 键盘（先设 `AXFocused`） | 滚轮 |
| --- | --- | --- | --- |
| 没有辅助功能权限 | 0 送达，静默 | — | — |
| 普通 post-to-pid，或另补 91/92 两个窗口字段 | 0 送达（AppKit、SwiftUI、Electron、Chrome、WKWebView） | ✓ AppKit、SwiftUI、Electron、Chrome、WKWebView | 0 送达 |
| 盖章 post-to-pid（Cua 做法，见下） | ✓ AppKit 自绘区域（送达被遮挡的窗口 A，而不是上层的 B）、NSButton、SwiftUI、Electron、Chrome；**WKWebView ✗**；最小化窗口 ✗ | ✓（同普通） | ✓ AppKit、Electron、Chrome、WKWebView |
| 盖章后改走 SkyLight 的 `SLEventPostToPid`（只测了一次） | AppKit 0 送达 | — | — |

- **盖章的做法**：
  1. 先发一个 mouseMoved 作为引子；
  2. 每个鼠标事件设置字段 7=3、51=窗口号、58=1、91 和 92=窗口号、40=目标 pid，并用 `CGEventSetWindowLocation`
     写入窗口内坐标；
  3. 只用 `CGEventPostToPid` 投递**一次**。

  每个动作只送达一次，没有出现 cmux 那种因 SkyLight 和 CG 各投一次而重复的问题。一次点击耗时约 65 ms，其中包括
  30 ms 引子和 30 ms 的按下到抬起间隔。
- **结果与 cmux #36068 的公开数据一致**：普通 post-to-pid 不能送达点击和滚轮，键盘可以；盖章做法能送达 AppKit 和
  Electron 的点击与滚轮；WKWebView 没有后台点击途径。本次还补充验证了 Chrome 157 与 SwiftUI。
- **光标与焦点**：所有投递中，光标、前台 App、窗口层级都没有变。网页收到的事件 `isTrusted` 为真。

### Q4 与 3a 本人输入检测的关系

在全部 56 个 AX 与 post-to-pid 动作的前后，读取了 `CGEventSourceSecondsSinceLastEventType` 在 Combined 和 HIDSystem
两种状态下的 7 种类型：按键按下、左键、右键、其他键、左键拖拽、滚轮、指针移动。结果没有一次被重置，各计时都随时间正常增长。

**阳性对照**：同一段时间里本人在 Alma 中打字，`keyDown` 读数降到 0.15 s，说明检测本身是灵敏的。3a 已记录过，Yuki
用 `CGEventPost` 发出的全局事件会推进这些计时。

**结论**：第 2 层事件既不会被 3a 当成本人输入，也不会推进 3a 的基准。代价见 R7：本人的输入同样不会挡住第 2 层，所以
第 2 层需要另立“冲突”规则，见 §6。

### Q5 权限

| 方法 | 所需权限 |
| --- | --- |
| ScreenCaptureKit 截窗口 | 屏幕录制，加上 R9 的捕获确认 |
| 读取 AX 树及 `AXPress`、`AXValue`、`AXFocused` | 辅助功能（`AXIsProcessTrusted`） |
| `CGEventPostToPid`（普通或盖章） | 辅助功能（`CGPreflightPostEventAccess`）；没有权限时静默丢弃 |
| 输入监控 | **不需要**：`CGPreflightListenEventAccess()` 全程为假，所有方法照常工作 |

用到的私有接口有 `_AXUIElementGetWindow`、`CGEventSetWindowLocation`、事件字段 7/40/51/58/91/92。都不需要
entitlement，但都可能随系统版本变化。用 pyobjc 的 `loadBundleFunctions` 加载它们，在 Yuki.app 的 hardened runtime
下是否需要 `allow-unsigned-executable-memory`，与 3a 的同类问题一起待实机确认。

## 4. Windows（本次没有机器）

[`computer_3b_windows.py`](evidence/computer-3b-spike-20261110/computer_3b_windows.py) 已就绪：

- 本人在普通（非管理员）终端里运行
  `uv run --no-project --python 3.12 --with comtypes python computer_3b_windows.py <输出目录>`，约 1 分钟，期间不要动电脑。
- 结果写入 `result.json` 和 BMP 图片。
- 只操作脚本自己打开的窗口：临时文件上的 Notepad、Calculator，以及临时 profile 的 Chrome 或 Edge 测试页，另加一个
  topmost 遮挡窗口。
- Notepad 或 Calculator 已经打开时跳过该项，因为 Windows 11 会复用本人的窗口。
- 结束时只终止它自己拉起的进程。

脚本已通过本仓库 ruff，但从未在 Windows 上运行过，首次运行可能需要小修。

| 问题 | 做法 | 测量 |
| --- | --- | --- |
| W1 截图 | 对可见、被 topmost 窗口遮挡、移到屏幕外（x=−4000）、`SW_SHOWMINNOACTIVE` 最小化四种状态，分别比较 `PrintWindow(PW_RENDERFULLCONTENT)`、`PrintWindow(0)`、屏幕 DC `BitBlt` | 每张图的黑像素比例、耗时、尺寸 |
| W2 坐标 | `GetWindowRect`、`DWMWA_EXTENDED_FRAME_BOUNDS`、客户区原点、`GetDpiForWindow`、`DWMWA_CLOAKED` | 用于写换算公式；100%、125%、150% 缩放各跑一次 |
| W3 UIA | `ElementFromHandle(hwnd)` 后，用 RawViewWalker 在该窗口子树里取包含该点的最小元素；与 `IUIAutomation::ElementFromPoint` 对照。Calculator 的数字按钮用 `Invoke`；Notepad 文档用 `Value.SetValue`；网页按钮用 `Invoke`，输入框用 `Value`，列表用 `Scroll.SetScrollPercent`。最小化后再做一遍 | 每个动作前后的 `GetCursorPos`、`GetForegroundWindow`、目标窗口层级、`GetLastInputInfo`，效果由 UIA 读回或窗口标题确认 |

启动目标后，脚本先用 `SetForegroundWindow` 把前台交还给终端，再开始测量，结果记录在 `*_launch.after_handback`。
如果系统不允许交还，结果里会看到前台仍是目标窗口，这一项的焦点结论就要另做。

**需要在 Windows 实机上确认的**：

- `PW_RENDERFULLCONTENT` 对被遮挡、GPU 合成（Chromium、WinUI Calculator）和移出屏幕的窗口是否都不是黑图；
  最小化窗口预期是空白或只有标题栏。
- UIA 的 `Invoke`、`SetValue`、`SetScrollPercent` 是否会把窗口带到前台、移动光标，或推进 `GetLastInputInfo`。若推进，
  会与 3a 的让出规则互相干扰。
- Chrome 不带 `--force-renderer-accessibility` 时，是否在 UIA 客户端出现时自动开启无障碍。
- 另一个虚拟桌面上的窗口（`DWMWA_CLOAKED`）能否截图和操作。
- 目标是管理员权限窗口时，UIA 是否同样被 UIPI 拒绝（应返回 `target_elevated`）。

UFO2 不是后台先例：它的 `ControlReceiver` 总会先 `set_focus()`，默认用 `click_input` 和 `type_keys`。它的截图后备
`_win32_print_window` 用的正是 `PW_RENDERFULLCONTENT`，注释写明“RDP-safe”，适用于被遮挡的窗口。

## 5. 先例（研究子任务读码所得，2026-11-10）

### cmux（manaflow-ai/cmux）

- **#36068（`b1b4e240`）**：做了输入注入 A/B 评测，脚本在 `scripts/measure/cu-driver-eval/`，对比了 `inject.swift`
  中的 cg-pid、sl-pid、session-tap、nsevent-pid、stamped-pid 几种做法。评测结果放在私有工单里；公开数字只见于 #36080
  的评论（macOS 26.5.2 虚拟机，每格 3 次），结论与本次的 Q3 一致。
- **#36080（`eadfa248`）**：本人接管检测。只监听的 session tap 加上 `eventSourceUserData` 标记识别自己的事件，
  1.5 秒静止后恢复，10 秒拒绝。它需要辅助功能权限；监听按键还需要输入监控权限。
- **#36377（`555b1cbf`）**：后台调用只在真正冲突时暂停。冲突的定义是：
  - 目标是本人的前台 App，或本人指针下的 App 或窗口；
  - 调用走前台、整屏途径；
  - 调用没有指定 App；
  - 调用会改变焦点。

### Cua（trycua/cua，`libs/cua-driver/rust/crates/platform-macos/src`，main `7ab7fe8b`）

- **盖章**：`input/mouse.rs` 的 `post_mouse_event_with_mode` 给事件盖章；第 437–441 行说明，没有 mouseMoved 引子时，
  后台 AppKit 控件会静默忽略按下事件。第 578–589 行记录了 macOS 27 起改用窗口内坐标。
- **按键认证**：`input/keyboard.rs` 说明，macOS 14+ 的 VS Code、Chrome、Electron 需要用 `SLEventSetAuthenticationMessage`
  给按键加认证。本次 Electron 42 和 Chrome 157 不加认证也收到了普通的 post-to-pid 按键，原因不明，3b 实施时回归验证。
- **窗口范围的元素查找**：用 `_AXUIElementGetWindow` 限定在单个窗口内（`ax/window_scope.rs`）。
- **Chromium 无障碍**：用 `AXManualAccessibility` 开启，等待最多 4 秒（`ax/enablement.rs`）。
- **截图**：截单个窗口；另一 Space 的窗口改用私有的 `SLSHWCaptureWindowList`（`capture.rs`，`skylight.rs` 第 822–890 行）。
- **工具说明**：`click` 工具优先走 AX，说明中写它对后台、最小化、另一 Space 的窗口都有效。

## 6. 3b 设计建议

**macOS（连接器 `screen_macos.py`）**：

- **截图** `computer(action=screenshot, window, device)`：
  - 窗口列表来自 `SCShareableContent(onScreenWindowsOnly:false)`，只取 layer 0、有标题、不属于连接器自己的窗口，返回
    `windows:[{id,app,title}]`；`id` 是 CGWindowID 的字符串。
  - 截图用单窗口过滤器加 `ignoreShadowsSingleWindow`，按 §5.2 在来源处产出最终尺寸。
  - 后端初始化时先调用 `CGMainDisplayID()`（R1）。
  - id 不存在或进程已退出时返回 `window_not_found`；`AXMinimized` 为真时返回 `window_unavailable`（R4）。
  - 隐藏 App 和另一 Space 的窗口照常截图。真实 Space 待实机（R13），如果截到空图，返回 `window_unavailable`，不靠
    像素判断。
- **动作**：执行时按当下的 bounds 换算窗口图像坐标，换算方法同 Q1。

  | 动作 | 做法 |
  | --- | --- |
  | `click` | 先在窗口子树中命中元素（R2）：<br>• 输入框类（`AXTextField`、`AXTextArea`、`AXComboBox`、`AXSearchField`）设 `AXFocused`；<br>• 其他有 `AXPress` 的元素用 `AXPress`；<br>• 两者都不是时，用盖章 post-to-pid 点击一次。 |
  | `type`、`key` | 普通 `CGEventPostToPid` 按键，投给该窗口所属 pid，落在 App 当前焦点（R6）。 |
  | `scroll` | 盖章滚轮。 |
  | `double_click`、`right_click`、`move`、`drag` | 本次没有实测，3b 实施时先用盖章事件验证，不通过就在窗口模式下返回 `device_not_supported` 并说明原因。 |

- **回执**：一律 `{submitted:true}`，附动作后的窗口截图。AX 返回码为 0 不代表生效：AppKit 的 `AXPress` 只说明动作已
  分派，WKWebView 的盖章点击调用也照常返回。不新增“已核实”的回执。
- **后台不送达的情况**：WKWebView 的像素点击（R3）、只能盖章点击的自绘控件，都可能没有效果。3b 不自动退回第 3 层
  前台操作，由模型看截图后自行决定省略 `window` 改用整屏；第 3 层仍受 `owner_active` 让出规则约束。
- **本人输入与叫停**：
  - Desktop「停止」后，第 2 层输入同样返回 `control_held`（任务书 §5.5 第 4 项）。
  - 不套用 3a 的 `owner_active` 让出规则，因为后台事件不碰光标，Q4 也表明两者互不可见。
  - 按 cmux #36377 增加**冲突**判断：目标 pid 是本人当前的前台 App，且基准之后有本人输入，就按 3a 同样的方式让出，
    返回 `owner_active`。是否采用，待本人决定（§8 第 3 问）。
- **开启 Chromium 无障碍**：对 Electron 设 `AXManualAccessibility`，对 Chrome 设 `AXEnhancedUserInterface`，然后
  等待并重读，最长 4 秒（R12）。

**Windows（连接器 `screen_windows.py`）**：

- 截图用 `PrintWindow(PW_RENDERFULLCONTENT)`；最小化（`IsIconic`）时返回 `window_unavailable`。
- 动作用 UIA：`ElementFromHandle` 后在该窗口子树中按坐标找元素，依次尝试 Invoke、Value、Scroll，再视实测加 Toggle、
  ExpandCollapse、SelectionItem。
- 不用 `PostMessage`。找不到合适的 pattern 时，处理方式与 macOS 相同：不自动退回前台。
- 管理员窗口返回 `target_elevated`。
- 依赖：连接器需要新增 comtypes，或用 ctypes 手写 UIA vtable。上述各项都以 §4 的脚本结果为准。

**合同影响**：

- 不改冻结的参数：`window` 早已在合同里，坐标相对窗口图像，错误码沿用冻结列表中的 `window_not_found`、
  `window_unavailable`、`device_not_supported`、`control_held`、`owner_active`。
- 要改的只有两处说明：`computer` 工具说明里关于窗口模式的回执与限制；`screenshot` 结果的 `windows` 从空列表改为
  真实列表。
- 由于修改说明会改变合同 revision 的哈希，需要按既有流程更新合同快照。

**工期估计**：

| 部分 | 估计 |
| --- | --- |
| macOS 后端（窗口列表、窗口截图、AX 定位器、盖章投递、伪屏幕 E2E、实机） | 4–5 天 |
| Windows 后端（PrintWindow、UIA、comtypes 打包、实机来回） | 4–6 天，取决于 §4 结果 |
| 服务端、工具说明、合同快照、文档 | 1 天 |
| **合计** | **约 2–2.5 周** |

## 7. 本次实验对本人桌面的影响

- **焦点**：除 R8 列出的两次之外，焦点始终在本人的 Alma 上；R8 那两次都已恢复。
- **系统弹窗**：R9 的确认弹窗出现过一次，本人已处理。
- **本人的点击**：实验期间，本人点到过测试宿主窗口 B 一次（宿主日志记录到激活，1.7 秒后取消激活），与实验动作无关。
- **清理**：实验打开的窗口和进程（宿主、遮挡窗口、Electron、临时 Chrome、Calculator）均已关闭。Electron 在
  `~/Library/Application Support/spike-electron` 留下的数据目录已删除。草稿目录（脚本、`out/`、`img/`）保留在会话
  scratchpad 中。

## 8. 待本人决定

1. **私有接口**：是否接受连接器使用 macOS 的未公开接口（`CGEventSetWindowLocation`、事件字段号、`_AXUIElementGetWindow`）？
   不接受的话，第 2 层只剩 AX 动作和键盘，自绘控件与没有 AX 元素的位置都做不了后台点击。
2. **后台送不到时**：是返回失败（建议 `device_not_supported`，并说明“请省略 `window` 改用整屏”），还是自动退回前台？
   自动退回会移动光标、抢焦点。
3. **冲突规则**：目标是本人的前台 App 且本人刚有输入时，是否让第 2 层也返回 `owner_active` 让出？
4. **Chromium 无障碍模式**：是否接受 Yuki 让本人的 Electron 或 Chrome App 进入完整无障碍模式（R12，有开销）？
5. **最小化窗口**：是否维持 `window_unavailable`（建议维持）？另一种做法是只允许不需要截图核实的 AX 动作。
6. **Windows 实测**：请在 Windows 电脑上运行 §4 的脚本，并把 `result.json` 和图片交回。

## 9. 证据索引

位于 `docs/operations/evidence/computer-3b-spike-20261110/`：

- `macos/`：
  - `probe.py`：测量工具；
  - `capture.py`：两种方式截取窗口；
  - `ax.py`：读取 AX 树、命中测试，执行 press、focus、setvalue、scroll；
  - `post.py`：post-to-pid，分普通、`--winfields`、`--stamped`、`--sl` 四种方式；
  - `axval.py`、`axmin.py`、`axscrollrow.py`；
  - `hosts/`：AppKit/WKWebView 宿主、遮挡窗口、Electron 宿主、测试页。
- `images/`：
  - `appkitA-occluded-sck.png`：被遮挡窗口的截图；
  - `appkitA-after-ax-sck.png`：AX 按下、设值、滚动之后；
  - `appkitA-after-post-sck.png`：盖章点击、按键、滚轮之后；
  - `appkitA-otherspace-sck.png`：不可见 Space 上的实时内容；
  - `appkitB-mini-changed-sck.png`：AppKit 最小化时的实时内容；
  - `chrome-min2-sck.png`：Chrome 最小化时的旧画面；
  - `electronA-occluded-sck.png`；
  - `web-after-sck.png`：WKWebView，被改写的 `Abcd`；
  - `unc-dialog-sck.png`：R9 的系统弹窗。
- `summary-idle.txt`：56 个动作逐条记录了光标、前台、本人输入计时是否被重置。
- `computer_3b_windows.py`：§4 的 Windows 待跑脚本。

全部截图、93 个 JSON 测量结果和测试日志都保留在会话草稿目录 `spike3b/`（`img/`、`out/`）中，没有提交。
