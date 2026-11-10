# Yuki 统一 Computer Use 与 Browser Use 任务书

日期：2026-11-09

状态：**调研、独立复审与任务书，产品代码尚未修改。** 唯一例外是阶段 0 的服务端两项（连接器连接/断开日志、握手后的服务端失败改用 1011），已由 #590 合并（`1fad7185`）并 Bot-only 上线。

Issue：#588。关联：[服务器文件传到本人电脑任务书](Yuki-服务器文件传到本人电脑任务书-2026-11-07.md)（其 F08“本人电脑/产物 → Yuki 环境”独立推进，不在本书范围）、[桌面端本机完整操作统一接入任务书](Yuki-桌面端本机完整操作统一接入任务书-2026-10-29.md)（本机 Shell/文件；当时明确不做 GUI 与浏览器，本书取代该限制）、[文件膨胀与工具面删减任务书](Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §6、[工具面切换记录](../operations/tool-surface-cutover-20261107.md)、[Tool Kernel](tool-kernel.md)、[工具结果](tool-results.md)、[本机连接器](../operations/local-agent-connector.zh-CN.md)、[持久环境桌面](../operations/desktop-computer.zh-CN.md)、[10-29 浏览器调研](research/local-device-20261029/browser.md)。

## 1. 目标

把 Yuki 做成通用 Agent 的执行底座：**同一组 `computer` / `browser` 工具**，省略 `device` 时操作 Yuki 自己的持久 Linux 环境，给出 `device` 时经 Connector 操作本人的 Windows / Mac。底座要做对的是：模型能稳定地看到真实画面与页面结构、动作有真实回执、恢复不重复点击、本人照常用电脑且随时能叫停。模型能力的提升由模型自身带来，不为当前模型的弱点增加专用机制。

同时修复连接器稳定性：本人电脑（本期实测为 Mac mini）会无声离线，直到有人重启 Desktop。

## 2. 基线与方法

- 源码 `origin/main` `2385157f`，复审时对照 `1fad7185`（#590）；Yuki-Desktop `Institute-of-Agent-Life/Yuki-Desktop` main `337aaac`（只读克隆，已删除）。
- 三路只读调研（沙箱侧、Connector/Desktop 侧、浏览器侧）与一次不依附作者结论的独立复审；另在临时 profile 的 headless Chrome 157 上实测快照体积、iframe 覆盖与回车提交，未触碰本人 Chrome profile。
- 生产只读：Mac mini 连接器日志、`client_devices`、`external_agent_*`、bot 日志与 Tailscale 状态。
- `D:` 前缀指 Yuki-Desktop 路径。标 [待实机] 的结论需在 Mac mini / Windows 上用签名版 Desktop 验证；标 [spike] 的设计在实施前先做小实验，结论写回本书再估工期。

## 3. 作者决定

2026-11-08：

1. 新增模型工具 `computer` 与 `browser`（27 → 29）；`device` 表示操作哪台电脑，省略即 Yuki 环境；截图与页面结构直接作为工具结果回到模型上下文。
2. 操作集与 Provider 无关，默认主模型；不接各家私有 computer-use 协议，不按内容隐式切换 Provider；需要专门模型时经 `model_runtime` 用途显式配置。
3. Windows 与 Mac 一起做。

2026-11-09：

4. 体验目标：computer use 像 Codex 那样自然——能在后台操作就不动本人光标；browser use 像 Claude Code 那样新开一个 Yuki 专用浏览器窗口。驱动本人日常 Chrome 的扩展降为可选后续。
5. 与本人共用桌面不以按钮为主：后台操作不冲突；必须使用真实光标时，本人的输入即叫停；恢复由本人明确表示（聊天里说继续，或 Desktop「繼續」）；按钮只作备用（§5.5）。
6. 连接器保持 Desktop 子进程模式并加监督；Mac mini 开启托盘常驻与登录项；不恢复独立系统服务（§5.8）。
7. macOS 只在 14+ 声明 `computer` 能力，Desktop 最低版本不变。
8. 作者自行把 Mac mini 连接器服务器地址改为 `http://127.0.0.1:18768`（CLI 转为 `ws://127.0.0.1:18768/agents/v1/connect`），去掉经 Tailscale 绕回本机。

## 4. 现状（已核实）

### 4.1 Yuki 沙箱

