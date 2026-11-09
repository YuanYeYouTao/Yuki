# Yuki 统一 Computer Use 与 Browser Use 任务书

日期：2026-11-09

状态：**调研与任务书，产品代码尚未修改。** 唯一例外是阶段 0 的服务端两项（连接器连接日志、握手后失败改用 1011），已作为 #590 单独提交；合并与上线另行记录。

Issue：#588。关联：[服务器文件传到本人电脑任务书](Yuki-服务器文件传到本人电脑任务书-2026-11-07.md)（F08 即本书阶段 2）、[桌面端本机完整操作统一接入任务书](Yuki-桌面端本机完整操作统一接入任务书-2026-10-29.md)（本机 Shell/文件；当时明确不做 GUI 与浏览器，本书取代该限制）、[文件膨胀与工具面删减任务书](Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §6（工具面约定）、[工具面切换记录](../operations/tool-surface-cutover-20261107.md)、[Tool Kernel](tool-kernel.md)、[工具结果](tool-results.md)、[本机连接器](../operations/local-agent-connector.zh-CN.md)、[持久环境桌面](../operations/desktop-computer.zh-CN.md)、[10-29 浏览器调研](research/local-device-20261029/browser.md)。

## 1. 目标

把 Yuki 做成通用 Agent 的执行底座：**同一组 `computer` / `browser` 工具**，省略 `device` 时操作 Yuki 自己的持久 Linux 环境，给出 `device` 时经 Connector 操作本人的 Windows / Mac。底座要做对的是：模型能稳定地看到真实画面与页面结构、动作有真实回执、恢复不重复点击、本人随时能接管。模型能力的提升由模型自身带来，不为当前模型的弱点增加专用机制。

同时修复连接器的稳定性：本人电脑（本期实测为 Mac mini）经常无声离线，直到有人重启 Desktop。

## 2. 基线与方法

- 源码 `origin/main` `2385157f`（#589 之后）；Yuki-Desktop `Institute-of-Agent-Life/Yuki-Desktop` main `337aaac`（只读克隆，已删除）。
- 三路只读调研（沙箱侧、Connector/Desktop 侧、浏览器侧），另在临时 profile 的 headless Chrome 157 上实测快照体积、iframe 覆盖与回车提交；不触碰本人 Chrome profile。
- 生产只读：Mac mini 连接器日志、`client_devices`、`external_agent_*`、bot 日志与 Tailscale 状态。
- 下文 `D:` 前缀指 Yuki-Desktop 仓库路径。标 [待实机] 的结论需要在 Mac mini / Windows 上用签名版 Desktop 验证。

## 3. 作者已定决策（2026-11-08）

1. 工具形态：新增模型工具 `computer` 与 `browser`（27 → 29）；`device` 表示操作哪台电脑，省略即 Yuki 环境；截图与页面结构直接作为工具结果回到模型上下文。
2. 模型：操作集与 Provider 无关，默认主模型；不接各家私有 computer-use 协议，不按内容隐式切换 Provider；如需专门模型，经 `model_runtime` 用途显式配置。
3. 本人电脑上的浏览器：先用 Yuki 独立 Chrome profile，再做扩展驱动本人日常 Chrome。
4. 与本人共用桌面：Yuki 需要时直接操作；本人在 Desktop 一键接管/叫停，交还后继续；沿用现有接管/交还语义，不加审批、不做空闲检测。
5. 平台：Windows 与 Mac 一起做。

2026-11-09 补充：

6. 体验目标：computer use 像 Codex 那样自然——能在后台操作就不动本人的光标；browser use 像 Claude Code 那样新开一个 Yuki 专用浏览器窗口。扩展驱动日常 Chrome 退为可选的后续阶段。
7. 与本人共用桌面不以按钮为主：后台操作不冲突；必须使用真实光标时，本人的物理输入即接管；按钮只作备用（§5.5）。
8. §9 的 D1–D4 按建议采纳；D5 由作者自行把 Mac mini 连接器地址改为 `http://127.0.0.1:18768`。

## 4. 现状（已核实）

### 4.1 Yuki 沙箱

