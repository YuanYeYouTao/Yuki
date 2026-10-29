# 桌面端统一接入本机能力：源码调查

日期：2026-10-29。只做源码与文档调查，没有实施、提交、推送、安装、调用真实云端或付费模型。

本文保存调查基线的源码事实与首轮候选建议。后续施工按[复审后的统一任务书](../../Yuki-桌面端本机完整操作统一接入任务书-2026-10-29.md)及现行模块合同执行；以下建议不构成另一份施工合同。

基线：

- Desktop：`Institute-of-Agent-Life/Yuki-Desktop`，独立只读工作树 `D:/Code/My_Github_Repo/Yuki-Desktop-full-access-audit-20261029`，`9114d043ebcf46c14b694b279abf2c31b71be5a1`。
- Backend：独立文档工作树 `C:/Users/bymay/.codex/worktrees/local-full-access-taskbook-20261029/Yuki-QQbot`，`02ce928ee35a4e891082cd40edd96a79f348d963`。
- 已读后端 `AGENTS.md`、`docs/architecture/development-contract.md`、架构索引、本机连接器现行文档、桌面电脑现行文档，以及 Desktop `README.md`、`RELEASING.md`。Desktop 的跟踪文件与隐藏文件扫描未发现仓库内嵌套 `AGENTS.md`。

以下 Desktop 源码位置相对上述 Desktop 基线；Backend 位置注明仓库。引用是调查基线，不代表未来实现后的行号。

## 1. 结论

现有桌面端已具备正确的薄入口：桌面主进程管理同一个 Python Connector，Connector 使用当前 OS 用户的独立服务主动连接云端。应扩展这套连接器的设备能力，统一接入本机 Shell、原生 Agent 与浏览器。不要在 Electron 新增另一个云端 WebSocket、Shell 执行器、任务队列、文件接口或本机 Harness。

完整访问应是用户在本机针对当前 Yuki 一次明确开启，执行沿当前 OS 账号已有权限，工作目录仅是命令上下文，能够访问该账号本来可以访问的其他目录。不新增项目白名单、逐命令确认或服务器管理员资格。当前仅授权使用 Agent 的旧安装，升级程序本身不能替用户宣称已经开启了完整访问；由同一连接页完成一次扩展授权即可。

设备级 Shell 不能要求机器上先安装 Codex、Claude 或 ACP。Agent 的安装发现、原生会话和权限请求属于相应能力，不应成为整台电脑已连通的门槛。连接状态、服务状态、具体能力状态、原操作结果应继续分别显示。

这里开放的是 **Yuki 自身使用本机的通用执行能力**，不是替 Codex 设置 full-access。文件读写、目录查找、程序启动和管理优先直接使用同一 Shell；Codex/Claude 是复杂编程任务的可选委托，不充当简单命令的强制中转。原生 Agent 自己的 sandbox/审批配置只按其真实合同呈现，不能拿来证明 Yuki 已拥有整机执行能力。

## 2. 已有连接与执行路径

| 事实 | 源码证据 | 复用方式 |
| --- | --- | --- |
| Desktop 通过明确 IPC 调用管理 Connector | `src/preload/index.ts:6-26`；`src/main/app.ts:348-386` | 在现有 bridge 增加完整访问配置/状态管理，不新增通用 renderer 执行命令 IPC。 |
| 主进程只启动 Connector 管理 CLI | `src/main/agent-connector.ts:52-93` | 当前 `runConnector` 用绝对 Python 路径、参数数组、`shell:false` 和 stdin；这是管理通道，不是本机 Shell 产品功能。不要把它改成远程命令执行器。 |
| Connector 运行时随安装包分发 | `electron-builder.yml:8-12`；`build-tools/prepare-connector.mjs:14-36` | 新能力跟随同一个后端包构建，用户无需额外装 Python。 |
| 当前登录 Person 可以申请 connector 用途授权 | `src/main/connections.ts:1269-1272`；`src/main/app.ts:354-355` | 沿当前身份和原配对流程扩展能力，不再抄第二份普通用户配对码。 |
| 检查当前服务器、原设备与当前人物是否匹配 | `src/main/connections.ts:1227-1250` | 保留真实设备/人物/服务器 namespace，不把名称或路径当身份。 |
| 目录和回执走同一云端原生服务 | `src/main/connections.ts:1253-1266`；`src/main/app.ts:387-427` | 继续从同一服务查询设备能力与原操作。 |
| 桌面提交 Agent 要求沿普通消息 outbox | `src/main/app.ts:429-439`；`src/renderer/views/ConnectorTasks.tsx:120-167` | Agent 委托、聊天接纳和原生执行分别结算；不能把发出请求冒充本机命令成功。 |
| 已有回执、问题回答、取消和 detach UI | `src/renderer/views/ConnectorTasks.tsx:337-387` | 复用回执区域；Shell 的运行日志与取消也进入原操作结果。 |

