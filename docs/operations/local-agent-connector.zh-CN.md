# 本机 Agent 连接器

Yuki 继续运行在服务器，Codex、Claude Code 或 ACP Agent 留在用户电脑。电脑主动连接原 Yuki HTTP 服务上的 `/agents/v1/connect`，不用开放电脑入站端口，不上传原生登录文件，也不用在服务器安装 Codex。

连接器是同仓独立包 [yuki-agent-connector](../../packages/yuki-agent-connector/pyproject.toml)，当前源码版本 `0.3.4`。手动安装需要 Python 3.12，配套桌面安装包随附独立运行时。它不安装 Bot，不运行另一个 Main／Work，不替原生 Agent 重建历史，也不直接发聊天消息。进度与问题进入原 Work，汇报仍通过 Main 的 `send`。

## 从桌面连接

配套 Yuki Desktop 的「编程 Agent」页提供首次接入：确认当前 Yuki 与服务器地址，明确允许这位 Yuki 使用本机 Agent，再点击「连接本机 Codex」。桌面准备随附运行时，以当前已登录的 Person 申请 `purpose=connector` 授权并完成配对，随后发现已有 Codex；多个候选时才需要选择。项目目录可以选择，也可以沿已有原生会话继续。用户不需要安装 wheel、Python、修改 PATH 或另外抄配对码。

桌面源码、五平台 CI 安装产物和正式 Release 是不同状态；实际可下载版本以桌面仓库对应产物为准。原生 Codex 仍需用户自行安装、登录并处理它自己的权限。服务已启动、服务器已连接和原生握手成功分别展示，不能代替实际模型任务的执行结果。现有独立安装及手动入口继续保留。

服务器部署者需一次性提供同一域名的 HTTPS／WSS 入口：`/client/v1/` 和 `/agents/v1/` 转发给同一 Yuki 服务，后者允许 WebSocket Upgrade。已有 HTTPS 代理直接复用域名与 443，不另开 Agent 端口；尚无入口时按[桌面本地部署与公网接入](desktop-local-deployment.zh-CN.md)配置实际域名、证书和代理。电脑主动连出，不开放入站端口。下文 SSH 隧道是已有 SSH 用户的高级方式，不是普通桌面接入的默认前置条件。

服务器 URL 与 device ID 参与原 journal namespace。已有 HTTP／隧道安装不能仅覆盖成新 HTTPS 地址就声称原任务无损迁移；先核原未决操作与回执，再明确处理原安装。

## 安装与显式配对

安装本次构建的独立 wheel；正式包是否已发布应查发行回执，不把源码版本当成公开发行：

```sh
python -m pip install /path/to/yuki_agent_connector-0.3.4-py3-none-any.whl
yuki-agent-connector --version
```

公网直连使用 HTTPS／WSS，反向代理把同一服务的 `/agents/v1/` 转发给 Yuki，并允许 WebSocket Upgrade。凭据不用 URL 查询参数传递。

已有 SSH 的远程部署也可以转发服务器的 loopback 服务，不必为连接器另开公网端口。在电脑保持这条隧道：

```sh
ssh -N -L 127.0.0.1:<本机空闲端口>:127.0.0.1:<服务器服务端口> <SSH主机>
```

随后把配对地址设为 `http://127.0.0.1:<本机空闲端口>`。配对 HTTP 和后续 WS 都只连接电脑 loopback，远程一段由 SSH 加密。隧道需要在连接器运行期间保持；使用已有 SSH 身份和主机校验。代理环境中把 `127.0.0.1,localhost,::1` 加入 `NO_PROXY`。

在 Yuki 管理端为真实内部 Person 明确签发电脑控制配对码：

```sh
yuki admin client pair --person <内部PersonID> --purpose connector
```

已认证的个人入口也可通过 `POST /client/v1/link-codes` 明确提交 `purpose="connector"`。普通聊天 token 或聊天配对码不能直接用于电脑连接；平台超管和 operator 也不自动获得别人的电脑控制权。

在电脑上执行：

```sh
yuki-agent-connector pair --url https://yuki.example.com --label "家里的电脑"
```

随后在隐藏输入中填一次性码。CLI 调用 `POST /agents/v1/pair`，保存独立电脑凭据并形成对应 WSS 或 loopback WS 地址；不打印 token。自动化可用 `--code-stdin` 从标准输入交付一次性码，或显式使用 `YUKI_CONNECTOR_CODE`，用后由调用方清理该变量。一次性码只能成功兑换一次。