- **进程归属**：环境容器（可选 headed 层）内 `desktop_service.py` 作为 Manager 服务 `yuki-desktop` 运行 Xvnc `:1`（1440×900）、Openbox 与 Chromium（持久 profile `~/.local/share/yuki-computer/chromium`，CDP `127.0.0.1:9222`；headless 模式加 `--headless`）（`desktop_service.py:41-116`）。Manager 持有控制租约 `ComputerControl` 与 `ComputerRuntime`（`persistent.py:119-121`），RPC `display_*`、`tabs_list`、`control_*` 与流 `display_stream`/`files_read`/`control_watch`（`persistent.py:194-210,1120-1134`）。
- **模型如何用**：没有模型侧 computer RPC。`MODEL_TOOL_RPC_METHODS` 只有 `run_command`/`read_command`/`control_command`/`get_environment`（`sandbox/client.py:23-30`）。模型只能 `run_command` 调 `yuki-computer` CLI，且只从 `run_command` 说明的一句“装有浏览器时可用 yuki-computer”得知（`environment_tools.py:55-58`）。截图写到路径后，还要再 `view_file` 一轮才进上下文。
- **CLI 协议**：命令写 `commands/<uuid>.json`、轮询结果（0.1 s），Manager 每轮循环 0.5 s 收取（`yuki_computer.py:51-68`，`persistent.py:1242-1243,1278`）；`help-request`/`wait-control` 走 `requests/` 并要求一个存活的 `terminal_exec` job（`computer_control.py:191-237`）。
- **浏览器（CDP）**：每次调用新开一条 WebSocket，`recv()` 无超时（`yuki_computer.py:104-132`）。快照是 `Accessibility.getFullAXTree` 拍平的 `[backendDOMNodeId] role: name`，无层级、无值与状态、无体积上限，只含主 frame（`:195-206`）。点击是 JS `this.click()`（非可信事件），点击 `StaticText` 会抛 `TypeError` 却以 exit 0 返回（实测）；`press` 只发 `key`，**回车不会提交表单**（实测，补 `code`/`text:"\r"`/`windowsVirtualKeyCode:13` 后才提交）；`open` 只新建 target、不等加载；没有对话框、下载、文件选择、iframe 处理（`:208-387`）。`:353-354` 是不可达分支。
- **屏幕**：`import -window root` 全屏截图，xdotool 点击/输入/按键；无滚动、右键、双击、拖拽；xdotool 路径取得租约后不再复核（`:283-339`）。
- **快照体积实测**（今日格式，Chrome 157）：Wikipedia 194k 字符（约 5–6.5 万 token）、GitHub 仓库页 95k、Hacker News 42k、bilibili 12k、google.com 2k。约三分之一节点是重复 `StaticText` 的 `InlineTextBox`。
- **控制租约**（`computer_control.py`）：状态 `{holder: none|agent|human, lease_id, since}` 存于 Manager SQLite，按请求键幂等；人类 `take` 抢占 agent，agent 下一次输入得到 `control_held`（exit 75）；`return` 解决挂起的 help/wait；租约按**单次输入**取放（`yuki_computer.py:71-82`）。`agent_acquire` 把“另一个 agent 持有”也报成 `control_held`（`:140-143`），并行 Work 看起来像人类接管。Desktop 侧 noVNC 查看与接手/交回/改由這台控制（`D:src/renderer/views/Computer.tsx:160-183,433-511`），输入经 RFB 过滤（`client_api/rfb.py:35`）。
- **图像进模型的路径**：工具结果图像存于 `ToolExecutionResult.images`；四种方言都不在工具结果块里放像素，而是在一批回执配对后为每个调用追加一条 `role=user` 的 “[Host 工具媒体观察：call_id=…]” 消息（`services/turn_transcript.py:192-219`；Chat Completions `openai_compatible.py:67-76`、Claude `anthropic_messages.py:109-123`、Gemini `gemini.py:140-145`、Responses `deepseek_responses.py:158-172`）。非 core 工具与带 device 的 `view_file` 的图像归档为私有工具产物（`capabilities/results.py:192-237`，保留 24 h，`config.py:267`）；core 工作区图像保持 `source="workspace"` 并在每次后续派发前按文件版本复核（`workspace/service.py:312-331`）。
- **帧预算**：`_budget_tool_media` 按整轮**累计**统计已在转写中的图像、不淘汰，上限 16 帧 / 16 MiB（`agent_runner.py:428-478`，`config.py:335,342`）。**这会在约 16 步后终止任何“看一眼、动一下”的循环。**
- **体积限制**：Manager 响应行上限 256 KiB（`workspace_media_read` 例外 28 MiB）（`sandbox/client.py:186-189`）；Manager 子进程输出只保留 32 KiB（`persistent.py:126-142`），截图不能经 docker exec 标准输出回传。
- **过期内容**：`tests/support/desktop_computer_work_fixture.py:38-58` 仍脚本化已在 #583 删除的 `terminal_exec` 并读 `data.run_id`；`docs/operations/desktop-computer.zh-CN.md:40,44,58-59`、`local-agent-connector.zh-CN.md:11,19` 与 F08 任务书 §2 仍用切换前工具名。

### 4.2 本人电脑与 Connector

- **运行方式**：0.4.2 起（`eedbb13c`，2026-11-01）连接器是 **Desktop 的子进程**：`<runtime python> -I -B -m yuki_agent_connector --data-dir <dir> run --desktop-stdin`（`D:src/main/agent-connector.ts:346-358`），Desktop 持有 stdin，EOF 即取消运行（`cli.py:88-117`）。Desktop 每次启动与更新都会主动 `service uninstall`（`D:agent-connector.ts:309-312,340-343`），所以本机没有 launchd / 计划任务，尽管 `service.py:184-259` 支持。
- **协议与执行**：WSS 协议 4（服务端要求严格相等，`external_agents/service.py:36,80`）。连接器→服务端 `hello`/`response`/`event`/`terminal_event`/`catalog`/`ping`，服务端→连接器 `command`/`ack`/`revoked`/`catalog_ack`/`pong`。读以外的动作在执行前 `journal.begin` 记账，同 id 重复返回原回执，执行中崩溃记 `unknown`（`connector.py:404-432`，`journal.py:100-111,185-207`）——这正是“恢复不重复点击”的保证。未知资源返回 `invalid_resource`、`admitted:false`、`executed:false`（`connector.py:228,487-498`），**新增资源用能力标记即可，不必升协议**。
- **已有字节通道**：文件 `stat/read/release/write/get`，64 KiB 分块，写入逐块记账、整文件 sha256 校验后 `os.replace`（`files.py`）。设备 → 产物已通（`send(path, device)` → `publish_for_send`，`workspace/service.py:369-407`）；设备 → 模型像素也已通（`view_file(device)` → 私有工具媒体产物）。**缺的只是写进 Yuki 环境的入站字节流**（产物 → 环境、设备 → 环境），截图不依赖它。
- **事实**：`device_capabilities = {full_access, platform, shell, tty, interrupt_supported}`，只存在于活连接上（`connector.py:202-210`，`service.py:130-131`）。