当前 `src/shared/connector.ts:5` 的 driver 仅为 `codex | claude | acp`，`ConnectorConfiguration`、实例状态与 session API 都按 Agent 建模。它没有设备级 Shell 或浏览器配置，不能只改名称就宣称支持整机访问。

## 3. 现有产品行为与本轮缺口

### 3.1 开启设备不再依赖 Agent 安装

现有 `src/renderer/views/AgentConnector.tsx:98-101` 默认 Codex；首次表单 `:319-337` 配对后立即发现/登记 Agent，按钮 `:372` 写成“连接本机某 Agent”。`src/shared/connector.ts:5` 也只枚举 Agent driver。

建议把设置入口从“本机 Agent”改为“这台电脑”或同等清楚的产品名称，一次确认当前 Yuki、服务器、本人和设备；完整访问开启后，Shell 能力独立就绪。下方保留已安装 Agent 和浏览器能力，不让未装 Claude/Codex 阻挡设备上线。

不要把 Shell 伪装为一个 ACP Agent，再创建无意义会话来绕过当前接口。设备能力与真实 Agent 实例各认自身身份。是否扩展现有 `external_agent` 服务或共同本机协议，由后端总设计确定；桌面需要展示真实能力，不绑定某个供应商。

### 3.2 当前一次同意可以复用，但需要改变准确语义

`src/renderer/views/AgentConnector.tsx:351-370` 已有针对特定 Yuki 的一次勾选，随后沿原生权限执行。`:113` 与 `tests/e2e/agent-connector.spec.ts:522` 说明原 namespace 已授权后不会每次再加勾选门槛。

完整访问继续使用这一个本机入口。说明应直说：允许该 Yuki 在这台电脑上使用当前账号的文件、命令、已安装工具和可用浏览器；访问范围随 OS 账号权限。一次开启后不加入路径审批、逐操作弹窗或项目绑定。

仅在 Electron 偏好中保存一个布尔值不足以生效：真正执行者是独立 Connector。其配置应持有实际启用事实，Desktop 读取/修改它；不要复制成桌面、服务和云端三套可以互相冲突的完整访问状态。

旧 Agent-only 安装保留原 device、instance、会话、journal；开启完整访问扩展原设备能力，不要求重新配对、删除配置、迁移成另一台电脑或重放未决请求。

### 3.3 设备、服务和能力状态继续分别呈现

当前 `AgentConnector.tsx:390-409` 分别显示授权来源、背景服务、服务器接纳和协议版本；`:419-440` 又显示原生实例是否可用。应保留这套区分，增加 Shell/浏览器的真实能力结果。

现有状态刷新 `AgentConnector.tsx:136-150`、`ConnectorTasks.tsx:65-81` 通过服务器现有 hint 失效查询，不新增设备在线心跳数据库或全页面高频轮询。读状态不安装、不启动服务，不以版本号或诊断成功代替执行成功。

### 3.4 原操作区域目前会隐藏设备级操作

`ConnectorTasks.tsx:314-316` 只显示 `operation.instance_id` 在当前 Agent 实例目录中的操作。新增没有 Agent instance 的 Shell/浏览器操作后，若沿用此筛选会全部隐藏；原实例移除后，相应旧操作也不宜仅因今天目录变化而从原回执视图消失。

按已授权的原设备和原操作展示统一操作目录；Agent 会话筛选留在 Agent 功能内部。不要为了通过筛选创建虚假 shell instance，也不要从当前安装路径重建旧执行归属。

### 3.5 日志和取消不能依赖管理 CLI 的 stderr

`runConnector` 在 `src/main/agent-connector.ts:71-89` 收集管理 CLI stdout，并丢弃 stderr，避免把凭据或 traceback 直接送进 renderer。这不是已存在的实时本机命令日志接口。

