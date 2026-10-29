# 本机 Agent、反向连接与通用执行复用调查

范围更新：用户在实施中决定本机 Shell 直接运行 OpenCLI 等外部程序，只补统一文件管线。本文件下文保留当时的调查与候选；自研/内置浏览器 CLI、CDP 抽取、专属回执与打包已撤出本期，以现任务书第 7 节为准。
日期：2026-10-29。此文是本机电脑完整操作任务书的证据笔记，只读调查与文档，不代表实施或线上验收。

本文保存调查基线的源码事实与候选接线。施工目标、依赖和验收由[复审后的统一任务书](../../Yuki-桌面端本机完整操作统一接入任务书-2026-10-29.md)规定，以下存储/协议建议须按真实调用验证，不另成一套任务。

## 1. 本次目标和结论

用户要的是：云端 Yuki 经桌面连接后，能够使用本人电脑的 Shell、文件、已装程序和浏览器，类似 Coding Agent 使用本机电脑的方式。Codex、Claude 是可选的原生 Agent 委托能力，不是这些通用操作的前置条件。本次不把“完整操作电脑”偷换为给 Codex 配置 full-access flags，不重写 Coding Agent，不建立项目授权框架。

当前已有可用的反向连接骨架：电脑主动 WSS 连服务器，同一独立 Connector 包管理 Codex／Claude／ACP，桌面调用这个包的 CLI。原操作 journal、断网事件重传、提交后 ACK、原 Work 输入和执行回执也已经存在。缺少的是独立于原生 Agent 的设备级 Shell／文件／浏览器执行；不能把增加这项能力描述为“只加一个 action”。

可复用传输和回执原语；不能把通用 Shell 塞进原生 Agent 的实例／会话／执行模型。保留 `external_agent` 的原生委托职责，扩展现有终端的显式设备目标并按原 run 读取/控制；查询和监督差错按主任务书 T13/T14 修复，不因“保留原生职责”冻结错误实现。本机浏览器 CLI 随同一 Connector runtime 内置发行，不将其变成用户另装程序或服务器浏览器建设。

## 2. 基线与调查边界

| 对象 | 本次固定基线 |
| --- | --- |
| 后端／Connector | `02ce928ee35a4e891082cd40edd96a79f348d963`，调查树 `C:/Users/bymay/.codex/worktrees/local-full-access-taskbook-20261029/Yuki-QQbot` |
| 桌面 | `9114d043ebcf46c14b694b279abf2c31b71be5a1`，调查树 `D:/Code/My_Github_Repo/Yuki-Desktop-full-access-audit-20261029` |
| Connector | `0.3.6`；WSS 协议 `3`；Python `>=3.12,<3.13` |
| 锁定原生依赖 | `claude-agent-sdk==0.2.172`、`agent-client-protocol==0.12.1`，见 `packages/yuki-agent-connector/pyproject.toml:3-18` |
| 桌面 API 来源 | `contracts/source.json:3-9` pin `4d2a70a24723b390034754285687897af7036723`、Client API revision `7`；此 pin 不等于本次调查后端 HEAD |

已读 `AGENTS.md`、`docs/architecture/development-contract.md`、架构索引、本机 Connector 操作文档、Connector README 和桌面电脑现行说明。本文代码定位的后端路径均相对上述后端根目录；标明 Desktop 的路径相对桌面根目录。

本次没有运行模型任务、连接用户 daemon、修改用户配置、启动或停止服务，也没有做线上操作。只读运行本机 Codex `0.165.0` 的协议 schema 生成器，输出到调查树忽略目录 `.cache/local-device-codex-schema-0165/`；这证明本机版本的字段，不证明其他版本、当前账号额度或真实任务可执行。

## 3. 已有实现：连接只建一次，原生能力已有三种适配