### 4.3 连接器稳定性（2026-11-09 Mac mini 实况）

- Mac mini 连接器 0.4.3 经 `wss://yuki-mac-mini.taila979b9.ts.net/agents/v1/connect` 连接，即经 Tailscale serve → `127.0.0.1:18768` → Colima 端口转发 → bot，**绕 Tailscale 连回本机**；bot 每次重启期间得到一串 `HTTP 502`（`connector.log`）。
- 11-09 12:49 UTC 还在正常执行 Claude Code 操作，此后无声离线；`connector.log` 自 11-08 12:46（PST）上次 bot 重建后再无记录；14:09:56 UTC Desktop 重新拉起后 1 秒即连上。网络本身正常（Tailscale 在线，Windows 为局域网直连）。
- 根因（由源码确定，具体触发的那一次因无记录无法复原）：
  1. **致命退出无人记录、无人重启**：`run()` 对非 401/403 且非 websockets 可重试的 `InvalidStatus` 直接抛出（`connector.py:677-679`）；`connection()` 内除 `ConnectionClosed` 与撤销型 `DriverError` 外的异常都会结束 `run()`（`:652-674`）；CLI 只处理 `OSError/ValueError/httpx`（`cli.py:482-495`），其余只打到 stderr，不进 `connector.log`。Desktop 丢弃子进程 stdout/stderr（`D:agent-connector.ts:375-376`），子进程退出只清引用刷新界面，不重启、无退避、不记退出码（`:367-371`）。
  2. **假死**：服务端对连接处理中的任何 `ValueError`/`ClientError` 都以 1008 关闭（修复前 `http.py:309-311`），而连接器把 1008/4001/4003/4401/4403 当作授权丢失，在 `authorization_lost()` 里永久等待（`connector.py:666-668,710-714`）：进程活着、一直离线。
  3. websockets 16.1.1 只重试**建连**失败（`OSError`、开连超时、`EOFError`、`InvalidStatus` 500/502/503/504），其他状态码（400/404/421/426 等）与握手错误直接抛出；连接内部异常不会被重试（`websockets/asyncio/client.py:132-174,613-651`）。心跳 20 s/20 s，可在约 40 s 内发现半开连接。
  4. 服务端此前没有任何带设备 ID 的连接/断开日志。
- 已在 #590 修复服务端两点：连接/断开日志；握手后失败改用 1011（握手拒绝与 `ClientError` 仍为 1008），现有 0.4.3 连接器即可因此重连而不再假死。

## 5. 设计

### 5.1 工具合同（两工具，沿工具面约定）

成功结果是业务数据本身；失败 `{ok:false, error, message}`；结果可能已发生时加 `status:"unknown"`。两工具 `namespace="sandbox.run"`（与 `run_command` 同一插件授权）、`effect="write_state"`、`result_cacheable=false`。`device` 现在就声明：阶段 1 中给出 `device` 返回 `device_not_supported`（照 `copy_file` 源 `device` 先例），阶段 3 只放开实现、不改 schema，避免第二次合同版本变更。

```
computer(action, x?, y?, text?, dy?, window?, display?, device?)
  action: screenshot | click | type | key | scroll
  坐标为最近一次截图的像素；key 的 text 为按键组合（如 ctrl+l、Return）；scroll 在 x,y 滚 dy 格（正数向下）
  window：给出时只截该窗口、坐标相对该窗口截图，动作在后台投递给该窗口而不移动本人光标（§5.5）；
          省略时为整屏与真实光标
  成功：{width, height, display?, window?, windows:[{id,app,title}], control?} + 附带截图（动作后自动返回新截图）
  失败：control_held（附 control:{holder,since}）| display_unavailable | display_locked |
        screen_permission_required | input_permission_required | device_not_supported

browser(action, url?, node?, text?, tab?, device?)
  action: open | snapshot | click | type | press | scroll | screenshot | close
  open 给 tab 时在该标签页跳转，否则新开；type 以 text 替换输入框内容（text="" 清空）；
  scroll 用 node（滚到可见）或 text=down|up|<px>；snapshot 的 text 为过滤词
  成功：{tab:{id,url,title,loading}, events:[navigated|new_tab|dialog|download|file_chooser], snapshot, truncated?}
        screenshot 额外附带页面图
  失败：node_stale（附新快照）| tab_not_found | dialog_open | control_held | timeout |
        browser_unavailable | extension_disconnected | detached_by_owner | not_allowed | device_not_supported
```

坐标输入只在 `computer`；`browser` 只按节点操作。`browser` 增加 `screenshot`（headless 模式没有屏幕）。

### 5.2 快照格式（沙箱、独立 profile、扩展三条路共用）

