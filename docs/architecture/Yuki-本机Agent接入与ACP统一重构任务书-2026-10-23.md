# 本机 Agent 接入与 ACP 统一重构任务书

日期：2026-10-23（UTC+8）。源码基线：main c2ce7da66ab54efe192993b9615df48266a62137。

**状态：已获授权实施，本地实现与终审中。** 服务器设备服务、独立连接器、三种原生驱动和 Desktop 薄入口已落地；各项验证、真实原生实测与仍缺样的范围见[实施记录](../operations/local-agent-connector-implementation-20261023.md)。尚未合并、部署或公开发行，不把本地协议回归当作生产验收。

遵守[开发约束](development-contract.md)、[主 Agent](main-agent-runtime.md)、[固定工具合同](tool-kernel.md)、[个人 API](client-api.md)、[持久环境](../operations/persistent-environment.zh-CN.md)和[插件架构](../plugin-development/architecture.md)。本文保留本次重构边界与任务矩阵；现行能力由模块文档承接，计划、实现和验收分别记录。

## 1. 交付目标与范围

Yuki 运行在服务器，用户的 Codex、Claude Code 等 Agent 运行在自己的电脑，继续使用本机程序、工程、原生会话及明确获准的本机认证。电脑主动连接 Yuki；用户从 QQ、Telegram、App 或 ACP A 入口委托任务、查询进度、补充要求和回答问题。服务器不安装一份 Codex 来冒充用户本机。

本次包含：独立本机连接器、服务器设备通道、设备与实例目录、原生会话发现和控制、进度与权限问题回传、原操作恢复、固定 external_agent 工具改造，以及旧 ACP B 全消费者解耦。支持多台电脑、同机多个实例／profile、多条原生会话；使用同一套工具和资源服务。

本次不扩展成通用远程桌面、任意操作系统执行平台、新的模型 Harness、插件市场或新的个人客户端产品。现有 Desktop 只对接连接器的安装、配对、状态和启停；不重写聊天界面。设备或原生能力尚不具备时照实返回，不自动接管、重启、换账号、换实例或另起任务。

最高要求：删除与重构优先，禁止防御性编程。新增对象必须说明真实事实源、owner、生命周期和调用者；不造第二套 Work、会话历史、调度器、授权系统或状态同步平台。

## 2. 定稿架构

~~~mermaid
flowchart LR
    U[QQ / TG / App / ACP A] --> M[Yuki Main / Work]
    M --> E[统一 ExternalAgent 服务]
    E <--> H[原 HTTP 服务上的设备 WSS 通道]
    H <-->|本机主动连接| C[独立本机连接器]
    C --> X[Codex 原 app-server / daemon]
    C --> L[Claude 官方 CLI / SDK]
    C --> A[通用 ACP Agent]
    E --> W[原执行回执与 Work 私有输入]
    W --> M
    M --> S[统一 send / Social]
~~~

| 层 | 保留的职责 | 不承担的职责 |
| --- | --- | --- |
| Main / Work | 理解委托、目标、等待、业务完成与汇报 | 克隆原生 Agent 的逐步执行状态机 |
| ExternalAgent 服务 | 当前授权、资源定位、原操作关联、必要事件接纳、原回执查询 | 新模型循环、第二调度器、原生完整历史数据库 |
| 设备通道 | 认证连接、命令与事件传输、重连及确认 | 判断任务目标是否完成、生成聊天消息 |
| 本机连接器 | 实例连接、原生驱动、原操作投递、必要 journal 与对账 | 人格、记忆、业务预算、Work 状态机、第二 PluginHost |
| 原生 Agent | 自己的模型、工具、工程配置、执行和历史 | Yuki 的人物、聊天上下文与平台投递 |

本机连接器同仓独立发行，采用 Python 3.12 的轻量包，复用现有 Python ACP 协议经验和官方 Claude Python SDK；不依赖 Bot 整包或 Electron 才能运行。Codex 直接连接用户当前的原生 owner endpoint，不使用默认另启 runtime 的 SDK 路径。驱动是普通模块，不为标准 Codex、Claude、ACP 新造插件宿主。

建议目录职责如下；文件按实际调用组织，不为目录整齐拆空壳类：

~~~text
src/yuki/identity/                  共用凭据资格，不依赖聊天 Conversation
src/yuki/external_agents/           服务、资源与操作仓库、工具、设备路由
src/yuki/acp_adapter/               保留方向 A
packages/yuki-agent-connector/      独立 pyproject、CLI、后台服务与发行
  src/yuki_agent_connector/
    drivers/                       codex、claude、acp 普通模块
    ...                            连接、journal、配置与系统集成
~~~