| 能力 | 真实实现与证据 | 不能据此声称的能力 |
| --- | --- | --- |
| 电脑主动连出 | `connector.py:296-303` 用设备凭据建立 WSS；服务端 `external_agents/http.py:161-171` 认证后接纳 `/agents/v1/connect` | 这不是服务器直接拨进用户电脑，也不是 SSH 的另一套实现 |
| 同一服务的消息双向流动 | `connector.py:269-294` 并行 reader／events／catalog；`transport.py:29-62` 用原 command ID 配对响应，断线使等待者得到真实离线事实 | 不能只凭 socket 在线就说本机 Agent、Shell 或浏览器已就绪 |
| 桌面是管理薄入口 | Desktop `src/main/agent-connector.ts:53-95,109-119` 启动所选 Connector CLI，固定原 data directory；`:320-374` 沿现有配对入口 | Electron 中没有另一套 Codex RPC 或 Claude Agent loop；扩展时不应再写一套 |
| 原生实例目录 | `connector.py:109-128` 目录只公开实例 ID、driver、能力和就绪事实；`external_agents/repository.py:103-139` 保存实际目录变化 | 目录里暂时没有 Agent，不应阻止新的通用 Shell 能力 |
| Codex | `drivers/codex.py:35-96` 官方 `app-server proxy` 连所选既有 owner；`:817-849` 官方 thread／turn RPC | 不会替用户安装、登录或重启 daemon；目前不是通用 Shell 通道 |
| Claude | `drivers/claude.py:178-217` 官方 SDK 指向明确 CLI，保留原生 preset 与 user／project／local 设置 | 任意独立 Claude TUI 不是可随意接管的 SDK 会话 |
| ACP | `drivers/acp.py:92-112` 真实 stdio 握手与能力协商；`:541-588` 本机文件与终端回调 | ACP Agent 的文件／终端回调不是 Yuki 自身的设备级终端；不应靠伪造 ACP Agent 暴露 Shell |

这里的 SSH／Tailscale 只解决电脑到 Yuki 服务的网络可达性，不能替代应用层的命令、原执行身份、结果和文件交付合同。HTTPS／WSS 是现有普通入口；SSH 隧道是现有高级连接方式，见 `docs/operations/local-agent-connector.zh-CN.md` 的“从桌面连接”“安装与显式配对”。本次没有为 Tailscale 声称已实现专门集成。

## 4. 工作目录和真实授权是两件事

`Instance.cwd` 已默认用户主目录，见 `config.py:63-77`。Desktop 新实例采用 `input.cwd`／原目录／`homedir()`，见 `src/main/agent-connector.ts:429-445`；它不是必须先选择仓库才能建立电脑连接的设计。

Codex proxy 自用户主目录启动；相对 `CODEX_HOME` 和 socket 只按原配置目录解析为本次子进程的绝对路径，原配置不改，见 `drivers/codex.py:50-75`。新原生 thread 才使用任务 cwd，见 `:832-839`。Claude resume 则读取原生会话的原 cwd，见 `drivers/claude.py:181-189`；ACP 恢复也从原目录读取原 cwd，见 `drivers/acp.py:404-439`。这些都是正确的工作上下文语义，不是文件白名单。

真实设备控制仍归原 Person：

- `external_agents/service.py:339-343` 从可信调用来源取得 `actor_person_id`；缺人物来源即不进入本人设备调用。
- `external_agents/repository.py:65-75` 核设备 Person 与原 actor 是否同人；平台超管身份不是替代条件。
- `identity/credentials.py:66-73` 核设备凭据 purpose、撤销和 Person 有效性；`external_agents/identity.py:16-27` 使用独立 connector 配对入口。
- `external_agents/service.py:348-359` 先恢复原操作回执，再判断今日可变原生状态；`:360-375,504-550` 新效果沿当前 Work 来源和真实设备资格执行。

通用本机执行沿真实 Person 来源与适用的 Work 委托，不新增项目、目录或逐条命令授权表。普通终端、看图与发送不强制接纳 Work；原生 Agent 的接纳要求继续由其现行合同决定。SELF 没有本人设备来源时，保真实边界，不借最近真人或超管身份，也不另建人物框架。