- 基于可访问性树、经 CDP 获取：`chrome.debugger` 本身就是 CDP，三条路同一实现。
- 缩进的类 YAML 行：`- button "Sign in" [e57]`；去掉 `InlineTextBox`、`LineBreak`、`ListMarker` 与无名 `generic/none`（保留子节点）；连续 `StaticText` 并入父行；显示 `value=`、`checked`、`expanded`、`selected`、`disabled`、`focused`、`required`、`level`；单节点文本截到约 100 字符；首行 `page: 标题 — url [tab]`。
- 节点 ID 无状态：主 frame `e<backendDOMNodeId>`，子 frame `f<k>e<id>`；节点死亡（跳转、重渲染）时 `DOM.resolveNode` 失败 → `node_stale` + 新快照，绝不按猜测位置点击。
- iframe：`Page.getFrameTree` 逐 frame `getFullAXTree({frameId})`；跨域 frame 用 `Target.setAutoAttach{flatten:true}` 会话。封闭 shadow DOM 已包含（实测）。
- 体积：默认约 12k 字符，按文档顺序；超出时优先保留可交互元素、标题、landmark 与表单字段，返回 `truncated:{shown,total}`；更多内容走既有结果预算与 `read_result`。

### 5.3 动作语义与恢复

- 点击：`DOM.scrollIntoViewIfNeeded` + `DOM.getContentQuads` 取中心，`Input.dispatchMouseEvent` 可信事件；`press` 发完整键事件；`exceptionDetails` 视为错误。
- 动作完成：启用 Page/Network 事件，执行后在固定上限内等待导航/加载、对话框、新 target、下载或短暂网络静默中的任一项，再快照；到上限返回 `loading:true`，不是错误。CDP 接收一律带超时。
- 对话框：监听 `Page.javascriptDialogOpening`，返回 `dialog` 事件，不自动接受；由下一次 `press`（Enter/Escape）处理。
- 下载：`Browser.setDownloadBehavior` 到沙箱 `/workspace/downloads`、本人电脑上的连接器私有目录；按 guid 报告路径与状态，回放不再触发。
- 文件选择：`Page.setInterceptFileChooserDialog` → `file_chooser` 事件，再以 `DOM.setFileInputFiles` 填入沙箱 `/workspace` 路径。
- **恢复**：工具调用键即命令 ID。沙箱侧动作先在 `environment_mutations` 写待定行，再执行、写结果；重启后有结果返回原结果，只有待定行报 `unknown`，**不照搬 `display_wake` 的“待定即重跑”**（`computer_runtime.py:86-104`）。本人电脑侧复用连接器 `journal.begin`。点击、按键、输入、表单提交一律不重发。
- 标签页归属：页面打开的新标签页自动归当前 Work（今天无归属、只能用原始 targetId 访问）；不得关闭他 Work 的标签页。
- 并行 Work：同一环境的动作按环境串行执行（Manager 内一把锁），不再把“另一 agent 持有”报成人类接管。

### 5.4 截图与帧预算

- 截图（沙箱与本人电脑）作为私有工具媒体产物归档，`source="tool"`、不可变；`capabilities/results.py:196-200` 的归档条件从“非 core provider 或带 device 的 view_file”改为“凡非 workspace/history 来源的图像都归档”——这是删除特例，不是新存储。
- 帧预算（**待作者决定 D1**）：改为只统计本次请求实际携带的图像，旧的工具媒体观察由既有上下文压缩丢弃，需要时凭工具产物句柄重读。不另设截图专用阈值。
- 不支持图像的模型（如 DeepSeek）：沿既有规则返回 `image_capability_unavailable` / `media_read:false`，动作效果保留；`browser` 的快照必须单独就够用。不加“某情况自动附截图”的规则。

### 5.5 与本人共用桌面：先不冲突，再自然接管

目标是本人照常用电脑，Yuki 同时干活；按钮只是备用。三层，按顺序使用：

1. **浏览器不碰光标**：`browser` 经 CDP 向页面投递输入（`Input.dispatch*` 作用于该页面），Yuki 专用 Chrome 窗口可以在后台或另一个桌面空间，不移动本人光标、不抢焦点。网页任务优先走这里。
2. **原生应用优先后台操作**：`computer` 给出 `window` 时，截图只截该窗口（macOS ScreenCaptureKit 按窗口过滤；Windows `PrintWindow`/`BitBlt` 窗口 DC），动作投递给该窗口所属进程而不经全局光标：
   - macOS：能定位到可访问性元素时用 AX 动作（`AXPress`、设置 `AXValue`、`AXFocused`）；否则 `CGEventPostToPid` 把带坐标的键鼠事件只发给目标进程（同样需要辅助功能权限）。
   - Windows：能定位到 UI Automation 元素时用 `InvokePattern`/`ValuePattern`/`ScrollPattern`；否则向目标窗口投递窗口消息（`PostMessage` 的鼠标/键盘消息）。
   - 坐标到元素的定位用系统命中测试（macOS `AXUIElementCopyElementAtPosition`，Windows `IUIAutomation::ElementFromPoint`），模型仍只给截图坐标，不需要可访问性树快照。
   - 已知局限：游戏、画布类、部分 Electron/自绘应用不响应后台投递，结果回执只写 `submitted`，由下一张截图核实；不响应时改用第 3 层。
3. **必须用真实光标时，本人的物理输入即接管**：省略 `window` 的全局输入期间，连接器监听本人的物理键鼠（macOS 事件监听区分本进程合成事件的来源 PID，Windows 低级钩子的 `LLMHF_INJECTED`/`LLKHF_INJECTED` 标志），一旦出现即把本地租约置为 owner：正在执行的动作停在当前一步，后续输入返回 `control_held`。恢复不设闲置计时：本人在聊天里让 Yuki 继续，或点 Desktop 横幅上的「繼續」，租约回到 none。