服务端沿现有 HTTP 应用提供设备 WSS，不另开一个 API 服务或增加必需的第三方 relay／账户。首次连接复用用户已有 Yuki 地址，电脑主动建立 WSS；远程使用 HTTPS/WSS，反向代理保留 Upgrade。普通自部署用户不需要给电脑开放公网入站端口或配置 SSH。已有代理／CA 配置沿标准配置使用，不硬编码本机 Mihomo 端口。

## 3. 已有证据与当前缺口

### 3.1 本机 Codex 实测

2026-10-23，在 Windows 的既有官方 daemon 0.163.0 上，经 app-server proxy 和标准 WebSocket 握手测试。没有向用户原有工作会话注入输入；模型测试均使用独立测试会话，持久测试会话已归档。

| 已测项目 | 实际结果 |
| --- | --- |
| 现有 daemon 连接与发现 | HTTP 101；读取到 14 个已加载线程，可读 active／idle |
| 文本执行与纠偏 | 原 turn 接受 steer，最终输出 DAEMON_RECONNECT_STEER_OK |
| 双客户端 | 同一 thread 与 turn；观察者收到 8 条文本 delta 和原 completed |
| 原发起连接断开 | 新连接仍读到同一 thread 为 active，原执行继续 |
| 所有测试连接断开后重连 | 同一原执行仍 active，resume 加入原线程，最终输出 DAEMON_ALL_DISCONNECTED_OK |
| 精确取消 | ACK 与同 turn 的最终 interrupted 分别取得 |
| 临时会话失败证据 | ephemeral 新线程被第二连接 resume 时返回 no rollout found；不能推导全部临时会话都不支持控制 |
| 启动时序 | turn/start 返回 inProgress 后立即 interrupt 曾返回 no active turn；观察到真实 active 后取消成功 |

持久测试线程为 01a164ae-104b-7d01-880b-44b48af068fd，纠偏 turn 为 01a164af-d43a-7a51-a61a-dc4f13d9e9b0，取消 turn 为 01a164af-ea04-7aa2-ad42-864feb754c80。全断连接测试线程为 01a164b0-fac6-7902-adf8-9c115da88d4d，原 turn 为 01a164b1-02c4-7433-9a02-ae0cf6f9b25c。失败的 ephemeral 线程为 01a164ad-b17e-7fc3-8b29-ab44f9f5beb7。以上只作测试证据，不用作新业务身份或代码常量。

本地原始记录是本轮临时目录中的 result.json、result-v2.json、result-v3.json、result-disconnect.json；其中 result-v2.json 保留启动后立即取消的失败，本文保存已核对的结论。这里没有验收真实编码、工具 diff、权限审批、用户问答、断连漏事件补齐、进程崩溃恢复、Claude 或服务器端产品链。没有控制用户正在工作的原 Desktop／TUI 线程，不能据此宣称所有独立 CLI 都已能原地接管。

### 3.2 main 中必须贯穿处理的缺口

路径均相对 src/yuki；行号对应本文源码基线，不作为测试断言。

| 编号 | 当前事实与源码 | 重构要求 |
| --- | --- | --- |
| G01 | sandbox/external_agents.py:33–38，非 list 都要求 current_work_control | 目录、状态和历史查询不强制新建或接纳 Work |
| G02 | external_agents.py:65–80；runtime/work_queries.py:81–99，原 Conversation／generation／Work 子树及同 Work peer 门槛 | 个人设备按真实 Person 与授权定位；Work 只拥有自己提交的操作，不独占用户会话 |
| G03 | sandbox/acp_config.py:13–29；acp_peer.py:561–595，服务器 command／env／workspace 与 new_session | 驱动与启动配置归实际 Agent 主机，发现／接入／新建分开 |
| G04 | acp_peer.py:296–420，文件与终端回调在服务器 Linux | 回调跟随 Agent 所在主机，不假装为本机工程操作 |
| G05 | completion_receiver.py:118–140，普通 session_update 直接 ACK，仅 permission 进入 Work | 重要进度、问题、终态真正进入原反馈链 |
| G06 | task_repository.py:185–240,275–318；continuation_worker.py:61–62 | 保留原回执、先提交再 ACK、原 Work 唤醒；抽离 sandbox 专属类型 |
| G07 | services/agent_tools.py:814,829；capabilities/provider.py:22,37–47；sandbox/environment_tools.py:58–78 | external_agent 不再由 sandbox 目录和 sandbox.run 能力表达拥有权 |
| G08 | capabilities/results.py:337；runtime/effect_outcomes.py:96–102,192；work_control.py:329–342 | 新操作语义贯穿结果、效果、等待与预算，不能只搬工具文件 |
| G09 | subagent_repository.py:288–295；sandbox/client.py:187–217；resource_cleanup.py:59–61 | 清退只释放所属订阅／操作；不把用户原生 session 当作自建 peer 一并关闭 |
| G10 | client_api/identity.py:103–192,389–433；db_models.py:16–25，设备凭据强制解析／创建 App 来源 | 抽离共同凭据与人物资格；聊天来源由个人入口附加，电脑授权显式建立 |
| G11 | Plugin SDK 无设备／外部驱动注册；plugin_host/http_client.py 是缓冲 HTTP | 标准连接器不借插件后台任务承载长连接，不新增本机 PluginHost |
| G12 | client-api.md 与架构索引仍有“ACP B 后置”“工具合同 26” | 本次即纠正现状；实施时更新全部模块消费者、旧计划入口和升级文档 |