OS 账号是实际执行环境：Windows 用户任务使用 `InteractiveToken`、`LeastPrivilege`，Linux 是 `systemd --user`，macOS 是用户 LaunchAgent，见 `service.py:121-203`。所谓完整操作指当前 OS 账号真实能够做的事，不自动提升 UAC／root，也不能用“完整访问”字样冒充绕过平台限制。

当前关闭桌面窗口不会停掉 Connector 用户服务，见 `packages/yuki-agent-connector/README.md:87-97`。产品需要明确“窗口关闭”“连接断开”“本机服务退出”三个不同事实，不能把开着窗口作为线上仍在执行的唯一判断。

## 5. 原操作、断线、取消和恢复：哪些必须原样保留

| 合同 | 当前证据 | 通用执行应保留的行为 |
| --- | --- | --- |
| 先记录再产生外部效果 | `connector.py:131-169` 在 driver 执行前 `journal.begin`；`journal.py:146-164` 提交原命令 | Shell 也先记原命令意图；重连后不重提 |
| 同 ID 不重复派发 | `journal.py:149-153` 原 ID 和参数一致则返原投影，参数冲突拒绝复用；服务端 `service.py:348-359` 同样先查原操作 | 原 request／run ID 不因 UI 重试、断线或 server 重启而重建 |
| 未确认保 unknown | `connector.py:183-224` 已派发后异常不伪称未执行；`service.py:221-251` 原操作查不到仍不授予重派资格 | 不把 `not_found`、超时、掉线转成自动重跑 |
| 原事实有序投影 | `journal.py:27-38,185-209` 已接纳与终态保留，响应和事件共用 sequence | 不让迟到 running／progress 复活已结束终端 |
| 事件先落盘再传 | `connector.py:70-78`、`journal.py:281-304`；`:243-257` 按序、按 WS 背压发送 | 输出片段可以按需要读取；真实终态及外部效果回执必须保留 |
| 服务端 commit 后 ACK | `external_agents/http.py:175-189`；`journal.py:315-317` ACK 只删本机 outbox 原件 | ACK 丢失重传原事件，只确认一次，不再次执行命令 |
| 网络断开不等于进程取消 | `connector.py:231-237,306-321` socket 退出不取消已派发工作；服务停止才关闭本进程拥有的 owner，`:333-354` | Shell 进程归本机执行器；网络恢复只查原进程／journal |
| 取消提交与原执行终态分开 | Codex `drivers/codex.py:913-966` 排队删除和运行中 interrupt 分开；Claude `drivers/claude.py:444-460` interrupt 后仍 pending | 小叉按原 run 发送取消；拿到请求 ACK 不能直接显示“进程已停止” |
| detach 只结束监督 | `drivers/codex.py:721-743`，`drivers/claude.py:402-412` | 不能把 detach 解释为 Shell 成功、关闭用户原生会话或杀进程 |

Codex 能重连所选原 owner 并按 journal 原资源恢复观察，见 `drivers/codex.py:295-315,376-522`。Claude／ACP 的旧进程 owner 消失后分别明确返回 `previous_sdk_owner_not_attached`／`previous_acp_owner_not_attached`，见 `drivers/claude.py:83-98`、`drivers/acp.py:184-198`；没有伪造重新接管。通用 Shell 服务崩溃后也应区分“有 journal 但实际进程不可确认”与真实退出，不把恢复原记录变成启动同样命令。

## 6. Shell 最少需要泛化什么

建议继续使用现有固定 `terminal_exec`／`terminal_read`／`terminal_control` 与 `environment_status`，不是增加 `local_shell` 的第二套工具。`environment_tools.py:53-89` 已有执行返回 run、增量读取、交互输入和控制的合同。

建议的目标规则：`terminal_exec` 和 `environment_status` 可显式选择 `device_id`；省略仍按既有服务器环境语义。`terminal_read`／`terminal_control` 通过原 `run_id` 找到已固化的执行设备，不能在读取或取消时重新选择设备。多台电脑要用真实 ID 定位，不能拿“当前连接的那个”重定向原执行。

最小改动分为四层：