- **进程归属**：环境容器（可选 headed 层）内 `desktop_service.py` 作为 Manager 服务 `yuki-desktop` 运行 Xvnc `:1`（1440×900）、Openbox 与 Chromium（持久 profile `~/.local/share/yuki-computer/chromium`，CDP `127.0.0.1:9222`；headless 模式加 `--headless`）（`desktop_service.py:41-116`）。Manager 持有控制租约 `ComputerControl` 与 `ComputerRuntime`（`persistent.py:119-121`），RPC `display_*`、`tabs_list`、`control_*` 与流 `display_stream`/`files_read`/`control_watch`（`persistent.py:194-210,1120-1134`）。
- **模型如何用**：没有模型侧 computer RPC。`MODEL_TOOL_RPC_METHODS` 只有 `run_command`/`read_command`/`control_command`/`get_environment`（`sandbox/client.py:23-30`）。模型只能 `run_command` 调 `yuki-computer` CLI，只从 `run_command` 说明的一句“装有浏览器时可用 yuki-computer”得知（`environment_tools.py:55-58`）；截图写到路径后还要再 `view_file` 一轮。
- **CLI 协议**：命令写 `commands/<uuid>.json` 轮询结果，Manager 每轮循环 0.5 s 收取（`yuki_computer.py:51-68`，`persistent.py:1242-1243,1278`）；同一处理循环还调用 `observe_display()`（`computer_runtime.py:172-176,214`），`control_watch` 依赖它得到显示变化。`help-request`/`wait-control` 走 `requests/` 并要求存活的 `terminal_exec` job（`computer_control.py:191-237`）。
- **浏览器（CDP）**：每次调用新开一条 WebSocket，`recv()` 无超时（`yuki_computer.py:104-132`）；每个 CLI 调用是独立进程，无法保持会话级状态（对话框监听、文件选择拦截、下载行为、新标签页归属）。快照是 `Accessibility.getFullAXTree` 拍平的 `[backendDOMNodeId] role: name`，无层级、无值与状态，只含主 frame（`:195-206`）。点击是 JS `this.click()`（非可信事件），点击 `StaticText` 会抛 `TypeError` 却以 exit 0 返回（实测）；`press` 只发 `key`，**回车不会提交表单**（实测）；`open` 只新建 target、不等加载；无对话框、下载、文件选择、iframe 处理（`:208-387`）；`:353-354` 不可达。
- **屏幕**：`import -window root` 全屏截图，xdotool 点击/输入/按键；无滚动、右键、双击、拖拽；xdotool 路径取得租约后不再复核（`:283-339`）。
- **快照体积实测**（今日格式，Chrome 157）：Wikipedia 194k 字符、GitHub 仓库页 95k、Hacker News 42k、bilibili 12k、google.com 2k；约三分之一节点是重复 `StaticText` 的 `InlineTextBox`。
- **控制租约**（`computer_control.py`）：`{holder: none|agent|human, lease_id, since}` 存于 Manager SQLite、按请求键幂等；人类 `take` 抢占 agent，agent 下一次输入得到 `control_held`（exit 75）；`return` 解决挂起的 help/wait；租约按单次输入取放（`yuki_computer.py:71-82`）。`agent_acquire` 把“另一个 agent 持有”也报成 `control_held`（`:140-143`）；`finish_run` 释放 run 持有的 agent 租约（`:250-266`）。Desktop 有 noVNC 查看与接手/交回（`D:src/renderer/views/Computer.tsx:160-183,433-511`），输入经 RFB 过滤（`client_api/rfb.py:35`）。
- **图像进模型**：工具结果图像存于 `ToolExecutionResult.images`；四种方言都不在工具结果块里放像素，而是在回执配对后每个调用追加一条 `role=user` 的 “[Host 工具媒体观察：call_id=…]”（`services/turn_transcript.py:192-219`；Chat Completions `openai_compatible.py:67-76`、Claude `anthropic_messages.py:109-123`、Gemini `gemini.py:140-145`、Responses `deepseek_responses.py:158-172`）。非 core 工具图像与带 device 的 `view_file`（`local_media` 特例）归档为私有工具产物（`capabilities/results.py:192-237`，保留 24 h，`config.py:267`）；`read_result` 读回的图像已是 `source="tool"`（`tool_results/artifacts.py:478`）；`WorkspaceInspector` 产出的图像标 `source="workspace"`（`workspace/inspect.py:90,98`），并在每次后续派发前按文件版本复核（`workspace/service.py:312-331`）。
- **图像预算两处**：派发时 `_budget_tool_media` 按整轮累计统计、不淘汰（`agent_runner.py:428-478`）；请求准入时 `_check_request_media_budget` 对转写中的**全部**图像检查 16 帧 / 16 MiB，超出抛 `WorkCapacityError`（`agent_runner.py:418-426`，`turn_execution.py:155-156,626-631`）。执行压缩只按 token 预算触发，`_native_tail_items` 保留带媒体的 user 消息（`turn_execution.py:1078`，`context_compaction.py:492-510`）。**约第 17 张截图时请求被拒，而此前的点击已经发生。** Host 预处理器会缩放并按字节上限再压缩图像（`image_preprocessor.py:152-159,203`），坐标与原图不再一一对应。
- **体积与时限**：Manager 响应行上限 256 KiB（`workspace_media_read` 28 MiB）（`sandbox/client.py:186-189`）；Manager 子进程输出只保留 32 KiB（`persistent.py:126-142`）；沙箱 RPC 默认 7 s 截止，只有 `workspace_*` 等少数方法不设截止（`sandbox/client.py:164-180`）。
- **过期内容**：`tests/support/desktop_computer_work_fixture.py:38-58` 仍脚本化已删除的 `terminal_exec`；`docs/operations/desktop-computer.zh-CN.md:40,44,58-59`、`local-agent-connector.zh-CN.md:11,19` 与 F08 任务书 §2 仍用切换前工具名。

### 4.2 本人电脑与 Connector

- **运行方式**：0.4.2 起（`eedbb13c`）连接器是 **Desktop 的子进程** `<runtime python> -I -B -m yuki_agent_connector --data-dir <dir> run --desktop-stdin`（`D:src/main/agent-connector.ts:346-358`），Desktop 持有 stdin，EOF 即取消运行（`cli.py:88-117`）；Desktop 启动与更新时主动 `service uninstall`（`D:agent-connector.ts:309-312,340-343`）。
- **协议与执行**：WSS 协议 4（服务端严格相等，`external_agents/service.py:36,80`）。读以外的动作执行前 `journal.begin` 记账，同 id 重复返回原回执，执行中崩溃记 `unknown`（`connector.py:404-432`，`journal.py:100-111,185-207`）——这是“恢复不重复点击”的基础。未知资源返回 `invalid_resource`、`admitted:false`、`executed:false`（`connector.py:228,487-498`），**新增资源用能力事实即可，不升协议**。配置原子写入且加锁（`config.py:63,115-122`），每条资源命令重读（`connector.py:127-163,411-412`）。
- **字节通道**：文件 `stat/read/release/write/get`，64 KiB 分块、每块一次往返（`files.py:18`）。设备 → 产物、设备 → 模型像素（`view_file(device)`）已通；缺的“写进 Yuki 环境”属 F08，截图不依赖它。
- **服务端路由与事实**：`device_capabilities = {full_access, platform, shell, tty, interrupt_supported}`，只存在于活连接（`connector.py:202-210`）；`LocalDeviceService.status` 只透出这 5 个键（`local_device.py:72-76`）；按 `device` 路由今天只有 `terminal_exec`/`environment_status`（`sandbox/client.py:102-108`）；设备访问按设备归属本人核验（`local_device.py:44-49`）。