现有 ACP close 回收的是 Yuki 自建服务器子进程，不能写成当前已经会杀任意用户原生会话。要删除的是把这套所有权规则继续套到用户自有 session 的设计。

## 4. 人物、设备、原生会话与操作

资源关系为 Person → Device → Agent instance → native session → execution。这里是实际归属与引用，不要求给每一层另建业务状态机。

- Person 使用现有内部身份；没有平台账号也可以拥有电脑。账号转移不转移电脑；同人合并保留原设备 namespace、原 actor 和已发生回执。
- Device 是长期安装／配对身份，不是 App Conversation、OutboxOwner、当前 WebSocket 或设备显示名。一次重连只换连接，不换设备。
- Agent instance 是电脑上一个明确的原生安装与 profile／store／owner endpoint。两台电脑、两个 CODEX_HOME 或不同 Claude profile 即使有同名或相同原生 ID，也不能串线。
- 工具使用内部资源 ID；native thread／session／turn／permission ID 只作为原适配器引用，连同 device、instance 和原存储来源保存。名称、路径、端口和短 ID 不单独充当主键。
- 连接器 journal 绑定原服务器安装与设备身份；改服务器地址或换 Person 不暗中迁移旧授权和未决命令。

已有 session 保留原 cwd；新会话工作目录取明确本机配置或获准目标，在实际电脑解析，不继承服务器 /workspace，也不因恢复悄悄切目录。目录是原生执行资料，不为此新增 Project 平台或另一套目录授权状态机。

必须区分三种关系：

| 关系 | 行为与生命周期 |
| --- | --- |
| 只读观察 | 查询目录、状态、历史；不创建原生任务，不要求 Work |
| 监督已有执行 | 原 Work 登记监督关系、接收必要进度；结束监督只 detach |
| 提交新操作 | 原 Work 拥有该操作的原 request／native execution 关联；取消精确作用于该操作 |

watch 监督指定原执行；闲置会话的 watch 在首次真实执行出现时绑定它，原执行终态后收起该观察，新执行用新的监督关系。用户在本地继续输入、审批、取消或修改会话时，Yuki 接受原生事实。不能把“本地没加载”“读到相同状态”“连接断开”判为失败、空闲或无进展。原生执行完成、Work 目标完成、监督结束、消息投递成功分别记录。

### 4.1 授权复用与拆分

共同认证只负责凭据、原 Person、同人关系和当前撤销资格。个人聊天适配器再附加真实 App／ACP Binding 与 Conversation；设备连接不需要伪造聊天入口，不受一次聊天重置、App 停用或 Conversation generation 变化冒充设备撤销。

电脑首次配对明确授予 Yuki 对所选本机实例的访问／控制范围，复用一次性码、哈希存储、撤销和原命令回执。现有聊天 token 不自动变成 connector token，也不能直接作为设备 WSS 身份或自行扩大执行授权。已明确授予的操作不逐次追加人工批准门槛；原生权限需要决定时沿原请求处理。

用户从 QQ／TG／App／ACP 发出的真实委托，可以在本人设备授权范围内进入同一个服务。查询和新效果分别核当前真实授权；不因更换入口强制回原群，也不合并不同 Conversation 的公共历史。平台超管、operator 和设备授予不暗中互相升级。

撤销后既有长 WSS 不能继续接纳新的远程效果；原本已发生的执行与回执保留。本机原生登录、API key、认证文件不上传到服务器，不进入 Main、Memory、聊天账本、命令参数或诊断输出。

## 5. 原生驱动与 ACP 的去向

### 5.1 Codex

默认连接当前官方 app-server／shared daemon，使用实际 owner endpoint 和对应的原生 profile。initialize 后进行目录查询、只读 thread/read，以及确需订阅时的 thread/resume；live steer 绑定原 expectedTurnId，取消绑定原 turn。连接同一原线程，不 fork，不以另起 CLI 读取相同历史冒充接入活跃进程。