Shell 操作至少需要从原操作读取：命令及 cwd、stdout/stderr、仍在运行、实际退出码/信号、取消接纳和真正进程终止事实。`src/main/app.ts:395-408` 当前把结果主要投影成 `text`，不足以表达这些结构；需随实际 API 类型扩展。现有分页事件、原 operation ID 与刷新提示可复用。

断线后继续查询原 ID；不知道是否执行过时保留 unknown，不因重开页面再执行命令。取消作用于该原操作实际持有的进程，不关闭整个 Codex daemon 或其他用户任务；“停止观察”和“取消执行”继续分开。

不必为了查看日志先新增 xterm/远程桌面。若任务书选择支持交互式 shell，则输入、等待、终端尺寸与进程生命周期也由同一 Connector 承担，不能靠固定命令超时模拟终端结束；PTY/ConPTY 的具体实现和代码量由后端执行器调查统一核定。

## 4. 已有安装、升级、存储与退出

### 4.1 继续沿用已有安装

- `src/main/agent-connector.ts:138-177` 只读发现原配置和 OS 服务的实际 Python，保留原目录与来源。
- `:284-319` 关联原安装时只选择它，不重新配对、不启动服务。
- `:253-272` 升级已有服务时保留运行/停止状态；原本停止的服务不会因升级自动开机。
- `src/main/connector-installation.ts:103-145` 校验随附运行时后复制至版本目录，原数据目录继续保留；选择只存本机安装元数据，凭据仍在 Connector 配置。
- `src/main/connector-installation.ts:31-45` 当前只记录一个所选安装。其他 Yuki/人物的独立安装应保持原目录，切换不覆盖它们的配对和未决操作。不需要本轮新建全局多设备协调器。

完整访问配置必须加入既有状态/配置读写和升级验证。只改 Desktop 文案而未更新实际随附 Python 包，会出现“显示已开启，服务仍只接 Agent”的产品假象。

### 4.2 独立服务的生命周期需要说明清楚

`AgentConnector.tsx:630-651` 已有安装、启动、停止服务按钮，并明确关闭 Desktop 后背景连接继续工作。后端 `packages/yuki-agent-connector/src/yuki_agent_connector/service.py:128-170` 的 Windows 任务使用 `InteractiveToken`、`LeastPrivilege`；macOS 使用 LaunchAgent，Linux 使用 systemd user service（`:172-203`）。

因此“打开桌面端就有机会操作本机”可以作为建立和管理入口，但不能宣称“关闭窗口自动停止所有本机能力”，除非任务明确改变独立服务合同。建议沿用已有背景服务，界面持续显示真实运行状态，并提供同一个停止入口；不用新增常驻进程。

Windows 注销和 OS 锁屏、macOS 登录会话退出、Linux 图形会话与 user service 环境应分开验收；用户服务不是 SYSTEM/root 服务。完整访问不主动提权，实际 OS 拒绝仍按原错误报告。

### 4.3 复用现有撤销和凭据归属

`src/renderer/views/DeviceSecurity.tsx:158-174` 已按原 request ID 撤销设备授权，`:210-238` 按 chat/connector 用途分别展示设备。完整访问设备可沿这一入口撤销，不再新建权限系统或要求本人同时是服务器管理员。

`src/main/credentials.ts:42-94` 保存的是 Desktop 设备凭据：系统 safeStorage 加密，Linux 无可用 keyring 时仅会话内保存。这与 Connector 自己的配置/journal 是不同 owner；不能把 Codex/Claude 登录文件复制进桌面数据库或云端，以“统一”为由迁走认证。

## 5. 连接入口与 SSH/Tailscale

当前 Desktop 不创建 SSH 隧道，也不安装 VPN。`README.md` 现行入口明确：已有 SSH/VPN/Tailscale 时由原工具先建立可达地址，桌面填同一个 Yuki URL。普通接入复用 HTTPS 443 `/client/v1/` 和 `/agents/v1/`，电脑主动连出，不需要本机开放入站端口。后端现行证据：`docs/operations/local-agent-connector.zh-CN.md:17-38`。

`src/main/api/client.ts:14-29` 保存完整 HTTP(S) URL，支持路径前缀；`connections.ts:1227-1229` 在确认 Connector 时要求与当前连接 URL 相同。SSH 本机端口、Tailscale 地址、公网域名不是可以自动互换的字符串：切换可达入口需沿现有原实例/人物/请求 namespace 查证，不覆盖旧配置就宣称操作已迁移。