租约放在输入真正发生的地方——连接器：配置项 `computer_control:{holder: owner|none, since}`，与 `full_access` 一样原子写入、每条命令重读、在执行处核验；第 1、2 层不取租约，第 3 层输入前核验；截图始终可读；变化经 `catalog_changed` 同步到 `device_capabilities`。不新增服务端表或审批。

Desktop：连接器在 `--desktop-stdin` 模式下每个 GUI 动作向 stdout 打一行 JSON 活动；Desktop 据此在托盘与「這台電腦」页显示“Yuki 正在操作（后台 / 前台）”，备用按钮「停止」（写 owner）与「繼續」（写 none）。本人看的是真屏幕，不需要查看器。

沙箱侧本来就是独立显示，不与本人冲突；接管保持现状，`help-request` / `wait-control` 保留，`computer` 的 `control_held` 说明里给出等待方式。

### 5.6 GUI 驱动（连接器 0.5.x）

| 方面 | macOS | Windows |
| --- | --- | --- |
| 截图 | pyobjc + ScreenCaptureKit `SCScreenshotManager`（14.0+）；`CGWindowListCreateImage` 在 15 SDK 已废弃，作后备 | GDI `BitBlt`（`mss` 或约 150 行 ctypes）；不用 WGC（黄框） |
| 输入 | `CGEventPost`；无辅助功能权限时**静默丢弃**，必须先 `CGPreflightPostEventAccess` | `SendInput`；对管理员窗口（UIPI）静默无效，回执只写 `submitted`，由下一张截图核实 |
| 权限 | 屏幕录制 + 辅助功能，TCC 归“负责进程”；Desktop 子进程模式下应为签名的 Yuki.app，升级后保留 [待实机 `launchctl procinfo`]；ad-hoc/开发版每次重建重新授权 | 连接器在用户登录会话中运行，不受 session 0 隔离影响；安全桌面（UAC、锁屏）不可截图/输入 |
| 坐标 | 全局点坐标，副屏可为负；截图为像素（Retina ×2）；连接器按 `bounds`/`scale` 换算 | 启动时设 per-monitor-v2 DPI；`MOUSEEVENTF_ABSOLUTE\|VIRTUALDESK` 归一化到 0..65535 |
| 事实 | `device_capabilities.computer = {screen, input, displays:[{id,bounds,scale,primary}], control}` | 同左 |

- 依赖：darwin 标记下 `pyobjc-core`、`pyobjc-framework-Quartz`、`pyobjc-framework-ScreenCaptureKit`（cp312 universal2 轮子）；win32 用 `mss` 或纯 ctypes。不用 pyautogui、pynput。`build_runtime.py` 加 GUI 导入探针；wheel 仍为纯 Python；三平台 CI 不变（托管 runner 无法授予 TCC，只测事实探针与坐标换算）。
- 风险：pyobjc/libffi 回调在 hardened runtime 下可能需要 `com.apple.security.cs.allow-unsigned-executable-memory`，签名版验证后再决定是否加。macOS 15 起有每月一次的屏幕录制再确认提示，作为本人操作写进文档。
- Desktop 最低 macOS 13，ScreenCaptureKit 截图需 14：只在 14+ 声明 `computer` 能力（**D3**）。
- 截图字节：连接器把截图注册为一个 `FileTransfers` 源，返回 `transfer_id/size/sha256/version/display/width/height/scale`，服务端复用 `download` 的读/释放循环交给 `WorkspaceInspector`。不依赖阶段 2。
- 可访问性只用于 §5.5 第 2 层的“坐标 → 元素”命中测试与后台动作，本期不向模型输出整棵原生可访问性树；网页结构由 `browser` 提供。真实任务证明需要原生结构快照时再议。

### 5.7 本人电脑上的浏览器

- **阶段 A，独立 profile**：连接器找到 Chrome（Windows 注册表 `App Paths`/Program Files/LocalAppData；macOS `/Applications` 或 `~/Applications`），找不到返回真实错误、不下载浏览器；profile 放连接器私有目录；参数 `--user-data-dir=… --remote-debugging-port=0 --no-first-run --no-default-browser-check`，读 `DevToolsActivePort`；**先连已有实例、失败才启动**（同一 profile 的第二次启动会交给已运行实例并忽略新参数，实测）；浏览器脱离连接器进程生命周期（Windows job object、macOS 进程组）[待实机]。浏览器自身截图走 `Page.captureScreenshot`，不需要 macOS 屏幕录制权限。Chrome 136+ 不允许对默认 profile 开远程调试，137+ 品牌 Chrome 移除 `--load-extension`，均印证独立 profile。
- **阶段 B，扩展**：MV3 扩展只做 CDP 中继（`chrome.debugger` 的 attach/detach/sendCommand 与 tabs create/remove，事件回传），页面逻辑全部在共享 Python 驱动；只附加到 Yuki 标签组里的标签页；调试信息栏接受，用户取消或打开 DevTools → `detached_by_owner` 即接管信号；通道用 native messaging（按扩展 ID 固定，不开端口），宿主为 `yuki-agent-connector browser-host`，经私有目录 Unix socket / Windows 命名管道桥到运行中的连接器（连接器今天没有本地 IPC，属新增）；保持 `connectNative` 端口以维持 service worker；上传文件需本人打开“允许访问文件网址”，否则报 `not_allowed`；先以固定 `key` 的已解压扩展分发。参考 Playwright MCP 扩展模式与 Claude in Chrome 的架构（不复制代码）。
- **Chrome 144 auto-connect**（`chrome://inspect/#remote-debugging`）不需要扩展，但每次连接都要本人点“允许”、暴露整个 profile；阶段 4 前做一次短 spike 对比（**D4**）。
- 共享驱动：把 `yuki_computer.Browser` 的 CDP 部分移入只依赖标准库与 `websockets` 的模块，以端点为输入，沙箱 CLI 与连接器共用；`fcntl`、文件信箱、xdotool、ImageMagick 等 Linux 专属部分留在沙箱 CLI。

