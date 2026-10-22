# 本机 Agent 连接器

Yuki 继续运行在服务器，Codex、Claude Code 或 ACP Agent 留在用户电脑。电脑主动连接原 Yuki HTTP 服务上的 `/agents/v1/connect`，不用开放电脑入站端口，不上传原生登录文件，也不用在服务器安装 Codex。

连接器是同仓独立包 [yuki-agent-connector](../../packages/yuki-agent-connector/pyproject.toml)，当前版本 `0.1.0`，要求 Python 3.12。它不安装 Bot，不运行另一个 Main／Work，不替原生 Agent 重建历史，也不直接发聊天消息。进度与问题进入原 Work，汇报仍通过 Main 的 `send`。

## 安装与显式配对

安装本次构建的独立 wheel；正式包是否已发布应查发行回执，不把源码版本当成公开发行：

```sh
python -m pip install /path/to/yuki_agent_connector-0.1.0-py3-none-any.whl
yuki-agent-connector --version
```

服务端使用已有 HTTPS 入口，反向代理须把同一服务的 `/agents/v1/` 转发给 Yuki，并允许 WebSocket Upgrade。凭据不用 URL 查询参数传递。只有本机隔离开发允许 loopback HTTP／WS；远程使用 HTTPS／WSS。

在 Yuki 管理端为真实内部 Person 明确签发电脑控制配对码：

```sh
yuki admin client pair --person <内部PersonID> --purpose connector
```

已认证的个人入口也可通过 `POST /client/v1/link-codes` 明确提交 `purpose="connector"`。普通聊天 token 或聊天配对码不能直接用于电脑连接；平台超管和 operator 也不自动获得别人的电脑控制权。

在电脑上执行：

```sh
yuki-agent-connector pair --url https://yuki.example.com --label "家里的电脑"
```

随后在隐藏输入中填一次性码。CLI 调用 `POST /agents/v1/pair`，保存独立电脑凭据并形成 WSS 地址；不打印 token。自动化可显式使用 `YUKI_CONNECTOR_CODE`，用后由调用方清理该变量。一次性码只能成功兑换一次。

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

`list/get` 只查询；`watch` 监督已有执行；`prompt/queue` 委托新输入；`steer` 纠偏指定活跃执行；`respond` 回答原问题；`cancel` 定向取消；`detach` 结束监督。detach、Work 完成、聊天重置和断网都不能顺手关闭用户整个 daemon 或其他会话。

确认丢失后按原 operation／run 查询。unknown 不代表未执行，不换 ID 重发。重要事件先进入服务器原回执和 Work 私有输入，再确认设备事件；同一事件重传不重复产生输入。高频 token 不作为持久进度洪流，原生完整上下文不灌入 Yuki 的公共历史。

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