### 4.3 连接器稳定性（2026-11-09 Mac mini 实况）

- Mac mini 连接器 0.4.3 经 Tailscale serve 绕回本机连接；bot 每次重启期间得到一串 `HTTP 502`。11-09 12:49 UTC 还在正常执行操作，此后无声离线；`connector.log` 自上次 bot 重建后无记录；14:09:56 UTC Desktop 重新拉起后 1 秒即连上。网络本身正常。
- 根因（由源码确定；具体那一次因无记录无法复原）：
  1. **致命退出无记录、无重启**：`run()` 对非 401/403 且非 websockets 可重试的 `InvalidStatus` 直接抛出（`connector.py:677-679`）；`connection()` 内除 `ConnectionClosed` 与撤销型 `DriverError` 外的异常都会结束 `run()`（`:652-674`）；CLI 只处理 `OSError/ValueError/httpx`（`cli.py:482-495`），其余只打到 stderr。Desktop 丢弃子进程 stdout/stderr（`D:agent-connector.ts:375-376`），退出时只清引用，不重启、不记退出码（`:367-371`）。
  2. **假死**：连接器把关闭码 1008/4001/4003/4401/4403 与 `revoked` 消息都当作授权丢失，在 `authorization_lost()` 里静默永久等待（`connector.py:666-668,710-714`）。而服务端：从不发送 `revoked` 与 4403；4401 在 `accept()` 之前发出，uvicorn 会转成握手 **HTTP 403**（`http.py:255-256`），连接器实际看到的是 `InvalidStatus 403`；#590 之前任何处理异常都关 1008。
  3. websockets 16.1.1 只重试建连失败（`OSError`、开连超时、`EOFError`、`InvalidStatus` 500/502/503/504），其他状态码与握手错误直接抛出；连接内部异常不重试。心跳 20 s/20 s。
  4. 服务端此前没有带设备 ID 的连接/断开日志。
- #590 已修服务端：连接/断开日志；握手拒绝与 `ClientError` 关 1008（带原因），握手后的服务端失败关 1011，现有 0.4.3 连接器因此会重连。

## 5. 设计

### 5.1 工具合同（一次冻结）

成功结果是业务数据本身；失败 `{ok:false, error, message}`；动作可能已发生时加 `status:"unknown"`。两工具 `namespace="sandbox.run"`（与 `run_command` 同一插件授权，`main_agent_backend.py:566-567`，不放宽不收紧）、`effect="write_state"`、`result_cacheable=false`。合同 revision 对描述与参数做哈希（`main_agent_contract.py:102-131`），所以参数一次定全；`device` 现在就声明，阶段 1 给出 `device` 返回 `device_not_supported`，阶段 3 只放开实现。

```
computer(action, x?, y?, x2?, y2?, dx?, dy?, text?, window?, display?, device?)
  action: screenshot | click | double_click | right_click | move | drag | type | key | scroll | resume
  x,y（drag 另有 x2,y2）为本次结果所附图像的像素坐标；key 的 text 为按键组合（如 ctrl+l、Return）；
  scroll 在 x,y 滚动 dx,dy 格；window 给出时只截该窗口、坐标相对该窗口图像，动作投递给该窗口（§5.5）；
  resume 解除本人的叫停（§5.5）
  成功：{width, height, display?, window?, control:{holder,since}} + 附带动作后的新截图；
        screenshot 另返回 windows:[{id,app,title}]；输入回执为 {submitted:true}（是否生效以截图为准）
  失败：control_held | display_unavailable | display_locked | desktop_unavailable | window_not_found |
        window_unavailable | target_elevated | screen_permission_required | input_permission_required |
        timeout（输入类附 status:"unknown"）| device_not_supported

browser(action, url?, node?, text?, tab?, device?)
  action: open | snapshot | click | type | press | select | hover | scroll | screenshot | close | accept_dialog | dismiss_dialog
  open 给 tab 时在该标签页跳转，否则新开；type 以 text 替换输入框内容（text="" 清空）；select 以 text 选定选项；
  scroll 用 node（滚到可见）或 text=down|up|<px>；snapshot 的 text 为过滤词；
  accept_dialog / dismiss_dialog 处理当前 JS 对话框，accept_dialog 的 text 为 prompt 对话框的输入
  成功：{tab:{id,url,title,loading}, events:[navigated|new_tab|dialog|download|file_chooser], snapshot}
        screenshot 另附页面图
  失败：node_stale（附新快照）| tab_not_found | dialog_open | control_held | timeout（输入类附 status:"unknown"）|
        browser_unavailable | not_allowed | device_not_supported
```

坐标输入只在 `computer`；`browser` 只按节点操作。`browser` 带 `screenshot`，因为 headless 模式没有屏幕。