本轮无需另写远程命令执行 SSH 系统。完整访问能力复用原 Connector WSS，现有隧道只解决可达性；若后续要桌面管理 SSH，应复用系统 ssh、原身份和主机校验，作为独立网络接入功能，不复制 Connector 或原生 Agent 的执行链。

客户端 SSE 自动恢复见 `src/main/connections.ts:1049-1120`；未决 outbox 通过 `:1065-1100` 查证原请求。它与 Python Connector 的 WSS 恢复是两条服务各自拥有的传输；不要把 Desktop SSE 在线误报成 Shell 已连通，也不要再增加第三条执行 WebSocket。

## 6. Electron 边界与浏览器

`src/main/windows.ts:47-52` 使用 contextIsolation、sandbox、无 Node integration；`src/main/app.ts:138-145` 只接受本窗口主 frame 的 IPC。这里识别本地调用来源，没有限制当前 OS 用户的文件范围。完整访问不需要删掉这些约束，也不需要把通用 Node shell API 暴露给渲染聊天正文的 renderer。

本期浏览器 CLI 加入桌面端配套 Connector runtime，由已有安装升级流程交付；用户不手工另装 CLI/Python/Node。CLI 在本人电脑连接已安装、实测支持的浏览器，OpenCLI/MCP 不作为本期依赖或默认后备。Electron renderer 的 Chromium 与用户日常浏览器 profile 不同，不能冒充后者的登录态。

现有 `Computer` 远程画面是服务器持久 Linux 环境的 VNC；后端现行 `docs/operations/desktop-computer.zh-CN.md` 已明确本机 Agent 和角色电脑分别管理。接本机浏览器不要求改造这套 VNC，不把本机屏幕伪装为角色沙盒，也不为了简单浏览器操作默认分发第二套 Chromium/Xvnc。

浏览器自身连接许可和实现实际需要的 OS 权限按事实验收；完整访问不绕过它们。本期不做全屏截屏或原生跨应用键鼠，不预设 Accessibility/屏幕录制权限为设备上线门槛，也不另加逐动作审批。

## 7. 跨平台与打包基线

已有正式目标见 `.github/workflows/desktop.yml:16-22`：macOS arm64、macOS x64、Windows x64、Linux x64、Linux arm64。没有 Windows arm64 CI 或安装目标，不能因 Electron 支持 ARM 就宣称本产品已验收 Win ARM。

- `electron-builder.yml:8-12` 把同平台 Connector 放在 extraResources；`:44-54` 为 macOS hardened runtime、最低 macOS 13、DMG/zip；`:55-61` 为每用户 NSIS；`:62-95` 为 Linux AppImage/deb/rpm。
- `.github/workflows/desktop.yml:32-58` 从 `contracts/source.json` 的精确提交取后端、构建实际独立 Connector；`:77-106` 做类型/合同/单测/E2E/打包及实际包内容检查。必须使用这条真实资源链。
- `RELEASING.md` 明确 unsigned CI 产物、签章、公证、真实安装和对外发布分别结算；新增 helper/扩展时更新现有签章清单和内容审查，不绕过校验或重建一套发行脚本。
- Backend `packages/yuki-agent-connector/pyproject.toml:3-6` 为 Connector 0.3.6、Python 3.12；`scripts/build_runtime.py:161-171` 输出实际版本、protocol、平台、架构和文件清单。仅 Python stdlib Shell 执行可随现有 runtime 发行；PTY/ConPTY、原生屏幕输入或额外浏览器程序会带来额外平台依赖，估算需单列。
- 本调查 Desktop `contracts/source.json` 仍固定 Backend `4d2a70a24723b390034754285687897af7036723`、server 4.0.0、Client API 7，OpenAPI SHA-256 `58462caed2f2fbe63d762d3627645476ab03baa26128982d950d73ba7701280a`。它不是本次 Backend `02ce928e`；实现后须同步真实快照/生成类型、pin、协议与随附运行时，不仅改版本文字。

## 8. 验证复用与新增验收

现有测试的用途：

