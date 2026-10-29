# 本机浏览器接入调研：复用 CDP 与统一终端

范围更新：用户在实施中决定本机 Shell 直接运行 OpenCLI 等外部程序，只补统一文件管线。本文件下文保留当时的调查与候选；自研/内置浏览器 CLI、CDP 抽取、专属回执与打包已撤出本期，以现任务书第 7 节为准。
日期：2026-10-29。范围：只读调研与任务设计，没有调用真实用户浏览器、读取登录资料、安装组件、操作生产服务或实施功能。

本文的源码/官方资料是调查证据。按[复审后的统一任务书](../../Yuki-桌面端本机完整操作统一接入任务书-2026-10-29.md)，本机浏览器 CLI 须内置于配套 Connector runtime，由桌面端统一安装升级；是否常驻交互、命令名及结果通道才是待验证接线。普通 terminal_control 维持原样 stdin。下文的 headless/OpenCLI/服务器资料属于调研，不能据此增加本期施工范围。

代码基线：Yuki `02ce928ee35a4e891082cd40edd96a79f348d963`；Yuki Desktop `9114d043ebcf46c14b694b279abf2c31b71be5a1`。已阅读两仓现有说明，以及 Yuki 的 `AGENTS.md`、`development-contract.md`、`persistent-environment.zh-CN.md`、`desktop-computer.zh-CN.md`。Desktop 树内没有 `AGENTS.md`。

## 判断

可行，而且没有必要再做一套浏览器工具、Playwright 调度或 VNC。优先把现有 `yuki-computer` 的 CDP 动作部分抽成可跨平台运行的模块，随桌面端已经携带的 Connector Python 运行时交付，通过同一套本机 `terminal_exec/read/control` 调用。浏览器只是本机可以运行的程序；Yuki 自己决定动作，不必委托 Codex 再间接操作，也不是替 Codex 设置 `full-access` 参数。

首版应完成“连接用户当前浏览器、使用原登录状态、观察页面、操作页面、把截图和下载结果交给 Yuki”的闭环。只打开一个新 profile 的 Chromium 不算完成。云端持久 Linux 环境继续沿现行 Manager 合同运行，本机离线不隐式切到另一台电脑执行原操作。

用户使用已安装且本期实测支持的浏览器，无需手工另装本期 CLI、Python、Node、MCP 或 OpenCLI。本期不捆绑浏览器本体，不给 Yuki 自己环境安装/启用浏览器、扩容沙箱或建设 VNC；源码里存在 Browser 动作不能证明线上已经安装可用浏览器。

## 当前已经有什么