默认配置和 journal 位于当前 OS 用户的数据目录，配置文件使用用户私有权限。所有命令支持在子命令之前指定 `--data-dir` 和 `--config`。一个安装目录对应一个服务器／电脑 namespace；另一服务器或独立设备使用另一目录，不能覆盖旧配置来搬运未决操作。

## 选择原生实例

实例指定电脑上真实安装的程序、profile 与工作目录。程序路径、环境和 owner endpoint 留在电脑配置中，不作为模型工具参数传输。每个实例使用自己的稳定本地 ID，同一安装可登记多个 profile。

```sh
yuki-agent-connector instance add --label "Codex 日常账号" --driver codex --command /path/to/codex --cwd /path/to/project
yuki-agent-connector instance list
yuki-agent-connector doctor
```

Windows 的 `--command` 使用实际原生 `.exe` 路径，工作目录可以是 `D:\Code\My_Github_Repo\Yuki-QQbot`。默认 Codex 通过官方 `app-server proxy` 连接当前 OS 用户／`CODEX_HOME` 对应的原 daemon；不自动更新或重启 daemon。明确选择其他 owner 时可配置 `--socket` 或 `--endpoint`，明确选择 profile 时使用 `--env CODEX_HOME=...`。

Claude 使用已安装的真实 CLI：

```sh
yuki-agent-connector instance add --label "Claude" --driver claude --command /path/to/claude --cwd /path/to/project
```

连接器通过官方 Claude Agent SDK 指定这个 CLI，使用原生 preset 和用户／项目／本地设置。Windows `.cmd`／`.bat` 不能冒充原生 CLI。SDK 管理的新执行或明确 resume 与任意接管一个正在运行的独立 Claude 终端不同，工具按真实能力返回结果。

通用 ACP 使用明确安装的程序及参数：

```sh
yuki-agent-connector instance add --label "ACP Agent" --driver acp --command /path/to/acp-agent --cwd /path/to/project --agent-arg=--stdio
```

文件与终端回调在这台电脑执行。各 Agent 的 list／load／resume／steer 等能力以实际协商为准，不支持时不暗中改成取消重启或新建会话。

`doctor` 查询实际程序与原生目录，不发模型任务；ACP 检查可启动所选协议程序完成握手。它不证明模型额度、真实代码修改、审批或端到端委托已经验收。配置变更后重启连接器应用新实例清单。

Desktop 可以先登记原生安装并选择已有会话，不要求先选新项目。`instance configure` 的 JSON 可省略 `cwd`：已有实例沿用原目录，新实例使用 `Instance` 原本的用户主目录默认值；只有显式选择项目才修改目录。登记和读取目录都不创建原生 thread。

由发现候选登记的 Codex 按原安装来源跟随正常程序升级。0.3.4 的 `discover --instance ID --json` 只读解析该原实例当前的程序路径，返回 `{"instance_id":"原ID","command":"当前路径"}`，不启动原生程序、不改配置或 journal。编辑同一实例的名称或项目时也先解析原来源，再保存；原实例、环境、owner 与回执保留。用户明确换成手工绝对路径时按该路径配置；原来源消失时明确报错，不自动选择另一个 Codex。

`status --json` 的 `protocol` 是本机 Connector 协议，`GET /agents/v1/devices` 的 `connector_protocol` 是服务器接受的协议。版本不同时更新较旧的一端；服务端的 `unsupported_connector_protocol` 拒绝与撤权分别诊断，不改写原操作、原会话或登录。原生实例的 `status`／`error_type` 与 OS 服务是否运行、服务器设备是否在线是不同事实，不能互相代替。

真实连接和断开会发布现有的可丢弃刷新提示，Desktop 收到后重新读取目录；这个提示不是数据库回执，不增加 revision 或持久事件，也不通过定时轮询维持状态。

## 前台与用户服务

```sh
yuki-agent-connector run
```

前台连通后可安装独立用户服务：

```sh
yuki-agent-connector service install
yuki-agent-connector service start
yuki-agent-connector service status
yuki-agent-connector logs --lines 60
```

Windows 使用当前用户的 Task Scheduler，登录后运行，不使用 SYSTEM、提权或保存 OS 密码。Linux 使用 `systemd --user`，macOS 使用用户 LaunchAgent。它们使用同一 CLI 和配置，没有第二套桌面驱动；退出桌面窗口不会结束独立用户服务。Windows 注销和无用户会话的行为不能冒充常驻系统服务。