### 5.8 连接器稳定性（连接器 0.4.4 + Desktop）

1. 连接器在 `cli.py` 的 `run` 中对除 `KeyboardInterrupt/SystemExit` 外的任何异常 `logger.exception` 写入 `connector.log` 后非零退出，stdout 的 `{"problem":…}` 带退出类别。
2. 不因暂时性服务端状态结束 `run()`：`connect(process_exception=…)` 让 401/403 以外的 `InvalidStatus` 都按退避重试；删除 `connector.py:677-679` 的重新抛出；`authorization_lost` 只认 4401/4403 与 `revoked` 消息（服务端已在 #590 把非授权失败改为 1011）；`refresh_config()` 失败时记日志并沿用原配置。
3. Desktop 监督：子进程非预期退出（非 `stop()/close()`）时按指数退避（1 s 起、60 s 封顶、稳定运行一段时间后归零）重启；保留最近一次退出码/信号与 stderr 末尾 64 KiB 于 userData；「這台電腦」页显示“连接器已停止（原因…）/已自动重启 N 次”。这是进程监督，不是新的完成门槛；原回执仍按 journal 规则为 `unknown`。
4. 服务器所在主机上的连接器用回环地址：Mac mini 的连接器改连 `http://127.0.0.1:18768`（文档已允许回环 WS），去掉 Tailscale 绕行；运维步骤加文档一行，无代码改动（**D5**，需在 Desktop 中重启连接器）。
5. 睡眠唤醒：依赖 20 s/20 s 心跳（约 40 s 内发现）；可选 Desktop 在 `powerMonitor.resume` 时向 stdin 写一个字节，连接器据此立即重连。低优先。
6. Desktop 诊断：显示服务端连接/断开时间、连接器进程状态与最近退出原因，并提供“导出诊断”带出 `connector.log` 末尾。
7. 保持 Desktop 子进程模式，不恢复独立系统服务：macOS 上它让签名的 Yuki.app 成为 TCC 负责进程（§5.6），且接管入口就在 Desktop；作为服务器的 Mac mini 需开启 `keepInTray` 与登录项（**D2**）。

## 6. 阶段与实施项

| 阶段 | 内容 | 依赖 | 估计 |
| --- | --- | --- | --- |
| 0 稳定性 | §5.8；服务端两项已在 #590 | — | 2–3 天（含 Desktop 发版） |
| 1 Yuki 沙箱 | `computer`/`browser` 工具、共享 CDP 驱动、快照格式、动作语义、截图直出与归档、帧预算、Manager 动作处理与恢复、清理过期 CLI 提示与夹具 | D1 | 3–4 天 |
| 2 文件传输第二期 | Manager 入站流 `files_write`（`files_read` 的镜像：头部 `{destination,size,sha256,source_key,request_id}` + 原始字节，校验后经 `self.files.copy` 提交并记 `environment_mutations`）；`copy_file` 放开产物/设备 → 环境 | — | 3–4 天 |
| 3 本人电脑 | 连接器 `computer` 资源与本地租约、Mac/Windows 驱动（含 §5.5 后台投递与物理输入接管）、能力事实、服务端按 `device` 路由、独立 profile `browser`（新开 Yuki Chrome 窗口）、Desktop 横幅/权限页 | 0、1；D2、D3 | 共享 4–5 天 + macOS 6–8 天 + Windows 5–6 天 + 浏览器 3–4 天 + Desktop 3–4 天 |
| 4 浏览器扩展（可选） | spike 对比 auto-connect，然后扩展 + native messaging 宿主 + 本地 IPC；只在独立窗口不够用（需要本人日常登录态）时做 | 3；D4 | 4–6 天 |

阶段 0 与阶段 1 可并行。阶段 1 完成后 Yuki 自己的电脑即可用，不等后续阶段。阶段 2 与阶段 1/3 无硬依赖，可穿插。

### 6.1 阶段 1 文件清单