| 事实 | 源码位置 | 复用边界 |
| --- | --- | --- |
| 主 Agent 经普通终端运行 `yuki-computer`，浏览器没有第二套模型工具 | `docs/operations/desktop-computer.zh-CN.md:40`；`docs/operations/persistent-environment.zh-CN.md:15` | 沿现有终端合同扩展真实本机 target，不增加 `browser_*` 工具清单或每步 Work |
| CDP 支持列页、建页、关闭、AX 快照、按节点点击、输入、按键、滚动、PNG | `src/yuki/sandbox/yuki_computer.py:91`、`:146`、`:179`、`:195`、`:208`、`:306`、`:318`、`:349` | 复用动作和返回语义；提取时让 CDP 连接/target 解析成为输入 |
| 当前 CDP 固定在容器 `127.0.0.1:9222`，每次调用重新开 WebSocket | `src/yuki/sandbox/yuki_computer.py:48`、`:97`、`:104`、`:125` | 必须去掉“9222 等于目标浏览器”的假设；当前逻辑不是本机浏览器发现实现 |
| 当前图形服务把 Xvnc、Openbox、Chromium 作为一棵子进程树启动；profile 留在持久 HOME | `src/yuki/sandbox/desktop_service.py:34`、`:38`、`:71`、`:103` | 保留服务器这条实现；不能拿其 `DISPLAY=:1`、`--no-sandbox`、固定端口去启动用户本机 Chrome |
| Linux 专属部分包括顶层 `fcntl`、文件锁、Manager 文件邮箱、`xdotool` 和 ImageMagick `import` | `src/yuki/sandbox/yuki_computer.py:7`、`:51`、`:71`、`:134`、`:313`、`:325` | 不能直接把整份文件加入 Windows/macOS Connector 后声称已跨平台 |
| Manager 的 `tabs_list` 先要求远端 display 运行 | `src/yuki/sandbox/computer_runtime.py:57` | 本机 CDP 不应再依赖服务器 Xvnc 的 display 状态；真实可用性从所选浏览器连接取得 |
| Desktop 电脑页是 noVNC RFB viewer，画面来自服务器签发的 display URL | Desktop `src/renderer/views/Computer.tsx:4`、`:410`、`:415`；`src/main/computer-tickets.ts:6` | 可保留远端电脑页；它既不发现也不控制用户本机 Chrome |
| 现有 Connector 由本机主动连出，已有设备与原操作日志 | `packages/yuki-agent-connector/src/yuki_agent_connector/connector.py:109`、`:131`；`src/yuki/external_agents/transport.py:14` | 共用连接、设备身份和将扩展的本机终端；不能把现有 Coding Agent 的 instance/prompt 语义直接冒充浏览器执行 |
| Connector 已随安装包携带 Python 3.12 和 websockets，构建覆盖 Windows x64、macOS 双架构、Linux 双架构 | `packages/yuki-agent-connector/pyproject.toml:6`、`:9`；`packages/yuki-agent-connector/scripts/build_runtime.py:19`、`:21`；Desktop `electron-builder.yml:8` | 原生 CDP 路线不需要额外下载浏览器或 Node/MCP 运行时；新模块仍须进入相同平台包验证 |