```sh
yuki-agent-connector service stop
yuki-agent-connector service uninstall
```

卸载服务不删除配置、journal、原生登录或用户会话。断网只断开传输，原生执行继续；停止连接器进程、OS 重启、原生 owner 退出是不同生命周期。Codex 停止客户端／proxy 不等于停止用户 daemon。Claude SDK／ACP 自己启动的进程依其真实生命周期处理，不套用 Codex 的存活结论。

## 代理与凭据

服务器 WSS 使用标准代理环境和系统选择，原生程序继承当前 OS 用户的环境。可在前台启动前设置 `HTTPS_PROXY`／`ALL_PROXY`、`NO_PROXY` 和实际 CA 变量；例如将 HTTPS 代理指向本机 Mihomo 的真实端口。端口取用户现有配置，程序不写死代理地址。

原生 Codex owner 的本地连接不走公网代理。服务器地址需直连时，把实际主机加入 `NO_PROXY`。用户服务与临时 shell 的环境不同：Linux／macOS 安装器保存其明确支持的原用户环境，Windows 服务按当前用户会话环境启动；修改代理后核服务的真实环境并重启，不能凭当前终端能访问就声称服务已生效。

不要把 Codex／Claude API key、登录文件或 connector token 发到聊天里。实例目录只展示名称、类型与能力；本机原生认证继续留在原程序的用户存储中。

## 状态、断线与原执行

工具目录返回内部 device／instance／session／execution ID；本地名称、路径和 native thread ID 不用于重建人物或执行归属。一个 Person 可从 QQ、TG、App 或 ACP A 查询本人获准的原操作，各入口的公共聊天历史仍分开。

`list/get` 只查询；`watch` 仅指定 session 时持续监督该会话，指定 execution 时只监督该轮；`prompt/queue` 委托新输入；`steer` 纠偏指定活跃执行；`respond` 回答原问题；`cancel` 定向取消；`detach` 结束监督。detach、Work 完成、聊天重置和断网都不能顺手关闭用户整个 daemon 或其他会话。

确认丢失后按原 operation／run 查询。unknown 不代表未执行，不换 ID 重发。重要事件先进入服务器原回执和 Work 私有输入，再确认设备事件；同一事件重传不重复产生输入。高频 token 不作为持久进度洪流，原生完整上下文不灌入 Yuki 的公共历史。

Work 恢复缺失的工具结果时，按原调用派生的请求键和原 Work、action、actor 读取本地
operation 回执，包括没有 run 的 steer／respond／cancel／detach。它与公开查询共用原结果
投影，但不联网、不重新派发，也不要求设备在线或原人物仍启用。控制已接纳不等于原生任务
完成；未知保留原操作 ID，已知失败仍是失败。公开查询和新效果继续核当前权限，隐私清退
已删除的回执不从当前设备或其他操作重建。

设备撤销后，已有长连接也不能继续接纳新效果；已发生的原回执仍按事实保留。配对、在线、目录、原生接纳、执行终态、Work 完成和聊天送达是分别核对的事实。

## 验证范围

[`test_external_agent_bridge.py`](../../tests/integration/test_external_agent_bridge.py) 使用真实 HTTPS／WSS、临时 SQLite、真实 Work 控制和可控本机协议对端，覆盖配对、内部 ID、进度／问题／终态可靠入账、精准目标、撤销、断线与原操作恢复；它不调用真实模型或发送 QQ 消息。

独立包分别验证 Codex 原生 RPC、Claude SDK 解析、通用 ACP stdio 回调及 journal。真实 Codex daemon 只读验证、独立 wheel 安装、Windows 用户服务、Linux／macOS 实机、Claude 真实账户与完整服务器到本机付费执行须在交付记录中逐项报告；不把协议替身、版本号或健康检查写成这些验收已经完成。

### 显式真实 Codex 验收

[`test_local_codex_bridge_live.py`](../../tests/integration/test_local_codex_bridge_live.py) 默认跳过。需要真实付费模型验收时，在项目测试环境另装独立连接器，并显式选择用户已经运行的 Codex：

```powershell
python -m pip install -e packages/yuki-agent-connector
$env:YUKI_REAL_CODEX_TEST = "1"
$env:YUKI_CODEX_BINARY = "/path/to/native/codex.exe"
python -m pytest tests/integration/test_local_codex_bridge_live.py -q -s
```