### 5.2 图像与坐标

- 图像来源（Manager 或连接器）直接产出最终尺寸的 JPEG/PNG：尺寸只由当前显示或窗口几何与当前模型 profile 的最大图像尺寸（读配置）决定；结果返回该 `width/height`。Host 不再缩放或重压缩这类图像。
- `x,y` 永远是“本次调用所附图像”的坐标；执行时按**当下**的几何换算（Retina 倍率、Windows DPI、多屏负坐标、窗口原点）。不保存“最近一次截图”状态，并行 Work 互不影响。
- 截图标 `source="tool"`；`view_file(device)` 同样改标 `source="tool"`。归档规则：凡无 `tool_handle` 且非 `workspace`/`history` 来源的图像都归档为私有工具产物；删除 `local_media` 特例（`capabilities/results.py:192-201`）。这是删除特例，不是新存储。
- 不支持图像的模型（如 DeepSeek）：沿既有规则 `image_capability_unavailable` / `media_read:false`，动作效果保留；`browser` 的快照必须单独够用。不加“某情况自动附截图”的规则。

### 5.3 长循环的图像预算（替换原 D1）

- 组装请求时，超出 `vision.max_frames_per_turn` 或媒体字节上限的**最旧**工具媒体观察，替换为文本：`call_id` 与 `media_artifact_handle`（可用 `read_result` 重读）。请求准入检查 `_check_request_media_budget` 与派发时 `_budget_tool_media` 都以“替换后实际携带的图像”为准；删除按整轮累计不淘汰的统计。
- 不新增阈值、不做付费压缩。Responses 续写链中已由 provider 持有的历史图像不随本次请求重发，本次只计入新携带的图像；实施时以 `deepseek_responses.py` 的续写实现为准写清并测试。

### 5.4 快照格式与浏览器驱动

- **驱动形态**：共享 CDP 驱动是一个**长驻进程**：沙箱里由 `desktop_service.py`（已拥有 Chromium）托管，经 runtime 挂载接受请求；本人电脑上由连接器进程承担。长驻才能保持对话框监听、`Page.setInterceptFileChooserDialog`、下载行为与新标签页归属。驱动只依赖标准库（沿用 `yuki_computer.py` 现有的标准库 WebSocket 实现），放在连接器包 `yuki_agent_connector/cdp.py`，环境镜像与 `install-desktop.sh` 按复制 `yuki_computer.py` 的方式一并复制该文件。上线顺序：先更新环境镜像/Manager，再上 bot。
- **快照**：基于可访问性树；缩进类 YAML 行 `- button "Sign in" [e57]`；去掉 `InlineTextBox`、`LineBreak`、`ListMarker` 与无名 `generic/none`（保留子节点）；连续 `StaticText` 并入父行；显示 `value=`、`checked`、`expanded`、`selected`、`disabled`、`focused`、`required`、`level`；首行 `page: 标题 — url [tab]`。**只做无损过滤，不另设字符上限**；超出走既有结果预算与 `read_result`（`config.py:261`）。`snapshot` 的 `text` 只返回匹配节点及其祖先路径。
- **节点 ID**：主进程 frame（含同进程 iframe）直接用 `e<backendDOMNodeId>`（同进程内唯一）；跨进程 iframe（OOPIF）用其 target/session ID 作前缀。节点死亡时 `DOM.resolveNode` 失败 → `node_stale` + 新快照，绝不按猜测位置点击。封闭 shadow DOM 已包含（实测）。
- **动作**：点击用 `DOM.scrollIntoViewIfNeeded` + `DOM.getContentQuads` 取中心，`Input.dispatchMouseEvent` 可信事件；`press` 发完整键事件；`exceptionDetails` 视为错误；对话框监听 `Page.javascriptDialogOpening`，返回 `dialog` 事件，由 `browser(action=accept_dialog|dismiss_dialog)` 经 `Page.handleJavaScriptDialog{accept,promptText}` 处理；文件选择拦截后以 `DOM.setFileInputFiles` 填入沙箱 `/workspace` 路径；下载 `Browser.setDownloadBehavior` 到沙箱 `/workspace/downloads`、本人电脑上的连接器私有目录，按 guid 报告路径与状态。
- **动作完成**：执行后等到导航/加载事件、对话框、新 target 或下载中的任一项；等待受该动作所在执行器的既有时限约束（沙箱为 Manager 自身的有界等待，§5.6），到时返回 `loading:true`，不是错误。CDP 接收一律带同一时限。
- **标签页**：新标签页按 `TargetInfo.openerId` 归属打开它的标签页所在 Work，仅用于列出与默认解析；不新增“不得关闭他 Work 标签页”的限制。

### 5.5 与本人共用桌面

本人照常用电脑，Yuki 同时干活；叫停是事实，不是计时器。

1. **浏览器不碰光标**：`browser` 经 CDP 向页面投递输入，Yuki 专用 Chrome 窗口可在后台或另一个桌面空间。网页任务优先走这里。
2. **原生应用优先后台投递** [spike，结论与 3b 设计建议见 [3b spike 记录](../operations/computer-3b-spike-20261110.md)；macOS 已实测，Windows 待实机]：`computer` 给出 `window` 时，截图只截该窗口（macOS ScreenCaptureKit 按窗口过滤；Windows `PrintWindow(PW_RENDERFULLCONTENT)`，普通 `BitBlt` 对被遮挡或 GPU 渲染窗口会得到黑图），动作不经全局光标：
   - 在**目标窗口自己的**可访问性子树里按坐标找元素（系统命中测试只返回最上层窗口，对被遮挡窗口无效）：macOS AX 动作（`AXPress`、设置 `AXValue`），Windows UIA `InvokePattern`/`ValuePattern`/`ScrollPattern`。
   - 找不到元素时：macOS 用 `CGEventPostToPid` 把带坐标事件只发给目标进程；Windows 不用 `PostMessage`（对 Chromium/Electron/UWP 与读取键盘状态的应用不可靠），改走第 3 层。
   - 窗口最小化或不存在：直接返回 `window_unavailable`/`window_not_found`，不靠黑图判断。
   - 游戏、画布类、部分自绘应用可能不响应，回执只写 `submitted`，由截图核实；不响应时改走第 3 层。