Unix socket 上仍是 HTTP Upgrade 后的 WebSocket；官方 proxy 只是字节通道。生产实现使用维护中的 HTTP／WebSocket 库，不把本轮探针手写帧解析复制成生产协议栈。不能自动更新、重启或替换用户正在使用的 daemon。

启动响应与真实运行事件分开。依据同一 turn 的事件／状态操作，不靠固定 sleep 消除竞态，也不因暂未登记 active 重发 turn/start。临时、持久、已加载和只在磁盘发现的线程按真实接口结果处理，不增加统一类型封禁或“未加载等于空闲”的推断。

### 5.2 Claude Code

驱动使用官方 Claude Agent SDK，显式指定用户本机 CLI 路径；不默认运行 wheel 捆绑的另一份 CLI。先在隔离目录验证 Windows 原生 CLI、native preset 和 user／project／local 配置，避免 SDK 默认提示或设置悄悄改变原生语义。SDK 包体仍可能捆绑 CLI，发行大小和实际运行路径要分别记录。

新建／resume 是 SDK 管理的原生执行；不能宣称可以任意加入一个独立、正在运行的 Claude 终端。已有会话的只读发现、闲置恢复和活跃控制分别验证。Windows 当前 SDK 不接受 .cmd／.bat 路径，应识别真实可用 CLI 并给出准确配置结果，不暗中造一层脚本去接管另一个进程。

SDK 会清理自己启动的进程，因此断网不能退出 SDK 上下文；连接器崩溃与单纯网络中断分开验收，不能套用 Codex daemon 的存活结论。认证沿官方支持方式和用户明确配置；cli_path 只证明选择了程序，不证明第三方产品可以提供 claude.ai 登录或订阅额度。本产品不新增 Claude 登录代理、共享额度或认证文件上传。

### 5.3 通用 ACP 与方向 A/B

**A 保留：** 外部客户端 → yuki acp → 个人 API → Yuki。独立 ACP Conversation、原 UUID Outbox、Main／Work／Social 继续工作；cwd 只是资料，不授予电脑执行能力；断开 stdio／HTTP 不自动取消。

**B 重构：** Yuki → ExternalAgent 服务 → 实际主机连接器 → 原生驱动。原有 request、run、peer、permission、cursor、completion 的真实关联继续可读；ACP 协议成为本机的一种普通驱动，文件与终端回调在实际主机执行。按 Agent 真实协商决定 new、load、resume、list、steer 等能力；Codex 与 Claude 不被强迫包成 ACP。

本次不保留 server sandbox ACP 作为隐式后备路径。迁完真实消费者后删除 ACP 专属 Manager job／RPC／启动配置、挂载、poll／completion 路由和安装说明；正常管理员终端、文件、环境生命周期和非 ACP job 继续沿现有职责。

## 6. 一个固定工具合同

保留一个 external_agent，不增加 codex_task、claude_task 或按设备动态加载的工具。部署冻结 action、schema、说明与顺序；direct、Code Mode、Script API 与相关能力声明共用一份。设备／实例／原生能力变化只体现在目录和结果，不改已提交声明。

下一版固定动作如下。公开参数使用内部资源 ID；启动命令、环境变量、凭据、owner endpoint 由连接器配置，不交模型拼接。

| 动作 | 语义 | 是否创建原生执行 |
| --- | --- | --- |
| list | 设备／实例／会话的获准目录和真实能力 | 否 |
| get | 按资源或原操作查询状态、历史、必要事件及游标 | 否 |
| watch | 将已有会话／执行纳入当前 Work 监督，登记唯一原关系 | 否 |
| prompt | 新会话或闲置原会话提交明确的新输入 | 是，登记原操作 |
| steer | 给指定活跃原执行补充输入 | 不创建另一执行 |
| queue | 明确排入原生会话队列 | 登记原排队操作；后续绑定实际执行 |
| respond | 回答指定原权限／用户问题 | 否，保原问题与回答回执 |
| cancel | 定向取消原操作／执行 | 否，ACK 与实际停止分开 |
| detach | 结束本 Work 的监督／订阅 | 否，不关闭原 session／daemon |

旧 close 不作为新调用别名保留；旧 close 回执按原合同读取，不改写历史。真正关闭用户原生实例由明确的本机／设备管理操作负责，不复用 detach 暗中执行。

prompt、queue、watch 沿已接纳 Work 保存业务关联；list／get 不要求 Work；对已有原资源的 respond／cancel／detach 沿实际授权操作，不强制再接纳一项新 Work。steer 沿被控制的原执行或监督关系记录本次真实 actor、当前授权和独立稳定操作键，不复用原 prompt 键，不要求另接纳 Work，也不计为新原生执行。所有工具调用沿原预算记账，不能把 steer 或查询误计成另一个原生模型任务。