1. `sandbox/environment_tools.py`：两工具声明加入 `SANDBOX_TOOLS`；`run_command` 说明删去 `yuki-computer` 一句。
2. `sandbox/client.py`：`MODEL_TOOL_RPC_METHODS` 增 `computer→computer_action`、`browser→browser_action`，加入 `_MUTATING_METHODS`，使用大响应上限；`open` 允许更长超时。
3. `sandbox/persistent.py` / `computer_runtime.py`：两方法交给 `ComputerRuntime`——按环境串行、写待定行、必要时唤醒显示、进程内取租约（actor `{work_id, request_id}`）、`docker exec … yuki-computer <action> --lease L --work W --shot <runtime 路径>`、从 runtime 目录读图、`finally` 释放租约。删除 `collect_commands`/`read_command` 及其调用（`persistent.py:1243`）。
4. `sandbox/computer_control.py`：`agent_acquire` 接受请求 actor，不再要求 `run_id`。
5. `sandbox/yuki_computer.py`：删除 `operation`、`input_lease`、`wake/sleep` 子命令与不可达分支；租约与 work 作为参数，**每次输入（含 xdotool）前复核**；补 xdotool 滚动；CDP 部分改用共享驱动。保留 `status`、`help-request`、`wait-control`。
6. 共享驱动新模块（位置随实施决定，须可被连接器包引用）：§5.2–5.3 全部行为。
7. `services/agent_tools.py`：截图经既有 preparer 转 `ChatImage`，作为 `PreparedMediaData` 返回。
8. `capabilities/results.py:196-200`：归档条件改为“非 workspace/history 来源一律归档”。
9. `services/agent_runner.py` 帧预算与上下文压缩：按 D1。
10. `main_agent_contract.py` 合同版本 35 → 36；`codemode/tool_visibility.py` 增两名（Code Mode 不重设计，#573；核对 `CODE_API_REVISION` 是否需要变动）；`client_api/work_projection.py` 把两工具映射到「操作電腦」「使用瀏覽器」类别。
11. 文档：`tool-kernel.md`（27→29、Manager 工具如何返回媒体）、`tool-results.md`（截图帧）、`desktop-computer.zh-CN.md`、`local-agent-connector.zh-CN.md`、F08 任务书 §2 的旧工具名。
12. 测试：先写失败清单；`desktop-headed` CI job 增一条 E2E；更新 27 工具断言；修复 `desktop_computer_work_fixture.py`。

### 6.2 阶段 3 连接器与服务端要点

- 连接器：新资源 `computer`（`screenshot` 归入 `READ_ACTIONS`；`click{x,y,button?,count?}`、`type{text}`、`key{keys}`、`scroll{x,y,dx,dy}`，可带 `display`）与 `browser`（独立 profile、共享驱动）；`computer-control --holder owner|none`、`computer-permissions --request` 两个 CLI 由 Desktop 调用（必须以 Desktop 子进程运行，TCC 才归 Yuki.app）；平台模块 `gui_darwin.py`、`gui_win32.py`；Linux 连接器有 `$DISPLAY` 时可用 mss/xdotool，否则 `unsupported`。
- 服务端：两工具带 `device` 时路由到 `LocalDeviceService`，命令 ID 即调用键；能力缺失的设备直接失败（旧连接器回 `invalid_resource`）；截图拉回后直出并归档。
- Desktop：权限状态与打开系统设置面板的按钮（`x-apple.systempreferences:…Privacy_ScreenCapture` / `…Privacy_Accessibility`）、操作横幅与托盘状态、随包更新连接器 0.5.x（`D:app.ts:410` 的最低版本检查不变，GUI 能力按事实声明）。

## 7. 失败用例（写代码前须逐条落实）

### 7.1 合同与恢复

- F1 直调与 Code Mode 清单不是 29 项、合同版本未变、与 `yuki_<name>` 包装冲突。
- F2 namespace/effect 缺失，导致截图被缓存或与点击并行。
- F3 阶段 1 给出 `device` 却在本地执行，而非 `device_not_supported`。
- F4 同一调用键重试时重新点击/输入，而非返回保存的结果。
- F5 Manager / 连接器 / bot 在待定与结果之间崩溃后报告成功或重试，而非 `status:"unknown"`。
- F6 套接字无应答未被视为不确定。
- F7 两个 Work 同时操作得到 `control_held`，而非串行。
- F8 本人接管后下一次输入仍被执行（xdotool 路径、解析与派发之间）。
- F9 交还后 `wait-control` 不完成，或仍有人持有时就完成。
- F10 动作失败/超时后 agent 租约未释放；撤销 Desktop 设备后租约未释放。

### 7.2 媒体

- F11 四种方言中任一没有把截图带进请求。
- F12 截图以 `source="workspace"` 发送，下一帧覆盖后复核失败。
- F13 截图超过响应行上限或被 32 KiB 输出截断。
- F14 不支持图像的模型丢失了点击效果（应为 `media_read:false` 且效果保留）。
- F15 帧预算让 30 步循环提前终止。
- F16 图像坐标与点击坐标不一致（屏幕 vs 视口、JPEG 重采样、Retina、Windows DPI、多屏负坐标）。

### 7.3 浏览器

- F17 页面加载完成前快照；跳转后旧节点点中错误元素（应 `node_stale`）。
- F18 跨域 iframe 表单不可达。
- F19 JS 对话框让调用挂起超时；回车不提交表单；文件输入弹出原生选择框阻塞页面。
- F20 下载落在工作区或连接器目录之外，或未返回路径；回放再次触发下载。
- F21 标签页归属跨 Work 泄漏，或允许关闭他 Work 的标签页；新标签页不可达。
- F22 巨大可访问性树溢出结果预算而没有 `truncated`/续读。
- F23 中文与 Unicode 输入在浏览器或 xdotool 中出错。
- F24 独立 profile：重复启动拿到无调试端口的实例；本人关闭窗口后无法重新打开；误杀本人的 Chrome。
- F25 扩展：service worker 闲置断开、本人取消调试、文件网址访问未开启。