3. **必须用真实光标时**：省略 `window` 的全局输入。Yuki 的合成事件全部打上自身标记（macOS `kCGEventSourceUserData`，Windows `SendInput` 的 `dwExtraInfo`）；“本人输入”定义为**任何不是 Yuki 产生的输入**（包括屏幕共享、远程控制，这正是 Mac mini 服务器常见的操作方式）。每次第 3 层输入前比较两个事实：自 Yuki 上一次第 3 层动作以来是否出现过非 Yuki 输入；出现过就置为叫停并拒绝。实现候选 [spike]：macOS `CGEventSourceSecondsSinceLastEventType(HIDSystemState)` 配合 Yuki 使用私有事件源（避免事件监听需要的“输入监控”第三项权限与主动监听卡住系统输入的风险）；Windows 低级钩子读 `dwExtraInfo`（注意超出 `LowLevelHooksTimeout` 会被系统静默摘除）。
4. **叫停与恢复**：叫停状态为 `holder=owner` 时，**三层的输入都返回 `control_held`**，截图与快照照常可读。叫停来源：第 3 层检测到本人输入，或 Desktop「停止」。恢复只由本人明确表示：Desktop「繼續」，或本人在聊天里让 Yuki 继续时模型调用 `computer(action=resume, device)`——只按既有“设备归属本人”规则核验（`local_device.py:44-49`），不新增规则。
5. **状态放在哪里**：叫停标志放在连接器**内存**里，由 Desktop 经现有 stdin/stdout 设置与读取；连接器在 stdout 打状态行（见 §5.8.2），Desktop 据此显示托盘与「這台電腦」页的“Yuki 正在操作（后台 / 前台）/ 已叫停”，并在子进程重启后重发标志。不写配置文件、不加 CLI、不新增服务端表或审批（共同约束 §0：刹车信号不逐条持久化）。

沙箱有独立显示，不与本人冲突，接管保持现状；`help-request`/`wait-control` 保留。阶段 1 中沙箱的 `window` 为截取该 X 窗口（Openbox 下 xdotool 定位，`import -window <id>`）、坐标相对该窗口，输入仍为全局 xdotool。

### 5.6 沙箱执行与恢复

- `computer_action`/`browser_action` 加入不设 RPC 截止的方法集合（与 `workspace_*` 同列）；Manager 自身的有界等待是唯一时限，避免 bot 侧 7 s 超时报“不确定”后 Manager 仍在排队执行。
- 输入类动作：先在 `environment_mutations` 写待定行，执行后写结果；重启后有结果返回原结果，只有待定行报 `unknown`，**不照搬 `display_wake` 的“待定即重跑”**（`computer_runtime.py:86-104`）。工具调用键即请求 ID。
- 读类动作（`screenshot`、`snapshot`、`browser screenshot`）不写待定行，重试即重新读取。
- 同一环境的动作在 Manager 内按环境串行；不再把“另一 agent 持有”报成人类接管。

### 5.7 GUI 驱动（连接器）

| 方面 | macOS（14+） | Windows |
| --- | --- | --- |
| 整屏截图 | pyobjc + ScreenCaptureKit `SCScreenshotManager`；`CGWindowListCreateImage` 在 15 SDK 已废弃，作后备 | GDI `BitBlt`（`mss` 或约 150 行 ctypes） |
| 窗口截图 | ScreenCaptureKit 按窗口过滤 | `PrintWindow(PW_RENDERFULLCONTENT)` |
| 输入 | `CGEventPost`；无辅助功能权限时**静默丢弃**，输入前必须 `CGPreflightPostEventAccess` | `SendInput`；对管理员窗口（UIPI）无效，检测到目标为提升权限窗口时返回 `target_elevated` |
| 权限 | 屏幕录制 + 辅助功能；TCC 归“负责进程”，Desktop 子进程模式下应为签名的 Yuki.app、升级后保留 [待实机 `launchctl procinfo`]；ad-hoc/开发版每次重建重新授权；macOS 15 起每月一次的屏幕录制再确认属本人操作 | 连接器在本人登录会话中，不受 session 0 隔离影响；安全桌面（UAC、锁屏）返回 `desktop_unavailable` |
| 坐标 | 全局点坐标、副屏可为负、Retina ×2，按 §5.2 换算 | 启动时设 per-monitor-v2 DPI；`MOUSEEVENTF_ABSOLUTE\|VIRTUALDESK` 归一化 |
| 能力事实 | `device_capabilities.computer = {screen, input, displays:[{id,bounds,scale,primary}]}`；`LocalDeviceService.status` 透出该键 | 同左 |