队列执行前若来源已撤销，只精确移除原排队操作；不能取消队列中的其他用户输入。缺少精确撤队能力时保留真实未决事实，不假装撤销成功。原生不支持动作或出现执行冲突时返回原能力／冲突，不退化成取消重启、新会话或隐式新 turn。

## 7. 传输、回执与恢复

WSS 采用一个版本化命令／事件合同，连接认证确定设备与服务器 namespace。模型和业务层只使用内部 instance／session／execution ID；服务端核原归属后，设备命令携带原 operation ID、该设备的本地 instance 与 native session／execution 引用、动作和必要参数。事件沿原 operation 回传 native 引用、事件键与真实内容，再由服务端关联内部资源。RPC 序号、一次连接 ID 与原生标题不充当业务操作 ID。

1. 服务器在短事务登记原意图和稳定操作键，再在事务外投递。连接器先查自己对同一原操作的投递事实。
2. 本机接受、原生接受、原生 turn 结束、Work 完成和 Social 送达分别持久化；“命令已到电脑”不能当成“任务完成”。
3. 只可靠记录必要问题、重要进度、终态、投递与未决事实。高频 token／重复状态可在内存合并，不逐 token 写 SQLite，不另存完整原生 transcript。
4. 重要事件先进入服务器原回执／Work 私有输入，再 ACK；重复／迟到事件不重复唤醒、不重复汇报。传输 ACK 不替代原生权限请求的正式回答。
5. 重连按原操作、原 native ID 和已确认游标对账；漏掉的必要事实补读，不能恢复的流式片段注明缺口，不生成伪造进度。
6. 原生接受与本机 journal 落盘之间可能存在崩溃窗口。上游不支持操作幂等时保留 unknown，查原资源；不承诺虚假的 exactly-once，不换 ID 盲重发付费／写入操作。
7. 网络断开只中断传输；本机进程仍持有原生执行。连接器进程退出、系统重启、原生 daemon 退出分别依据协议能力恢复，不混称“断线恢复”。

不为恢复另建全局去重服务、第二队列、总状态同步数据库或定时向模型追问进度。必要本机 journal 是连接器自己投递与未确认事件的事实源，不复制业务目标、完整原生历史或 Work 完成判断。

任何网络、进程、SDK、文件操作都在 SQLite writer 外；只读目录／状态不刷新 last_seen 等热写字段。在线展示来自连接／心跳事实，不能用“在线”证明执行完成或权限有效。断网队列中的旧远程命令重连后先核真实授权，不凭旧连接继续产生新效果。

## 8. 进度、问题、上下文与统一出口

连接器把必要事件交入原 Work 的私有输入与原恢复链；修复当前普通 session_update 被直接 ACK 的断点。关键进度、需要决定的问题和终态沿原 Work 当前可接纳输入的既有合同入队／唤醒；迟到回执照实保存，不凭外部通知复活已结束或已撤权的执行，不另建替代 Work。暂停、等待用户和恢复均复用原生命周期规则，不新增一套通知裁决。同一批变化合并，普通 token 只作为可选观察流，不逐条激活 Main。

Main 按用户要求报告真实进展，通过 send → Social → 原平台适配器；不让驱动直发 QQ／TG，不绕过内部事件与发送回执。任务已经完成但报告失败时，保留完成事实和真实发送结果，不撤销完成、不重传已经送达的部分。用户明确的报告偏好沿现有 Work 合同，不新增固定字数、次数、间隔或“全部先报告完”门槛。

权限和用户问题绑定原 request、native execution、真实选项与当前授权。普通已授权操作不一律升格成人工审批；本地用户已经回答、请求已结束或 native 已撤回时，不复答、不把旧问题应用到新 turn。外部 Agent 输出是待核对材料，不能变成人类授权、系统约束或新的用户控制请求。

Yuki 只取必要的事实、摘要和原生历史引用。原生完整提示词、工具、模型思考、登录资料和逐 token transcript 不灌入 Yuki 的公共 FrozenFragments，不反向把整份 SOUL／Yuki 工具合同注入原生 Agent。跨 QQ／App 查询同一执行不拼接两个 Conversation 的公共历史。

新增工具合同沿合法新请求链生效；不修改原已付请求、原工具配对或 opaque。设备上线／离线、改名、profile 增减不改固定工具 schema；缓存只认上游真实 usage，缺失保持 unknown，不把原 ID、文本哈希或同会话当成命中证明。

## 9. 生命周期、安装与旧链删除