也可显式指定 `YUKI_CODEX_SOCKET`／`YUKI_CODEX_ENDPOINT` 和结果文件 `YUKI_REAL_CODEX_REPORT`。不设置原 owner 时，由官方 proxy 解析当前 profile；测试不会启动或重启 daemon。

需要核验原生审批回传时可另设 `$env:YUKI_REAL_CODEX_APPROVE_ISOLATED = "1"`。这只授权测试构造的两条完整 PowerShell 命令：在该唯一临时目录写入带本轮随机标记的 `probe.txt`，再读回同一文件。测试核对原生请求的 cwd、单条 commandActions 完整命令和可用的普通 `accept`，使用服务器内部问题 ID 经公开 `respond` 回答；不选 execpolicy amendment，不改原用户策略，其他命令、目录、审批和用户问题一律保留失败。

该测试运行真正的 Connector／journal／CodexDriver，经隔离 HTTPS／WSS 和临时业务库，在唯一临时目录中新建自己的原生任务，写入并读回探针文件，核对工具进度、终态与原 Work 私有回执。它只清退自己的原生会话，不触碰用户已有线程；没有上述显式授权时，原生审批仍会明确报告并停止测试。使用真实模型会产生其正常调用费用。测试成功仍须分别记录版本、文件证据、原回执与原生清退结果，不代表生产服务器或真实 QQ 投递已经上线。

2026-10-23 在 Windows 上以 `codex-cli 0.163.0` 和连接器 `0.1.0` 完成一次真实模型全链路验收：原生审批经内部问题 ID 回答，隔离目录写入和读回各有一条成功命令回执，最终标记与文件内容一致，原执行终态进入原 Work 的 continuation；测试通过，耗时 34.98 秒。9 条重要事件中含 3 条工具回执，其中首次沙盒执行失败、随后精确授权后的写入与读回成功，失败事实仍保留。该次缓存字段未提供，记录为 unknown。测试只归档自己的新原生会话，未停止用户 daemon；这份记录不涵盖生产上线、真实 QQ 投递、Claude 付费执行或其他 OS 的实机服务验收。


## 协议 3：原回执、持续监督与断流恢复

Bot 与 Connector 0.3.0 配套升级，WSS hello 使用 `protocol_version=3`。原 device、instance、
session、execution、operation ID 和 journal 保留。旧 watch 在升级时明确保留单轮范围；新 watch
只给 session 时观察后续各轮，单轮结束只是一条进度。detach 或原 Work 结束时退出监督，不关闭
用户的原生会话。指定 execution 的 watch 不追踪其他轮次。

本机 journal 保存原命令、原响应与最新回执。事件和响应沿同一个 SQLite sequence 排序，
事件附带完整 `operation_state`，查询返回同一份投影。服务器按 `state_sequence` 接纳新事实，
不再靠事件有没有再次携带 admitted 来猜状态。断流变成 unknown，已确认接纳和原 ID 保留；
迟到进度不会复活完成或 detached 的观察。原命令是否成功仍不明确时，按原 client ID 查证，
不重发 prompt 或 queue。

实例分别启动，连接状态通过原 socket 的 catalog 更新。Codex 的正常 EOF、异常断开和 proxy
退出进入同一条恢复链，重连所选原 owner 后恢复 journal 中的观察。原生问题属于产生它的
RPC 连接；重连后即使请求数字重复，也不能用旧问题的回答批准新请求。Claude SDK 和 ACP
失去自己持有的执行流时记录 unknown，不声称可以接管已经消失的旧进程。

设备离线和原生实例离线分别呈现；设备离线时，未结束操作的查询返回 unknown，并附最后
确认的原回执。连接和断流事实进入原 Work 输入，由 Main 决定是否通过 send 汇报。任务没有
独立的 reporting 模式；日常聊天的 engage=quiet 和 SELF 安静工作的规则仍保留。

重要事件按 sequence 逐条从 journal 读取并流水发送，流量由 WebSocket 写入背压控制。
ACK 只在服务器提交后删除本机原件，不再阻塞下一条发送。断网重传未 ACK 原事件，不重派
原生操作；读端继续处理 RPC。事件发送、目录发送或读取任一退出，都结束该连接，避免看似
在线却不再上传进度。旧协议事件在服务端已提交但 ACK 丢失时，升级重传仍认原 event_id 和
原事实，不重复生成 Work 输入。

原生完整 diff 不作为持久进度上传；保留文件路径、命令结果、计划、问题、子 Agent 和工具
调用里程碑。需要细节时显式查询原生历史。以上为实现合同，实际测试与上线回执见本次交付记录。