- 依赖：darwin 标记下 `pyobjc-core`、`pyobjc-framework-Quartz`、`pyobjc-framework-ScreenCaptureKit`、`pyobjc-framework-ApplicationServices`（AX）；win32 用 `mss` 或纯 ctypes、`comtypes`/ctypes UIA。不用 pyautogui、pynput。`build_runtime.py` 加 GUI 导入探针；wheel 仍为纯 Python；三平台 CI 只测事实探针与坐标换算（托管 runner 无法授予 TCC）。连接器不做 Linux GUI。
- pyobjc/libffi 回调在 hardened runtime 下可能需要 `com.apple.security.cs.allow-unsigned-executable-memory`，签名版验证后再决定。
- 截图字节：连接器把截图注册为 `FileTransfers` 源，服务端复用 `download` 的读/释放循环。64 KiB 每块一次往返，因此截图在来源处按 §5.2 产出最终尺寸、优先 JPEG。
- 服务端：两工具带 `device` 时路由到 `LocalDeviceService`（今天只有终端与环境状态会按 device 路由），命令 ID 即调用键；能力缺失的设备直接失败（旧连接器回 `invalid_resource`）。

### 5.8 本人电脑上的浏览器

- 新开 Yuki 专用 Chrome 窗口：连接器找到 Chrome（Windows 注册表 `App Paths`/Program Files/LocalAppData；macOS `/Applications` 或 `~/Applications`），找不到返回 `browser_unavailable`、不下载浏览器；profile 放连接器私有目录；参数 `--user-data-dir=… --remote-debugging-port=0 --no-first-run --no-default-browser-check --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling`（后三项防止窗口被遮挡时截图停住），读 `DevToolsActivePort`；**先连已有实例、失败才启动**（同一 profile 的第二次启动会交给已运行实例并忽略新参数，实测）；首次启动会抢一次焦点；Windows 上以 `CREATE_BREAKAWAY_FROM_JOB` 启动，避免随连接器所在 job 被杀；macOS 上脱离连接器进程组 [待实机]。`--remote-debugging-port` 对本机所有进程开放 CDP，该 profile 不含本人登录态，接受此风险并写入文档。
- Chrome 136+ 不允许对默认 profile 开远程调试，137+ 品牌 Chrome 移除 `--load-extension`，均印证独立 profile。
- 扩展（驱动本人日常 Chrome、使用本人登录态）为可选后续，届时另写任务书；已知要点：`chrome.debugger` 不提供 Browser 域（下载需 `chrome.downloads`）、调试信息栏、MV3 service worker 生命周期、native messaging 宿主需连接器新增本地 IPC；另有 Chrome 144 auto-connect 可对比。

### 5.9 连接器稳定性（连接器 0.4.4 + Desktop）

1. **致命退出写日志**：`cli.py` 的 `run` 对除 `KeyboardInterrupt/SystemExit` 外的任何异常 `logger.exception` 写入 `connector.log` 后非零退出。
2. **授权丢失只认握手 HTTP 401/403**：删除 `revoked` 分支与 4001/4003/4401/4403/1008 关闭码分支；保留协议不匹配（1008 + `unsupported_connector_protocol`）的明确停止；其他 1008 与所有关闭都重连。`connect(process_exception=…)` 让 401/403 以外的 `InvalidStatus` 都按 websockets 自带退避重试，删除 `connector.py:677-679` 的重新抛出。连接器在 stdout 打状态行：`connected` / `disconnected` / `authorization_lost` / `protocol_unsupported`，以及 §5.5 的操作与叫停状态。
3. **Desktop 监督**：子进程非预期退出（非 `stop()/close()`）时按指数退避重启，读到 `connected` 状态行即把退避归零；子进程 stderr 写入与 `connector.log` 同样轮转规格的 `connector-stderr.log`；记录最近一次退出码/信号；「這台電腦」页显示连接状态、最近退出原因与已重启次数，提供“导出诊断”。这是进程监督，不是完成门槛；原回执仍按 journal 规则为 `unknown`。
4. **服务器主机上的连接器用回环地址**：见决定 8；运维文档补一行。
5. **睡眠唤醒**：依赖 20 s/20 s 心跳；可选 Desktop 在 `powerMonitor.resume` 时向 stdin 写一个字节，连接器据此立即重连。低优先。

## 6. 阶段与实施项

| 阶段 | 内容 | 依赖 | 估计 |
| --- | --- | --- | --- |
| 0 稳定性 | §5.9（服务端两项已在 #590） | — | 2–3 天（含 Desktop 发版） |
| 1 Yuki 沙箱 | 工具合同（一次冻结）、长驻共享 CDP 驱动、快照与动作语义、图像与坐标、长循环预算、Manager 动作与恢复、清理过期 CLI 提示与夹具 | — | 5–7 天 |
| 3a 本人电脑·前台 | 连接器整屏截图与全局输入、叫停/恢复（§5.5 第 3–5 层）、能力事实与服务端路由、Yuki 专用 Chrome 窗口、Desktop 状态与权限页 | 0、1 | 共享 4–5 天 + macOS 4–6 天 + Windows 3–4 天 + 浏览器 3–4 天 + Desktop 3–4 天 |
| 3b 本人电脑·后台 | §5.5 第 2 层窗口截图与后台投递 | 3a 与 spike 结论 | 约 2–2.5 周（[spike 记录](../operations/computer-3b-spike-20261110.md) §6；Windows 以实机脚本结果为准） |

阶段 0 与阶段 1 可并行；阶段 1 完成即可让 Yuki 自己的电脑可用；3a 单独可交付。F08（本人电脑/产物 → Yuki 环境）按其任务书独立排期。

### 6.1 阶段 1 文件清单