连接器独立于桌面窗口常驻，以所选原生 CLI／profile 的实际 OS 用户运行，保留该用户的 HOME、CODEX_HOME、认证和 owner endpoint；不默认以 SYSTEM／root 服务访问另一用户的原 daemon，不为常驻自动提权。用户级安装、配对、实例选择、状态、启停、日志和卸载应可由独立 CLI 完成；Desktop 调同一个服务，不另开 daemon 或重实现驱动。没有桌面客户端的用户也能安装使用。原凭据进 OS 安全存储或系统允许的私有文件，journal 与实例配置保留稳定 namespace。

独立包提供自己的依赖锁定、类型检查、定向测试与 wheel 构建，在不安装 Bot 的干净环境安装实际 wheel，验证 CLI、驱动导入和服务启停。现有根包 CI 不自动覆盖 packages 目录，必须将该包检查显式接入现有工作流；正式发行与 Desktop 调用同一版本产物，不再维护第二套连接器实现。

Desktop 的 OutboxOwner 含 character／Person／Binding／Conversation，不能作为电脑安装身份；Electron safeStorage 不直接成为无头服务依赖。旧 executor 的一次性 acknowledged→unknown 恢复规则不能替代原生长期会话对账；不整体合并旧 executor 分支。

关闭 UI、断网、结束监督、Work 终态、聊天重置均不能顺手关闭用户 daemon、兄弟 session 或其他人的执行。撤销设备／来源停止新的远程效果；仅对确属该 Work 的原操作按原合同定向取消。用户本地执行不因失去 Yuki 控制权被全部杀掉。

迁移清单必须包括：

- 外部资源／操作模型与历史回执 reader；原 request、run、peer、permission、cursor、completion 不按死代码清空。
- sandbox/external_agents、acp_manager、acp_peer、配置、Manager ACP RPC、completion_receiver 中的 ACP 专属消费者；只有真实依赖迁完才删除实现，不能整块删除普通 sandbox 公共设施。
- capabilities、AgentTools、effect_outcomes、Work 等待与归档、隐私清退、Subagent 取消、Code／Script 投影及 SDK 说明。
- 环境 ACP SDK 安装脚本、只读 peer 挂载、运维样例、配置字段、错误信息、提示词与冻结旧行为的测试／CI。
- 现行 module 文档、架构索引、#299／#455／#456 的当前状态入口。历史日期证据不改写为本轮新合同。

切换时停止旧链新接纳；既有活动 ACP B 操作按原 ID 保留观察与结算，分别确认完成、由明确授权精确取消或保留未决。不批量重置 Work、不丢预算、不凭切换重跑。可证明原资源能由新驱动继续观察时保原关联；无法证明时明确未决，不伪造迁移成功。删除旧新调用路径不等于删除尚在执行的进程及其事实，也不把所有活动必须结束设为升级门槛。不保留可接新调用的旧 server ACP runtime、双写、旧工具别名或自动 fallback；必要原活动结算、历史 reader 与冻结迁移可以保留。

## 10. 实施任务与顺序

以下为施工范围，实现和验收状态逐项记在实施记录；原有任务书复审和早期探针不独立算实现完成。阶段可以并行准备，依赖处按同一合同合流；某一供应商限制不阻断不依赖它的工作。

| 编号 | 任务与交付物 | 依赖 |
| --- | --- | --- |
| T01 | 冻结资源／操作／事件语义，确认原实体复用及唯一 writer；列出全部消费者与旧回执升级路径 | 当前任务书 |
| T02 | 从个人聊天认证抽出共同凭据资格；实现显式 connector 授权、撤销与不依赖 Conversation 的设备身份 | T01 |
| T03 | ExternalAgent 应用服务、目录、原操作仓库和设备 WSS；复用原 HTTP 应用与 Work 恢复，不造新调度器 | T01/T02 |
| T04 | 独立连接器包、依赖锁定、CI／wheel 发行、配置、安全存储、journal、实际 OS 用户下的服务与安装／卸载 CLI；干净环境不安装 Bot 整包 | T01/T02 |
| T05 | Codex 原 owner 发现、只读／订阅、原线程 prompt／steer／queue／权限／取消；维护库传输 | T03/T04 |
| T06 | Claude 指定 CLI 与原生配置验证、新建／resume／事件／权限／取消；分清只读发现和真正 live attach | T03/T04 |
| T07 | 通用 ACP 驱动、实际主机回调、真实能力协商，删除 server workspace 假设 | T03/T04 |
| T08 | 固定 external_agent 新合同；direct／Code／Script／能力／效果／预算／恢复全部消费者同改 | T01/T03 |
| T09 | 重要进度／问题／终态可靠入原 Work；原 send 汇报、跨入口查询与精确原请求回答 | T05/T06/T07/T08 |
| T10 | Work／Subagent／隐私清退生命周期解耦；断网、撤销、归档不关闭用户整个 Agent | T03/T09 |
| T11 | 替换旧 ACP B 数据与新调用链、删除全部专属旧路径，保原回执 reader；同步现行文档与升级手册 | T05–T10 |
| T12 | Windows 端到端、Linux 服务端、macOS/Linux 安装与生命周期适配验证；Desktop 薄接入同一服务 | T04/T09/T10 |
| T13 | 完成下列验收矩阵、删除复审和交付记录；按授权分别提交／PR／合并／部署 | T01–T12 |