| 已有测试 | 当前覆盖 |
| --- | --- |
| `tests/unit/agent-connector.test.ts:84`、`:169`、`:196`、`:727`、`:1389` | 参数/stdin、不经 shell、原配对、首次安装、保留原数据升级、Claude 发现等管理链。 |
| `tests/e2e/agent-connector.spec.ts:39`、`:131`、`:197`、`:275`、`:369`、`:479`、`:533` | UI 连接、关联原安装、无项目会话、ACP 参数、停止状态升级、状态区分、迟到问题和终态。 |
| `tests/live/native-agent-discovery.spec.ts:18` | 明确 opt-in 后经过实际 Electron main/preload 发现所选 Codex；不配对、不开 daemon、不执行模型任务。 |
| `tests/e2e/connection-recovery.spec.ts:21`、`:139`、`:217` | 客户端真实断流、原消息可见性、两设备原问题回执。 |
| `tests/release/connector-signing.test.mjs:32`、`:98` | Mac/Windows helper 签后清单接线；不等于正式证书签章。 |

本轮只做源码调查，没有执行这些测试，不能把文件存在写成测试通过。

新增验收应覆盖：

1. 未安装 Codex/Claude 的干净账号可单独开启设备完整访问并使用 Shell；不选项目，不加目录白名单。命令在两个相互独立的临时目录读写，含中文、空格和 shell 元字符，结果由实际文件和原回执核对。
2. 现有 Agent-only 设备升级后保留原 device、instance、会话、journal 和停止状态；本机一次开启完整访问后，重开页面/服务不重复确认，也不重发旧命令。
3. 同机两个 Yuki/人物的独立配置不互相覆盖；切换服务器地址不会搬运原未决操作或错误继承另一个 namespace 的完整访问。
4. 长运行命令的 stdout/stderr 可连续读取，分页后保留旧片段；断线、ACK 丢失、桌面崩溃和重连后查同一 operation，不启动第二个进程。进程退出事实与连接断开分开显示。
5. 取消只终止原操作的实际进程/子进程；取消接纳与实际退出分别报告。关闭桌面、停止 Connector、撤销设备、退出 OS 会话分别验证，不能一概称“取消成功”。
6. Agent 会话和 Shell 操作在同一设备页可查，设备级回执不被当前 Agent 实例筛掉。用户没有安装的浏览器/Agent显示实际缺失原因，不影响其他能力。
7. Desktop 聊天正文/外部 iframe 不取得通用执行 IPC；完整访问实际由同一 Connector 执行，不走 renderer 的 Node API。
8. Windows x64：每用户 NSIS 全新安装/原位升级、实际用户计划任务、PowerShell/命令参数和 Unicode、ConPTY（若纳入）、当前账号的文件访问、普通拒绝访问、登录/注销及锁屏分别验证。
9. macOS arm64/x64：DMG/zip、随附 Python 架构、LaunchAgent 的真实 PATH/代理/CLI profile、shell 编码与取消、系统真实授权、hardened runtime 和签章后 helper 清单分别验证；Rosetta 不替代 Intel 实机。
10. Linux x64/arm64：AppImage/deb/rpm、systemd user 环境和当前 shell、X11/Wayland 与用户会话、keyring 有/无、实际架构与 helper 可执行性分别验证。图形权限和 Shell 可用性分别呈现。

## 9. 桌面部分理论代码量

当前直接相关产品文件可见行数为：`agent-connector.ts` 509、`connector-installation.ts` 146、`shared/connector.ts` 148、`AgentConnector.tsx` 783、`ConnectorTasks.tsx` 393，共 1,979 行；不代表都需要重写。现有 Connector 单测 1,520 行、该页 E2E 570 行。

沿原管理和回执链增加设备级完整访问、Shell 配置与真实状态，桌面部分粗估新增 **250–500 行产品代码、删除 80–200 行重复/Agent 前置 UI，净增约 50–420 行**；相关新增/调整测试约 **300–650 行**。此估算不含后端执行器、原生浏览器驱动、PTY/ConPTY、完整远程桌面或新交互终端模拟器，不含生成 OpenAPI/类型文件。它来自已读文件与需要变化的界面/类型表面，尚无实现 diff 或原型测量。

如果只把四类能力并列拼接成四套配对、服务和日志 UI，代码会明显更多，且状态容易互相矛盾。统一管理页可以减少重复逻辑，但新增真实 Shell/浏览器功能仍通常净增代码；不能承诺比只支持 Agent 的当前项目总行数更少。

维护性的主要收益是一个执行 owner、一个设备 namespace、一套运行时分发、一个后台服务和原操作回执链；不是压缩文件行数。避免提前加原生 UI 框架、重复文件 API、独立 Shell 守护进程和通用权限协调器，可以把新增量留在真实能力适配。