1. `sandbox/environment_tools.py`：两工具声明加入 `SANDBOX_TOOLS`；`run_command` 说明删去 `yuki-computer`。
2. `sandbox/client.py`：`MODEL_TOOL_RPC_METHODS` 增 `computer→computer_action`、`browser→browser_action`；加入 `_MUTATING_METHODS`；加入不设截止的方法集合；使用大响应上限。
3. `sandbox/persistent.py` / `computer_runtime.py`：两方法交给 `ComputerRuntime`——按环境串行、输入类写待定行、必要时唤醒显示、进程内取租约（actor `{work_id, request_id}`）、经 runtime 挂载请求长驻驱动、从 runtime 目录读图、`finally` 释放租约。删除文件命令协议 `collect_commands`/`read_command`，**保留** `observe_display()` 于处理循环。
4. `sandbox/computer_control.py`：`agent_acquire` 接受请求 actor、不再要求 `run_id`；删除已无用的 `finish_run` agent 租约释放。
5. `sandbox/desktop_service.py`：托管长驻驱动。
6. `sandbox/yuki_computer.py`：删除 `operation`、`input_lease`、`wake/sleep`、浏览器子命令与不可达分支；保留 `status`、`help-request`、`wait-control`；屏幕输入改为按租约参数执行且**每次输入（含 xdotool）前复核**，补滚动、双击、右键、移动、拖拽。
7. 连接器包新增 `yuki_agent_connector/cdp.py`（§5.4）；`Dockerfile.environment-desktop`、`install-desktop.sh` 复制该文件。
8. `services/agent_tools.py`：来源图像转 `ChatImage`，标 `source="tool"`，作为 `PreparedMediaData` 返回。
9. `capabilities/results.py`：§5.2 的归档规则，删除 `local_media` 特例；`view_file(device)` 改标 `source="tool"`。
10. `services/agent_runner.py` / `turn_execution.py`：§5.3 的图像替换与两处预算统一。
11. `main_agent_contract.py` 合同版本 35 → 36。Code Mode：`codemode/tool_visibility.py` 增两名；脚本拿到的是不含像素的业务数据（含 `media_artifact_handle`），每个嵌套调用有自己的调用键；只新增名称，`CODE_API_REVISION` 不变；不重设计 Code Mode（#573）。
12. `client_api/work_projection.py`：两工具映射到「操作電腦」「使用瀏覽器」类别。
13. 文档：`tool-kernel.md`（27→29、Manager 工具如何返回媒体）、`tool-results.md`（截图帧与替换规则）、`desktop-computer.zh-CN.md`、`local-agent-connector.zh-CN.md`、F08 任务书 §2 的旧工具名。
14. 测试与 CI：先写失败清单；`desktop-headed` job 增一条 E2E 并上传 `test-results/computer-browser/`；更新 27 工具断言；修复 `desktop_computer_work_fixture.py`。

## 7. 失败用例（写代码前须逐条落实）

### 7.1 合同与恢复

- C1 直调与 Code Mode 清单不是 29 项、合同版本未变、与 `yuki_<name>` 包装冲突。
- C2 namespace/effect 缺失，导致截图被缓存或与点击并行。
- C3 阶段 1 给出 `device` 却在本地执行，而非 `device_not_supported`。
- C4 同一调用键重试时重新点击/输入，而非返回保存的结果；读类动作重试却返回 `unknown`。
- C5 Manager / 连接器 / bot 在待定与结果之间崩溃后报告成功或重跑，而非 `status:"unknown"`。
- C6 bot 侧 RPC 超时后 Manager 仍执行了排队中的动作。
- C7 两个 Work 同时操作得到 `control_held`，而非串行。
- C8 沙箱本人接管后下一次输入仍被执行（xdotool 路径、解析与派发之间）；交还后 `wait-control` 不完成。
- C9 动作失败/超时后 agent 租约未释放；撤销 Desktop 设备后租约未释放。

### 7.2 图像

- M1 四种方言中任一没有把截图带进请求。
- M2 截图以 `source="workspace"` 发送，下一帧覆盖后复核失败；`view_file(device)` 不再归档或 `read_result` 图像被重复归档。
- M3 截图超过响应行上限或被 32 KiB 输出截断。
- M4 不支持图像的模型丢失了点击效果（应 `media_read:false` 且效果保留）。
- M5 30 步循环在第 17 张截图时被请求准入拒绝；被替换的旧图无法用句柄重读；Responses 续写重复计入 provider 已持有的图像。
- M6 图像坐标与点击坐标不一致（Host 二次缩放、Retina、Windows DPI、多屏负坐标、窗口原点）；并行 Work 使用了对方截图的坐标。

### 7.3 浏览器

- B1 页面加载完成前快照；跳转后旧节点点中错误元素（应 `node_stale`）。
- B2 跨站 iframe 表单不可达；OOPIF 节点 ID 在 frame 增减后指向错误元素。
- B3 JS 对话框让调用挂起；`accept_dialog`/`dismiss_dialog` 未用 `Page.handleJavaScriptDialog`；回车不提交表单；文件输入弹出原生选择框。
- B4 下载落在工作区或连接器目录之外、未返回路径；回放再次触发下载。
- B5 新标签页不可达或归属错误。
- B6 巨大页面的快照被截断而不是经结果预算与 `read_result` 续读。
- B7 中文与 Unicode 输入在浏览器或 xdotool 中出错。
- B8 Yuki 专用 Chrome：重复启动拿到无调试端口的实例；被遮挡时截图停住；本人关闭窗口后无法重新打开；随连接器重启被杀；误动本人的 Chrome。

### 7.4 本人电脑