实施时先用独立 Windows 目录验证原生工具、文件变更、审批、问题及崩溃窗口，再扩展产品入口；不能用协议握手替代这些测试。Linux／macOS 不靠路径替换宣称支持；本任务应完成相应服务与凭据适配，实机缺样要准确标明，不能冻结为永久 OS 特判。

## 11. 验收矩阵

先做真实协议＋可控故障回归，再做 Linux Yuki → Windows 连接器 → 原生 Agent 的隔离端到端；自然聊天只观察获准流量，不主动向真实群发测试消息。模型版本、实际认证、费用／usage、原生 CLI 与 SDK 版本逐次记录。

| 编号 | 场景 | 观察结果 |
| --- | --- | --- |
| V01 | 无 Work 查询设备、状态和历史 | 无原生新 turn、无 Work 接纳、无会话分叉；readonly 不热写 |
| V02 | 两台 PC、同机两 profile、同名／相同 native session ID | 内部资源明确隔离；目录变化不改工具声明 |
| V03 | 同 Person 经 QQ／App／ACP 查询和获准控制 | 指向同一原资源；公共聊天历史互不拼接 |
| V04 | 账号转移、Person 合并、原 Person 停用 | 转账号不转电脑；合并保 namespace；停用不被归并复活 |
| V05 | 聊天 token、connector token、operator、其他 Person | 不互相冒充／升级；跨人无权；显式委托可执行 |
| V06 | 撤销仍在线 WSS、断网旧命令重连 | 不再产生新远程效果；原本机运行与历史回执保事实 |
| V07 | 正常新委托、工具命令、文件编辑、diff | 原 operation 绑定真实 thread／turn 与效果，真实进度到原 Work |
| V08 | 原生接受后断确认／journal 崩溃窗口 | 查询原操作；未知保持 unknown，无盲目重发或假 exactly-once |
| V09 | 运行中 steer、expectedTurnId 冲突 | 原执行继续；冲突保真实结果，不退化 interrupt／fork／新 turn |
| V10 | 原生 queue、撤权、精确撤队能力缺失 | 原队列项关联原操作，不重提交、不误取消别人的输入 |
| V11 | 用户本地和远端同时操作 | 原生事实和执行 ID 保留，不凭本地缓存覆写原 owner 状态 |
| V12 | 所有连接断开、重连、遗漏必要事件 | 原生继续；原 ID／游标对账；不可恢复片段标缺样 |
| V13 | 连接器、SDK、daemon、系统分别退出 | 各自恢复结论有独立证据，不把网络重连冒称无损崩溃恢复 |
| V14 | 取消 ACK 与真正停止、终态后迟到进度／问题／完成 | 同一原执行结算；ACK 不冒充 interrupted；迟到事实不复活 Work 或重发 |
| V15 | 原生审批、用户问题、本地已答／过期／重复回答 | 原问题持久关联与当前授权，精确回答；无重复或串 turn |
| V16 | 高频 token、重复进度与真实里程碑 | 不逐 token 写 DB／唤醒 Main；重要进展真实进入原 Work 并 send |
| V17 | 汇报失败、部分送达、Work 已完成 | 真实完成与 Social 回执独立，不撤销目标完成或重传已发部分 |
| V18 | 结束监督、Work／Subagent 归档、聊天重置、UI 关闭 | detach 所属关系；不关闭用户 daemon、兄弟会话或无关任务 |
| V19 | Claude 配置／认证／Windows CLI 与已有活跃终端 | 实际原生配置可核；new／resume／readonly／live attach 不混称 |
| V20 | 通用 ACP 能力缺项与实际主机回调 | 真实能力和错误；本机文件／终端确在本机；无隐式服务器 fallback |
| V21 | ACP A new／load／prompt／cancel／断开 | 原 Conversation、UUID、Main／Work／Social 不退化；cwd 不授予设备执行 |
| V22 | 新工具合同与未决旧请求、旧 ACP B 回执 | 新链无旧 close 别名／server ACP 调度；原回执可读且不重跑 |
| V23 | 多设备上下线、改名、profile 变化、跨入口询问 | schema／顺序固定；不改原已付输入；真实 usage 缺失仍 unknown |
| V24 | 实际发行包的 CLI 安装、升级、卸载和 Desktop 退出 | 干净环境不安装 Bot 整包；实际用户／profile 和 cwd 正确；原 journal／配置不暗换归属；不擅自关闭用户 Agent |