### 7.4 本人电脑

- F26 macOS 未授予屏幕录制/辅助功能时静默无效（应为 `*_permission_required`，输入前必须预检）。
- F27 屏幕锁定、显示器休眠、安全桌面时报告成功（应为 `display_locked`/`desktop_unavailable`）。
- F28 UIPI 下 `SendInput` 被声称为“已点击”（回执只能是 `submitted`）。
- F29 旧连接器收到 `computer` 命令时未得到 `invalid_resource`，或服务端未隐藏无能力设备。
- F30 `full_access` 关闭时仍执行。
- F31 点击成功但图像归档失败时丢失效果（应 `media_read:false`）。
- F32a 给出 `window` 的后台动作移动了本人光标或抢走前台焦点。
- F32b 目标应用不响应后台投递时回执写成“已点击”（应为 `submitted`，由截图核实）。
- F32c 本人物理输入未被识别为接管，或 Yuki 自己的合成输入被误判为本人输入而自我停止。
- F32d 接管后无闲置计时自动恢复（应只由聊天“继续”或横幅「繼續」恢复）。
- F32e 后台截图截到了被遮挡窗口的旧内容或黑屏而未报告。

### 7.5 稳定性

- S1 重部署期间 502/503 被重试并记日志；S2 代理返回 404/421/426 也被重试；S3 服务端内部错误 → 1011 → 重连而非停住（#590 已覆盖服务端）；S4 真实撤销 → 4401/4403 → 停住、ERROR 日志、Desktop 显示“授权已撤销”；S5 未捕获异常 → `connector.log` 栈 + 非零退出 → Desktop 退避重启并显示原因；S6 崩溃循环有退避上限与可见计数，无忙等；S7 Desktop 退出与本人点击停止时不重启；S8 睡眠唤醒约 40 s 内重连；S9 连接时配置正被改写 → 沿用原配置并记日志；S10 重启时进行中的终端/Agent 操作回执为 `unknown`，绝不重派；S11 服务端日志能对上每台设备每次断开的时间与原因。

### 7.6 历史与界面

- F32 Client API 的 Work 步骤未归入「操作電腦」「使用瀏覽器」；旧 `run_command`/`yuki-computer` 回执与 help run 投影出错。
- F33 文档、提示词或夹具仍引用 `terminal_exec` 或 `yuki-computer` 浏览器流程。
- F34 `desktop-headed` CI job 没有运行新 E2E 或没有产物。

## 8. 验证

- **阶段 1**：`desktop-headed` job 中一条离线全应用 E2E（脚本化模型）对本地夹具站点：多步表单、回车提交、下载链接、`<input type=file>`、`alert/confirm`、跨域 iframe、第二个标签页；中途插入本人 `take` 再 `return`，确认 Yuki 继续。产物写 `test-results/computer-browser/`：29 工具清单、各帧图像、四种方言的请求体（含图像观察消息）、服务端记录的输入日志、租约时间线、回执。另用真实模型跑若干真实网页任务并保存产物，记录成功率与每步耗时（作为基线，不作上线门槛）。
- **阶段 0**：连接器单测覆盖 S1–S10 的退出与重试分类；Desktop 监督在 Mac mini 实机演练：杀子进程、断网、bot 重启、睡眠唤醒，记录 `connector.log`、Desktop 诊断与服务端 `connector_connected/disconnected` 三方时间线。
- **阶段 2**：`files_write` 的大小与 sha256 不符、中途断开、重放三类 E2E；生产库副本离线验证 `copy_file` 新方向的回执。
- **阶段 3**：签名版 Desktop 在 Mac mini 与 Windows 上实机录制：权限授予前后、多屏与缩放、锁屏、接管/交还、离线重放各一次，产物为截图加回执；`launchctl procinfo` 证明负责进程为 Yuki.app。
- **阶段 4**：先交付 spike 结论（auto-connect 与扩展各自的实机 UX），再实现。
- 通用：`ruff`、`mypy --platform linux src`、单元与集成全量；连接器包三平台 CI；不把本地 macOS 环境失败当作通过，凡环境失败的测试须核对其是否掩盖了本次改动的真实失败。

## 9. 作者决定（2026-11-09 已定）

| 编号 | 问题 | 决定 |
| --- | --- | --- |
| D1 | 帧预算：现为整轮累计 16 帧且不淘汰，会终止长循环 | 只统计本次请求实际携带的图像，旧图由上下文压缩丢弃、凭产物句柄可重读；不设截图专用阈值 |
| D2 | 连接器运行方式 | 保持 Desktop 子进程模式并加监督；Mac mini 开启托盘常驻与登录项；不恢复独立系统服务 |
| D3 | macOS 13 上的 `computer` | 只在 macOS 14+ 声明能力，Desktop 最低版本不变 |
| D4 | 阶段 4 走扩展还是 Chrome 144 auto-connect | 阶段 4 改为可选；需要时先做短 spike，倾向扩展 |
| D5 | Mac mini 连接器改连回环地址 | 作者在 Desktop 中把连接器服务器地址改为 `http://127.0.0.1:18768`（CLI 转为 `ws://127.0.0.1:18768/agents/v1/connect`）并重启连接器 |
| D6 | 与本人共用桌面 | §5.5 三层：浏览器不碰光标、原生应用优先后台投递、必须用真实光标时物理输入即接管；恢复只由本人明确表示继续 |