“Electron 能运行网页”也不等于能够控制 Chrome。Electron 的 `webContents.debugger` 操作的是对应的应用内 `webContents`；它不会自动继承外部 Chrome 的标签或账号。把网页搬进新的 Electron 窗口，还会换一个 browser context，不能把这个结果叫“复用本机现有登录态”。见 [Electron Debugger 官方说明](https://www.electronjs.org/docs/latest/api/debugger)。

现有 CLI 也不等于完整浏览器产品：它未实现下载进度/完成事件订阅，AX 快照没有为跨进程 iframe 做完整 target 遍历，亦没有通用网络观察或文件上传入口。首版至少补实际下载完成与文件回传；其他能力沿同一 CDP 会话按真实任务扩展，不另建执行链，也不宣称当前可处理所有网页。

## 当前登录态究竟怎么接

### Chrome 136 的限制与 Chrome 144 的新入口要一起读

Chrome 136 开始，`--remote-debugging-port` 和 `--remote-debugging-pipe` 不再对默认用户数据目录生效，使用旧参数必须指定非默认 `--user-data-dir`。另开 profile、复制 Cookies 文件或退出用户 Chrome 再强行重启，都不是“无缝接入原浏览器”的依据；本次不设计复制登录数据库或解密凭据。见 [Chrome 官方变更说明](https://developer.chrome.com/blog/remote-debugging-port?hl=en)。

Chrome 144+ 又增加了用户显式启用的现有会话接入：在运行中的 Chrome 打开 `chrome://inspect/#remote-debugging` 允许远程调试，连接时由 Chrome 自身显示是否允许本次调试会话，并显示自动化连接状态。官方 `chrome-devtools-mcp --autoConnect` 使用这一能力，可以保留当前标签、扩展和登录上下文。两者是不同入口，不矛盾，也不能因为新入口存在就忽略浏览器本身的许可。见 [官方现有会话说明](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session) 与 [当前 auto-connect 使用说明](https://developer.chrome.com/docs/devtools/agents/use-cases/auto-connect)。

接入后是在用户原账号下操作；站点已有权限、登录过期、企业策略和浏览器自身拒绝仍按真实结果返回。Yuki 的“允许本机访问”不改变这些事实，也不意味着把浏览器 profile 或 Cookies 上传到服务器。页面内容、截图及用户选择交付的文件会沿任务进入云端，这是实际数据流，应在连接说明中讲清。

### 不必为此引入整套 MCP

本轮只读核对官方 `ChromeDevTools/chrome-devtools-mcp` 的 `63170b11a2fa74d5ceea757194d7b25f3e145f68`：`src/BrowserManager.ts:382-405` 从显式 `userDataDir/DevToolsActivePort` 读取端口与 WebSocket 路径，`:432` 将该 endpoint 交给 `puppeteer.connect`。这说明连接浏览器使用的是 CDP WebSocket，MCP 并非浏览器协议的硬依赖；Yuki 可以复用已有 `websockets` 实现连接与 CDP 动作。见 [固定源码](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/63170b11a2fa74d5ceea757194d7b25f3e145f68/src/BrowserManager.ts#L382)。

**这里已证明官方存在原会话 CDP 接入路径，尚未证明 Yuki 的 Python 客户端已在三平台成功接入。** 必须做真实验证：Chrome 显式启用后产生的 endpoint、连接许可、浏览器级 socket、`Target.getTargets/attachToTarget`、flattened session 路由和原页面输入。不能继续假定新入口一定提供旧的 `/json/list` 与独立 page socket。见 [CDP Target 官方协议](https://chromedevtools.github.io/devtools-protocol/tot/Target/)。

浏览器版本、安装位置、channel 与 profile 取真实现场资料，不扫一串目录然后默认挑第一个账号。多个真实 profile 需要指定目标；这属于目标消歧，不是项目目录门槛。用户选的是 `user data dir` 根，不能把其下 `Default`/`Profile 1` 子目录混为同一个参数。连接失败说明实际失败，不自动复制资料、另起新 profile 或切服务器浏览器假装成功。

## 候选接线路线

1. **提取本机所需 CDP 动作，而不是复制一套。** 协议动作和快照格式保持一份轻量源码，随 Connector 的本机 CLI 打包；原 Linux 调用受影响时做必要适配和回归，不新增服务器浏览器交付。`fcntl`、容器服务和 X11 输入留在原层，不把 Bot 的 ORM/插件/Manager 整体装进本机包。
2. **验证本机 CLI 与统一终端接线。** 可验证由本机 `terminal_exec` 启动交互进程并保持 CDP 连接的路线。命令、输入/结果协议属于设计示意，尚非原终端已有接口；先证明原调用如何配对真实页面结果，再选最小接线。普通 `terminal_control(input)` 继续原样交付 text，不把任意 stdin 升格成浏览器业务协议。浏览器能力无需按点击建立 Work、native prompt 或另一套模型工具。
3. **连接 owner 留在本机。** Chrome 的许可作用于调试连接，保持 browser socket 与 target session 可避免每步重新请求连接。页面操作应保发起它的原调用关联与实际结果；具体 CLI/结果通道验证后确定，不另造模型恢复键或动作数据库。ACK 丢失查原事实，不重做点击、提交或下载；原命令也不能保证任意站点业务恰好执行一次。关闭完整访问后未派发的受控动作不再执行，原查询/取消及已发效果保留；清理自己的连接而不关闭用户 Chrome。无需预定每动作读配置文件或新建控制队列。
4. **先接明确选中的已有浏览器。** 同一 CLI 可以接受已授权的显式 CDP endpoint，或读取用户选择的 Chrome 144+ profile endpoint；两个入口共用一个 CDP client。需要独立 profile/headless 时再作为显式启动选项，不能作为复用现有登录态的隐式替代。现代 Chrome headless 与 headful 共享实现，[官方说明](https://developer.chrome.com/docs/automation-and-testing/headless) 不支持为二者再造两套动作引擎。
5. **保留既有服务器职责，不扩大本期。** 服务器原终端、文件和现有调用继续按原合同使用；实际是否装有浏览器另认现场，本期不为其安装或验收新的浏览器环境。本机有独立 owner、文件系统和 run，不把服务器 DISPLAY/租约当成本机授权；协议、设备连接、观察、取消和媒体发布复用原链。

同一终端合同并不等于把 Linux Bash 字符串直接发到 Windows。调用本机 CLI 要沿实际打包的 Python 路径与参数执行；浏览器动作 JSON 与 CDP 本身可跨平台，shell/PTY 和平台路径的实现由统一终端模块解决。此处不再增加一层 browser-daemon 或为这一 CLI 单独开公网调试端口。

文件上传是现有 CLI 缺失的动作，须计入实现增量。最短页面路线是用 [CDP `DOM.setFileInputFiles`](https://github.com/ChromeDevTools/devtools-protocol/blob/a45ae55ca46eeb4c6768011da4470ec0f103d01c/pdl/domains/DOM.pdl#L681) 设置真实文件输入节点，传 Chrome 所在本机能打开的路径；云端已有附件先经共同文件桥保存到本机。以隔离测试站实际收到的 multipart 文件字节验收，不把文件名已显示视为上传完成。此处不承诺系统原生选文件对话框、所有拖放组件或所有跨进程 iframe 均已实现。

下载也不只有一个回调：[官方 `Browser.pdl:140-196`](https://github.com/ChromeDevTools/devtools-protocol/blob/a45ae55ca46eeb4c6768011da4470ec0f103d01c/pdl/domains/Browser.pdl#L140) 明确 `suggestedFilename` 不是最终磁盘名，完成事件的 `filePath` 依平台可以缺失，亦不保证文件存在。`setDownloadBehavior` 作用于 browser context，不能把改变真实用户 profile 全部下载行为当成某一标签的局部操作。实施时须按原 guid/frame 和实际文件确认要导出的原件；路径未知保留未决，不能猜文件名或读取另一条下载冒充成功。这部分须用实际 Chrome 版本验证，不能仅凭协议有方法就写成已验收。

## 截图和下载必须真正交付

目前本机 Native 连接是 JSON 命令/事件流（`external_agents/http.py:192-202`、`external_agents/transport.py:14-31`），`DeviceConnection` 没有文件字节流接口；本机 `C:\...\capture.png` 不是服务器 Manager 能打开的文件。把路径打印到 stdout，或让模型看见 Base64 文本，都不等于模型已经观察图片、也不等于聊天收到了附件。

最小补齐项应属于所有本机能力共用的文件桥，而不是浏览器专属上传器。文件原件和临时观察图有不同消费者，不能每张图一律发布长期成果：

1. 本机截图/下载落到真实本机文件；依原设备、run 和原文件传输操作选定要交付的版本。下载完成取浏览器真实完成事件或真实文件结果，不能以开始下载或文件名出现冒充完成。
2. 通过统一设备传输把文件流交给服务器私有临时文件，保原 transfer/request ID、来源设备/run、名称、字节数和内容 hash。浏览器内容、Shell 产物与 Coding Agent 产物共用这一条路径；不用 Base64 大字段挤 stdout，也不为页面另加一套 artifact 存储。
3. **临时观察图**：在事务外接收并核对所选版本，复用 `WorkspaceInspector(artifact_id=None, file_metadata=..., data=...)` 与 `NativeMediaPreparer` 的字节入口，接现有 `ToolArtifactRepository.write_media_artifact` 保存私有 prepared pixels，给模型返回原 `tool_handle`。这不是新的成果发布，不为每张截图创建 `WorkspaceStore` snapshot。
4. **明确发送或保存为成果的文件**：拿真实服务器 FD 调用现有 `WorkspaceStore.snapshot(...)`，生成原 ID 关联的不可变 artifact，沿 `send(artifact_id=..., attachment_kind=...)` 交付。已发布图也可沿 `workspace_inspect(artifact_id=...)` 观察；原本机文件后续变化不改已经发布的字节。
5. 连接/确认丢失查原传输、私有媒体或发布回执；不知道结果就保留未知，不重复下载、重新点击、重发附件或重新生成交付身份。正常临时接收文件按其原 owner 的发布/回收生命周期处理。

现有复用点：`src/yuki/workspace/store.py:215-295` 的 FD 快照；`sandbox/persistent.py:773-787` 的 `workspace_publish`；`workspace/service.py:211-233` 的原发送发布请求；`workspace/tools.py:23-35` 的 artifact 观察；`social/tools.py:17-30`、`social/service.py:1141` 的 artifact 发送。**`workspace_publish` 当前是打开服务器环境路径再调同一个 Store，不是现成的本机文件上传 API。** 应复用其不可变快照和原请求合同，新增真实设备文件来源，不能把本机路径塞进去。

临时观察的现有基础为 `workspace/inspect.py:39-54`、`workspace/service.py:109-110`。必须补的真实接线是：`capabilities/results.py:158` 当前明确排除 `provider_id="core"`，所以不能声称本机 `workspace_inspect` 的 core 结果已经自动进入私有媒体。实施让这条实际路径显式复用 `tool_results/artifacts.py:349-378`，按现有 `source="tool"`/`tool_handle` 与 `validate_media` 消费；不伪造另一个 provider，也不继续把本机 `workspace_path` 交给服务器 Manager 的 `workspace_media_validate`。保留原设备/传输/执行来源、内容 hash 和当前真实 `ArtifactAccess`，不另建图片数据库。

### 存放位置与回收

| 内容 | 拟议位置和生命周期 |
| --- | --- |
| 用户文件与浏览器下载原件 | 留在本机实际路径；不会因云端观察、发给聊天或任务结束被自动移动/删除 |
| Yuki 自动截图 | Connector 管理的本机临时缓存，目录取现有安装的 OS 数据/缓存位置；只清自己的临时文件，原运行/未确认传输仍需的文件保留，不套用服务器路径 |
| 云端单次观察的接收原件 | 文件桥的私有临时接收文件；prepared pixels 已按原请求确认保存在既有私有资料后，按原 owner 清退临时原件，不永久再放一份公开成果 |
| 给模型观察的 prepared pixels | 现有 `ToolArtifactRepository` 私有工具媒体与原 `tool_handle`，沿其可配置 TTL 和真实保护引用回收 |
| 明确发送/保留的成果 | 现有 `WorkspaceStore` 不可变 snapshot；此库没有 TTL，不能把临时截图策略套上去 |
| 需要服务器软件处理的副本 | 任务明确需要时才复制到服务器持久环境；它是另一份真实文件，普通文件按环境现行合同保留，不整盘同步 |

`WorkspaceStore._initialize:68-69` 已移除旧 `expires_at`；其 `cleanup:139-177` 只清损坏 metadata、manifest 不再引用的 blob 和遗留 `.pending`，不会按年龄清掉有效 snapshot。因此“所有截图先 snapshot，再等 GC 自动清”不成立。

私有工具媒体的 TTL 默认来自 `config.py:270` 的 86,400 秒，可配置；`tool_results/artifacts.py:660-713` 仅删除到期且不再受保护的项。现有 `_protected():65-93` 保护原 Work/祖先的活动或近期保留期，以及真正登记的 `artifact_refs`（work_note、observation、summary）；这不是“24 小时一律删”，也没有“全部已付请求自动成为 tool_handle 引用”这一 owner 类型。

Work 原效果的图片另沿 `runtime/work_journal.py:766-796` 保存私有对象，已付请求/检查点所需原图也按其原 owner 的冻结与引用 GC 保留，不用重新读取本机文件或重新截图重建已付请求。新文件桥只接现有生命周期；要验证这些原消费者，不能为了延长每张本机截图而新增永久副本或第二套媒体库。会话入站缓存的 24 小时寿命不直接套到本机原件、Connector 截图缓存或已发布成果。

Desktop `src/main/media/uploads.ts:48-76`、`:133-185` 与后端 `src/yuki/client_api/http.py:429-444` 已有流式上传、hash 和原请求回执模式，可借用实际字节接收及回执组件；它们的 `client.upload`/Person 归属不直接改名冒充 Native 执行，文件导出也不制造新的用户聊天事件。具体文件流帧/HTTP 接收选择随统一设备协议确定，本调研不另立第二条外部身份或上传凭据。

## OpenCLI 的位置

只读核对官方 `jackwener/OpenCLI` `97dc0cd5214b9602bcda78d7422f231ee8a803e9`，package 为 `2.0.2`、Node `>=20.18.1`。它用浏览器扩展和本地 daemon 复用 profile 登录态，提供 DOM/AX、点击、输入、截图和站点命令；扩展需要 debugger 等真实权限。见 [固定 README](https://github.com/jackwener/OpenCLI/blob/97dc0cd5214b9602bcda78d7422f231ee8a803e9/README.md)、[package](https://github.com/jackwener/OpenCLI/blob/97dc0cd5214b9602bcda78d7422f231ee8a803e9/package.json)、[扩展 manifest](https://github.com/jackwener/OpenCLI/blob/97dc0cd5214b9602bcda78d7422f231ee8a803e9/extension/manifest.json)。

它适合作为用户已经安装的可选本机 CLI，通过同一个本机终端使用，尤其适合已有站点命令。首版不必强制安装、嵌入它的独立 App，或把其 daemon 再套一层 Yuki daemon。它的 automation window 与当前手工页面不完全相同，不能只凭“登录态复用”就声称原标签的复杂临时页面状态也已复用；要验收目标页面和 profile 的真实行为。Chrome debugger 扩展也是 [CDP 的另一种传输](https://developer.chrome.com/docs/extensions/reference/api/debugger)，能力受浏览器真实域、页面和企业策略限制。

OpenCLI 与 Chrome DevTools MCP 的仓库许可证均为 Apache-2.0；Yuki/Connector/Desktop 当前为 AGPL-3.0-only。原生 CDP 路线复用已有 websockets，不引入这两个整包依赖。以后确需复用其源码/随包交付，保留各自 LICENSE、版权及实际第三方 notices，并在锁定版本下核依赖；通过用户已有 CLI 运行不等于可以删其许可文件。官方 [OpenCLI LICENSE](https://github.com/jackwener/OpenCLI/blob/97dc0cd5214b9602bcda78d7422f231ee8a803e9/LICENSE)、[Chrome DevTools MCP LICENSE](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/63170b11a2fa74d5ceea757194d7b25f3e145f68/LICENSE)。

## 浏览器自动化与操作整台桌面分开验收

浏览器自动化是 CDP 对页面、标签和网页内容的操作，包括页面截图；无需为此启动本机 Xvnc 或传输整屏视频。桌面 Computer Use 还包括其他应用、系统窗口、真实鼠标键盘、多屏、窗口缩放和操作系统授权。网页的 CDP 输入不能自动点击 Windows 文件对话框、UAC 或任意第三方应用。

| 范围 | 跨平台实际成本 |
| --- | --- |
| Chrome CDP | 共用动作协议；差异主要是浏览器发现、profile、实际 endpoint 和打包路径。先承诺已验证的 Chrome，Edge 等 Chromium 品牌逐项实测；Safari/Firefox 不列为 CDP 首版已支持 |
| 截图/媒体 | CDP 截图跨平台；普通文件桥、浏览器下载位置与文件名处理要三平台验证，不依赖服务器 Linux 路径 |
| 系统画面 | Electron `desktopCapturer` 能捕获屏幕/窗口，不能单独提供系统输入；macOS 屏幕权限与 Linux PipeWire/portal 仍存在，见 [官方 API](https://www.electronjs.org/docs/latest/api/desktop-capturer) |
| 系统输入 | Windows SendInput 受真实 UIPI 完整性级别约束；Wayland 的 RemoteDesktop portal 有自己的设备授权和会话。不能把 Linux `xdotool` 搬去所有系统，见 [Microsoft SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)、[XDG RemoteDesktop](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html) |

因此统一设备入口应留下真实能力区分，不能把“浏览器已接通”显示成“整台桌面可控”。本轮浏览器方案不新增第二套 VNC；将来系统级 Computer Use 复用设备连接、终端/文件来源与实际执行回执，另补相应 OS 执行器和验证。

## 理论代码量与资源判断

以下按功能块估算，非实测工时或提交承诺；不包含统一本机终端、设备授权和通用文件桥的主体实现，避免与主任务书重复计数。

| 浏览器增量 | 生产代码估算 | 说明 |
| --- | ---: | --- |
| 提取并复用现有动作 | 约 190–240 行移动 | 当前 `Browser` 与分发约这个量级，不应在 Connector 再复制一份；移动不计为新增 |
| 实际 profile/endpoint 选择和发现 | +120–250 | Windows/macOS/Linux 路径、channel、显式配置及真实错误；不靠静默重启浏览器补齐 |
| 交互 CLI 与持久 browser/target session | +150–300 | 复用现有 CDP 动作；输入/输出沿已计划的本机终端，含浏览器级 Target 会话适配 |
| 下载完成事件与原本机文件关联 | +80–180 | 确认真实下载结果；字节上传复用通用设备文件桥，不在此重复实现 |
| 页面文件上传 | +20–60 | 基础 file input 的 CDP 动作；不含系统选文件对话框或另一套文件桥 |
| CLI 动作确认与关闭完整访问 | +80–180 | 稳定动作 ID、派发前事实和原结果、已排队动作撤销；复用原 journal 原语，不新建 Work |
| 桌面连接页的浏览器状态和目标资料 | +60–140 | 原设备连接页补资料，无新控制台/新 VNC 页 |
| 定向测试 | +350–800 | 三平台路径、原 profile、真实 CDP、断线与产物闭环；不把 Windows/Linux skip 当通过 |

浏览器专属生产增量约 **510–1,110 行**，包含最小页面上传、动作确认/撤权与浏览器 UI；主任务书若将 UI 全计在桌面项，这里去重为约 **450–970 行**。若同时做独立 profile 的启动管理，再预留约 100–220 行，属于另一项显式能力。通用设备文件桥仍要计入整个方案，不能把“截图已打印本机路径”当作零成本完成。功能增加后总行数大概率增加；主要节省是共享原动作与协议，避免两份同类实现和另一套浏览器生命周期，不应承诺总代码量必降。

复用用户已经运行的 Chrome，避免在云端新开 Chromium、Xvnc 和窗口管理器，服务器新增主要是设备控制流、模型所需页面内容及文件接收。页面本身 CPU/RAM 转移到本机，并不会消失；高频 AX/截图、下载和传输也有开销。交互 session 保持连接可少做握手和浏览器许可往返，按需取快照不做常驻整屏轮询。未采样真实任务前不填写节省 MB、CPU 百分比或延迟数字。

## 该候选路线的验证场景

1. Windows、macOS、Linux 各在隔离测试 profile 建立基线，再由操作者自愿用已有登录 profile 验收：原账号与原标签保持，原页面当前状态可读写；截图/页面请求证明确实操作所选本机浏览器。不能只用 fixture 或新建 profile 代替这一项。
2. Chrome 144+ 显式开启、允许、拒绝和撤销分别核对。原生 Python 连接若不兼容实际握手，记录为缺口后选择修正 CDP 传输或使用可选 OpenCLI；不宣称没有 MCP 已全部验证，也不绕过浏览器自身许可。
3. 用同一长终端 run 完成列页、AX、点击、输入、滚动、截图与真实文件上传；测试站收到字节与所选本机文件一致。既有服务器 CLI/CDP 行为保留。浏览器关闭、目标页销毁、profile 多候选、设备离线按真实状态返回。
4. 在输入接纳后丢云端连接，核原操作、stdout 游标和本机进程；重连不重发网页提交、不重复下载、不重置预算。不能将 `unknown` 改为成功，或自动关闭用户浏览器来收尾。
5. PNG 临时观察经真实文件流、字节准备和已有私有 `tool_handle` 进入模型；明确交付的截图/下载文件才冻结到原 `WorkspaceStore`，`send` 可交付真实附件。hash 与原文件版本一致，发布后改本机文件不改已交付版本；传输/私有媒体/发布确认丢失按原 ID 查证。到期且无真实引用的观察资料可回收，原 Work/已付请求需要的私有副本仍按原 owner 保留，不依赖本机原件重新下载。
6. 正式三平台安装包在没有开发 checkout、全局 Python 或全局 Node 的机器上能运行原生 CDP CLI；用户选择 OpenCLI 时才核其实际安装和扩展。关闭窗口/托盘/退出与设备连接持续策略遵从同一产品合同，不为浏览器特设第二常驻服务。
7. 长 CLI 已启动且有待派发动作时关闭完整访问，核后续 CDP 不再发出，原 action/终端结果仍可查询，用户 Chrome 与原人工标签保持。关闭开关不是杀用户浏览器的理由，重新开启也不重放旧缓冲动作。

本轮完成源码与官方资料核查、路线和理论估算；没有运行上述真实用户 profile 验收，没有实现、提交、推送、PR、合并或上线。