- D1 macOS 未授予屏幕录制/辅助功能时静默无效（应 `*_permission_required`，输入前预检）。
- D2 屏幕锁定、显示器休眠、安全桌面时报告成功（应 `display_locked`/`desktop_unavailable`）。
- D3 UIPI 下 `SendInput` 被声称生效（应 `target_elevated` 或只写 `submitted`）。
- D4 旧连接器收到 `computer` 命令时未得到 `invalid_resource`；`get_environment(device)` 丢失 `computer` 能力事实。
- D5 `full_access` 关闭时仍执行。
- D6 点击成功但图像归档失败时丢失效果（应 `media_read:false`）。
- D7 后台动作移动了本人光标或抢走前台焦点；被遮挡窗口截图为黑或旧内容；最小化窗口未返回 `window_unavailable`。
- D8 本人输入（含屏幕共享/远程控制）未被识别为叫停；Yuki 自己的合成输入被误判为本人输入；模型思考期间的本人输入被漏掉。
- D9 叫停后第 1、2 层仍在输入；恢复由计时器而非本人明确表示触发；`resume` 被非设备归属人触发。
- D10 Desktop 重启连接器后叫停标志丢失。

### 7.5 稳定性

- S1 重部署期间 502/503 被重试并记日志；S2 代理返回 404/421/426 也被重试；S3 服务端内部错误 → 1011 → 重连（#590 已覆盖服务端）；S4 握手 HTTP 401/403 → `authorization_lost` 状态行 → Desktop 显示“授权已撤销”；S5 未捕获异常 → `connector.log` 栈 + 非零退出 → Desktop 退避重启并显示原因；S6 崩溃循环有退避且计数可见、无忙等，读到 `connected` 后归零；S7 Desktop 退出与本人点击停止时不重启；S8 睡眠唤醒后重连；S9 重启时进行中的终端/Agent 操作回执为 `unknown`，绝不重派；S10 服务端日志能对上每台设备每次断开的时间与原因。

### 7.6 历史与界面

- H1 Client API 的 Work 步骤未归入「操作電腦」「使用瀏覽器」；旧 `run_command`/`yuki-computer` 回执与 help run 投影出错。
- H2 文档、提示词或夹具仍引用 `terminal_exec` 或 `yuki-computer` 浏览器流程。
- H3 `desktop-headed` CI job 没有运行新 E2E 或没有上传产物。

## 8. 验证

- **阶段 1**：`desktop-headed` job 中一条离线全应用 E2E（脚本化模型），对本地夹具站点：多步表单、回车提交、`<select>`、下载链接、`<input type=file>`、`alert/confirm/prompt`、**跨站** iframe（`localhost` 与 `127.0.0.1` 两个源，才会形成 OOPIF）、第二个标签页、30 步截图循环；中途插入本人 `take` 再 `return`，确认 Yuki 继续。产物上传 `test-results/computer-browser/`：29 工具清单、各帧图像、四种方言请求体（含图像观察消息与被替换的旧图文本）、服务端记录的输入日志、租约时间线、回执。另用真实模型跑若干真实网页任务并保存产物，记录成功率与每步耗时（作为基线，不作上线门槛）。
- **阶段 0**：真实连接器进程对真实 ASGI 服务端的 E2E，服务端依次返回 502、404、握手 403、1011 关闭与正常关闭，断言重试/停止分类、`connector.log` 与 stdout 状态行；Desktop 监督在 Mac mini 实机演练：杀子进程、断网、bot 重启、睡眠唤醒，记录 `connector.log`、`connector-stderr.log`、Desktop 诊断与服务端 `connector_connected/disconnected` 三方时间线。
- **阶段 3a/3b**：脚本化的实机测试程序在 Mac mini 与 Windows 上运行签名版 Desktop：权限授予前后、多屏与缩放、锁屏、叫停/恢复（含屏幕共享输入）、离线重放、专用 Chrome 被遮挡截图，产出可复现的产物（截图、回执、状态行时间线）；`launchctl procinfo` 证明负责进程为 Yuki.app。3b 先交付 spike 结论。
- 通用：`ruff`、`mypy --platform linux src`、单元与集成全量；连接器包三平台 CI；本地 macOS 环境失败的测试须核对其是否掩盖了本次改动的真实失败。

## 9. 复审记录

2026-11-09 独立复审（未提供作者结论）提出的必改项均已并入：长循环预算改为请求组装时替换旧图（§5.3）；坐标空间与来源处定尺寸（§5.2）；归档规则改为按 `tool_handle`/来源并删除 `local_media` 特例（§5.2）；Manager 动作不设 bot 侧 RPC 截止（§5.6）；叫停覆盖三层、本人输入以“非 Yuki 产生”为准并逐次比较事实（§5.5）；恢复路径定为 Desktop「繼續」与 `computer(action=resume)`（§5.5）；删除 `refresh_config` 失败沿用旧配置的防御分支；授权丢失只认握手 401/403，状态经 stdout 行告知 Desktop（§5.9）；工具参数一次定全（§5.1）；浏览器驱动改为长驻进程、对话框用 `Page.handleJavaScriptDialog`（§5.4）。建议项中：读类动作不写待定行、快照不设字符上限、OOPIF 用 target ID、不新增标签页关闭限制、后台投递改为窗口子树查找并去掉 Windows `PostMessage`、`PrintWindow(PW_RENDERFULLCONTENT)`、专用 Chrome 防后台节流与脱离 job、服务端能力透出与 device 路由、保留 `observe_display`、共享驱动位置与上线顺序、Code Mode 返回形态、阶段 3 拆为 3a/3b、F08 移出本书、连接器不做 Linux GUI，均已采纳。