1. **传输入口**：复用 `DeviceConnection.request`、一个设备 WSS 连接、现有重连与 ACK；在明确的命令／事件执行 kind 上分派原生 Agent 和终端执行。不加第二个控制服务器或端口，不伪造 Agent 实例。
2. **本机执行 owner**：Connector 内拥有普通 Shell 进程与输出；使用当前 OS 用户、明确 cwd 和实际本机程序。维护原 run、退出码、输出读取和控制，不拥有第二个 Yuki Work／Main loop。
3. **服务端原终端回执**：复用 `SandboxTaskRunModel` 的原来源与 terminal completion，而不是 AgentOperation。Host 在首次派发前核本人设备并固化目标，后续按原 run 路由。
4. **原调用与适用的 Work 接续**：普通调用保原工具回执；已经属于 Work 的执行才接其等待与 continuation，由 Main 决定是否继续和汇报。终端状态不变成另一套任务调度器。

`SandboxTaskRunModel` 的现有字段已经能存 request／source／progress／run／completion，见 `sandbox/db_models.py:22-35`；不强制 Agent instance。`SandboxTaskRepository.prepare:30-111` 已在首次发送前锚定原事件／原执行／原 Work；`bind_run:238-252` 保同一原执行绑定；`receive:272-321` 原子完成 task 与唯一 continuation。目标可沿原来源／进度的实际路由资料保存；是否需要新增列要看最终查询和索引，不能仅为字段整齐就要求主库迁移。

但这些对象并非无条件可直接调用。`SandboxTaskRepository.receive` 当前假定事件来自可信 Manager，本身不认证电脑设备。新增 WSS terminal handler 必须先以已认证连接的 device、原 task 中的设备目标、原 request／run 和原 actor 关联核对事实，再在同一写入边界提交 terminal completion；不能允许客户端仅自报任意 request ID 就结算他人的任务。

还需保留当前工具 Host 生成原请求身份的方式，见 `services/agent_tools.py:834-870`，不能让模型自己编 operation ID 替换 Host invocation／execution 身份。

### 不能硬复用的现有结构

| 结构 | 限制是实际模型职责，不是一个多余 if | 最小处理 |
| --- | --- | --- |
| `AgentOperationModel` | `external_agents/db_models.py:56-59` 强制 Agent instance FK；session／native execution 指向原生 Agent 资源 | Shell 不插入这张表，不建虚假的 `shell` Agent instance |
| `AgentRepository.prepare` | `repository.py:345-360` 将 tool 写死为 `external_agent`，只把 prompt／queue／watch 建为原生 run | 复用原终端的 prepare／run；不修改原生监督语义来迎合 Shell |
| Agent 事件投影 | `repository.py:630-656` 每条事件先查 Agent instance；`:705-731` 绑定 native session／execution 并送原生监督输入 | 新 kind 分派到终端处理；不把全部 Agent FK 放宽成 nullable 来做万能表 |
| Agent `_complete` | `repository.py:494-559` 映射 native 状态，结果 kind 固定 external_agent，生成原生 continuation | Shell 用真实 terminal completion，不能复制它的结果字形冒充原生任务 |
| ACP terminal callback | `drivers/acp.py:569-673` terminal 的 owner 是 ACP session，release／stop 与该 owner 绑定 | 可抽取确实共用的进程原语，不能直接把其生命周期当通用设备执行生命周期 |
| ACP Windows 中止 | `drivers/acp.py:654-660` Windows 当前只 kill 直接进程，POSIX 使用进程组 | 通用长命令必须另外核子进程树退出，不复制这段代码后声称跨平台取消完成 |
| 服务器 Manager 文件与 workspace 接口 | 本机绝对路径与服务器 `/workspace` 不共盘 | 文件交付显式走本机文件读取／传输到现有 artifact 发布入口；不伪造共享路径 |

`sandbox/task_repository.py:307-314` 的既有 terminal 取消会阻止其自动 continuation；原生 Agent 的取消可以通过其专属回执向活跃 Work 汇报，语义不同。对通用终端是否需要让 Work 观察取消事实应沿任务生命周期专项修改和验收，不能借用 Agent `_complete` 无意改变所有终端的取消行为。