测试检查真实行为，不冻结目录布局、调用次数、固定延迟或工具数量。写竞争验证短事务与外部 I/O 分离，不用新全局锁把所有会话串起来。不同原生会话可独立执行，同一 session 的输入次序与冲突交由原生协议，不新增 Yuki 并行模型循环。

## 12. 复审、交付与资料

### 本轮计划复审

| 轮次 | 检查与已纳入的修订 |
| --- | --- |
| 1 | 对照当前 main 与共同合同，区分 ACP A/B 现状、本机新范围和 daemon 探针证据 |
| 2 | 并行追查工具／Work 消费者、设备身份／Desktop、原生回执／进度，补 G01–G12 与 T/V 对照 |
| 3 | 反向审查成稿，补 steer 独立操作键、旧活动执行结算、真实 OS 用户、原 cwd、独立包 CI／发行 |
| 4 | 交叉核对原始失败记录、终态迟到事件与 Work 合同，复查新旧范围、局部证据、文档入口和链接 |

这些是任务书审查，不是 T01–T13 已实施，也不表示 V01–V24 已验收。

实施终审至少覆盖四个方向：①人物／设备／授权；②原操作／崩溃窗口／迟到回执；③Main／Work／上下文／统一 send；④全消费者删除、发行与升级。审查发现的新 coordinator、镜像状态、兼容 alias、隐式重启／重发、逐 token writer 和原生会话生命周期耦合，应先删再验真实行为。

交付记录分别列：任务书、本地实现、定向测试、真实 Provider／Windows 联调、CI、提交推送、PR、合并、镜像、部署和自然流量。某项未做即保留未做；一项供应商验证不能代替其他驱动验收。用户随后已授权实施；本轮提交与 PR 独立记录，未执行生产部署或公开包发布。

### 参考资料与采用边界

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)、[官方 CLI／daemon 命令](https://learn.chatgpt.com/docs/developer-commands)：原生会话、事件、steer 与多客户端；本机 Windows 探针只覆盖 §3 已测项。
- [Claude Python SDK](https://code.claude.com/docs/en/agent-sdk/python)、[v0.2.172](https://github.com/anthropics/claude-agent-sdk-python/releases/tag/v0.2.172)、[固定源码](https://github.com/anthropics/claude-agent-sdk-python/blob/6b4a557e9f395934bd16e78b04e0f09833eb387e/src/claude_agent_sdk/_internal/transport/subprocess_cli.py)：指定 CLI、配置与进程生命周期；安装复用不等于任意活跃终端接管。
- [Claude SDK 认证说明](https://code.claude.com/docs/en/agent-sdk/overview)、[Quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart)：产品认证必须按官方允许方式核实，不能由技术连通推导订阅登录授权。
- [ACP Session Setup](https://agentclientprotocol.com/protocol/v1/session-setup)：可选能力分别协商，不强造所有 Agent 相同的加载／纠偏行为。
- [Memoh Remote ACP 实验 #1007](https://github.com/felinics/Memoh/pull/1007)、[本机原生 Agent 目标 #1152](https://github.com/felinics/Memoh/issues/1152)：研究过服务器访问已连接电脑的路线；实验暂停／未交付不等于技术不可行，不照搬连接关闭就结束子进程的生命周期。
- [Happier 稳定版](https://github.com/happier-dev/happier/releases/tag/cli-stable)、[原 daemon 传输](https://github.com/happier-dev/happier/blob/b8d1c449c46ab4dfa8637db36b66f643d149e66d/apps/cli/src/backends/codex/appServer/daemon/codexAppServerDaemonTransport.ts)：参考原 owner 发现、控制与历史导入的区分，不引入整套产品运行时。
- [HAPI shared sessions](https://github.com/tiann/hapi/blob/5d8deb201189dc1f183c4f5bf38c87ec4d34dd70/docs/guide/codex-shared-sessions.md)、[Paseo 事件合同](https://github.com/getpaseo/paseo/blob/d972f67c3fea06fbc163608f5ddcc2c443de83ac/public-docs/sdk/events.md)、[cc-remote](https://github.com/muggle-stack/cc-remote)：参考本机原生运行、远程控制、游标与能力边界；不能把 resume 历史统称为 live attach，不接入其第二套任务／归档系统。
- [OpenClaw Codex supervision](https://docs.openclaw.ai/plugins/codex-supervision)：参考原会话只读目录和监督边界；paired-node 的最终文本 continuation 不等于完整事件／审批控制。

以上方案采用边界和取舍属于本次设计判断；第三方源码、官方声明、本机实测与最终 Yuki 产品验收分别认各自证据。