## 7. 原生 Agent 保持可选，保原 profile 和权限

Codex Driver 当前新 thread 仅传 cwd，turn 仅传文本和 `clientUserMessageId=operation`，见 `drivers/codex.py:832-849`。Claude Options 保原 CLI、preset 和设置来源，未强设权限模式，见 `drivers/claude.py:198-208`。这说明现有桥接不会自动把原生 Agent 变成完整权限，**不是本次需要修复的通用电脑能力缺口**。

只为核清两种概念，额外验证了上游已有参数：本机 Codex 0.165.0 schema 包含 thread 的 sandbox／approval 字段和 turn 的 sandboxPolicy；锁定 Claude SDK 0.2.172 的 `types.py:2380` 与 `subprocess_cli.py:636-637` 也已有原生 permission mode。本次不提出修改这些参数，不把 bypass 配置加入实施项，不改用户现有 thread、原已付请求、profile 或权限。

官方资料仅用于核边界：[Codex App Server](https://learn.chatgpt.com/docs/app-server) 表明原生 thread／turn、流事件、审批和取消已有正式协议；[Claude SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python) 和[权限说明](https://code.claude.com/docs/en/agent-sdk/permissions) 表明 SDK 使用原生设置与权限结果。原生能力不支持时返回实际缺口，不暗中取消重启、不转成新会话或付费重跑。

## 8. 现有测试覆盖与本次还需验证的缺口

以下是源码中已有测试覆盖，**本次只读测试，未重新运行，不能写成本轮测试通过**。

| 场景 | 已有证据入口 | 通用终端新增验收 |
| --- | --- | --- |
| 原命令去重／崩溃窗口／ACK | `packages/yuki-agent-connector/tests/test_journal_connector.py:34,46,106,137,202,232,307` | 同原 request 断线重连只执行一次；ACK 丢失只重传终态；事件投影与查询一致 |
| 真正来源变化与未发出 | `tests/unit/test_external_agent_dispatch_source.py:22` | Shell 首次发送前设备撤销／Work 来源失效不启动命令；已发生终态保原事实 |
| commit 与 WS response 解耦 | `tests/integration/test_external_agent_bridge.py:417,888` | 数据库 writer 排队不阻塞设备 RPC 读取；commit 失败不 ACK |
| 原设备／实例／会话区分 | `tests/integration/test_external_agent_bridge.py:648,689,754` | 两台电脑相同本地 run 文本或同名目录不能串取消；伪报他人 request 不结算 |
| 提交取消不等于停止 | `tests/unit/test_external_agent_final_audit.py:308`、`test_external_agent_cancel_receipt.py:36` | Shell cancel 后检查实际进程树、退出码、终态；长子进程、TTY、用户主动中断分别核 |
| Native parser／driver | Codex `test_codex_driver.py:145,220,480`；Claude `test_claude_driver.py:23-24,72-85`；ACP `test_acp_driver.py:16` | 原生能力回归，不为新 Shell 删除原生去重、原请求、问题 owner 和 profile 规则 |
| 当前缺失的主路径 | 当前 Connector 只有三种 Agent driver，`drivers/__init__.py`；无设备级通用 shell dispatcher | 没有 Codex／Claude 的设备仍能运行 Shell、访问 cwd 外的本人文件、启动已装程序 |

Claude 测试名中的 real SDK 指真实 SDK parser／control 与确定性替身 transport，不是付费模型或任意 CLI 实机接管。Codex 的常规 RPC 测试同样有替身 owner。真实 Codex 全链路测试 `tests/integration/test_local_codex_bridge_live.py:1,21-24` 默认跳过，需要显式付费验收；历史 10-23 的单次 Windows 记录不能代替本次通用执行验收。

本次通用执行的最低真实验收应覆盖 Windows、macOS、Linux：普通非仓库目录、空格／中文 cwd、绝对路径跨目录读写、短命令真实退出码、长命令增量读取、输入与取消、派生子进程、断网后命令继续、重连原 run、服务重启后的 unknown／终态、两个 server／device namespace 不串。

另核无公共本机端口、实际 OS 用户身份、无多装一套 Agent、桌面退出与服务退出区别；这些是实现事实核对，不另立命令审批和项目授权门槛。浏览器截图／下载和本机生成文件必须用显式传输后的真实 artifact 验收，路径字符串不能证明服务器已拿到文件。

## 9. 最小分期与代码量估算

1. **设备传输与原终端路由**：增加明确设备能力、命令／事件 kind 与路由，保存原执行目标；接入同一 WSS／journal／终端 settlement。原生 Agent 行为保持。Bot／Connector 协议变更按实际不兼容性联动版本和包，不能让服务端向未声明能力的旧 Connector 派新命令。
2. **本机终端真实能力**：当前 OS 用户的 Shell／cwd／进程／输出／控制；核 Windows 进程树与 POSIX 进程组；原 run 查证和终态先落盘。
3. **文件和其他程序的交付**：普通文件／程序先由 Shell 直接使用，不堆重复文件工具。服务器确需消费图片、下载或成果时，补显式本机读取／传输桥并复用已有不可变 artifact，不假称 `/workspace` 共盘。浏览器执行由对应调查笔记另列，不能以 Coding Agent 能执行 Bash 代替浏览器验收。
4. **两仓合同与旧文档清理**：固定工具 schema、direct／Code Mode 两种声明、Client API 生成、Connector runtime manifest、打包与 UI 状态一致；协议严格相等检查见 `external_agents/service.py:76-79`。保留原 device／journal／operation，不能用删配置、重新配对完成升级。

仅估算本笔记负责的复用部分：设备 transport／journal 的分派与必要投影约 **100–220 行生产改动**，服务器终端设备路由及认证结算约 **180–360 行生产改动**，相应协议／来源／去重回归约 **200–400 行测试**。本机跨平台进程实现、TTY、UI、文件传输与浏览器不在这两个区间内，需与其负责模块合并去重，不能把多个子报告的估算直接相加。均为理论量级，不是实测 diff 或进度承诺。

本次计数中现有三种原生 driver 为 **2,163 行**（Codex 1,002、Claude 487、ACP 674），Connector／journal 为 **718 行**，服务端 external_agents 的 service／repository／HTTP／models／transport 为 **2,125 行**。复用它们可以避免另写一套连接、Coding Agent 和恢复系统，但新增真正缺失的通用执行能力预计仍会净增代码，不能承诺“接入本机后总代码必然减少”。原生 driver 的 full-access 改造是本轮不需要的范围，生产改动估算为零。

应直接清理的已有说明矛盾：`packages/yuki-agent-connector/README.md:70-72` 仍称 watch 一轮结束，与同文件 `:119-122` 已实现的 session 跨轮监督冲突。更新原段即可，不再叠一条相反补丁。

## 10. 主要风险与处理边界

- 最大实现风险是误把“共用 WS”当“共用原生 Agent 资源表”，导致 Shell 的未知、完成、取消和 Work 归属被 session 模型改写。按执行种类分派，保留领域自己的原回执。
- 第二是网络恢复时把 `not_found` 或 owner 消失当成可重跑。原请求已可能发生外部效果，必须保 unknown，查询原 run／journal；不能后台重跑命令、恢复 Claude 付费 prompt 或重建 Codex thread。
- 第三是 Windows 只杀外层进程、输出 pipe 未 drain 就宣称取消完成，或桌面窗口关闭导致用户误判服务状态。真实子进程退出、输出结尾、服务 owner 分别验证。
- 第四是把本机文件路径当服务器文件。即使 Shell 成功，文件未传输仍只能报告位于哪台电脑；需要交付时走真实传输。
- 不新增上限、额外审批、项目绑定或 shadow Work 来弥补上述问题；先修真正的原执行归属、传输和回执。

本报告完成的是源码和协议调查。任务书、运行时代码实施、测试通过、PR、CI、合并、安装、上线和用户验收仍是不同状态。
