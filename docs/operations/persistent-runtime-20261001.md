# 共享持久 Runtime 重构实现记录

实施基线为 `b1382cf80429d82e5071275d5bc97f594209ed0b`，实施分支为
`codex/persistent-runtime-core`。本记录说明代码归属与验证范围；合并、镜像与生产状态
分别以 PR、镜像 revision 和部署时的只读检查为证，不能从本文推断当前已上线。
原施工任务书已退役，现行规则见 [主执行合同](../architecture/main-agent-runtime.md)。

## 生产入口与所有者

| 入口 | 来源与激活所有者 | 共享执行资源 | 结果与回执接受者 |
| --- | --- | --- | --- |
| 普通聊天 / 自主聊天 | Chat 的内部事件、原 turn/generation；root lease；没有持续目标时不强制建 Work | Runtime 主 TurnService / Runner | 原 Chat sender、账本、Work effect |
| SELF 首次触发 / Work 恢复 | 原 initiative/SELF 来源；WorkResumer 验证场景与 Presence | 同一主 TurnService / Runner | 原 SELF 反馈、Work 回执 |
| Person / SELF Automation | 原 run/step、script hash、委托及当前授权；DurableInvocations 自有 root | 显式注入同一主 TurnService / Contract | 原 step cursor；类型化效果核对；不重跑 unknown |
| 插件 SDK 主调用 / 续跑 | Host 的内部事件与安装批准版本；DurableInvocations；拒绝递归借权 | 同一主 TurnService / Runner | 原 invocation 同步结果；Host 持久 Work |
| 插件后台主调用 | 原获准目标与事件；原 WorkResume/主入口 | 同一主 TurnService / Runner | 原后台交付合同 |
| 沙箱完成 | 原 request/run 与持久 completion；原 Work/child 消费者 | 原来源对应的共享入口 | 原完成通知与 execution receipt |
| child | 原 parent 授权与预算；独立 child lease；SubagentExecution | 同一 Runner，独立工作者提示词和工具子集 | 原 child result 与父任务输入 |

ApplicationContainer 引用 Chat 构造的唯一 `YukiRuntime`。主工具在插件登记完成后冻结，
然后启动 Runtime，再启动插件后台主调用。外层只注册一次 Runtime 生命周期，
它内部拥有 root/child 两个调度 worker。执行任务登记只存 task 与嵌套计数；
actor、gateway、Memory session、transcript 与 WorkControl 随各自调用隔离。

## 职责删除与保留

| 原职责 | 最终归属 / 删除结果 |
| --- | --- |
| Automation 私有 Runner 和沿 runner/contract/chat 获取主服务的 helper | 删除；构造时显式注入主服务与合同 |
| `automation/agent_delivery.py` 直接扫描其他领域内部表 | 删除；原算法归 RuntimeEffectQueries，Automation 只用类型化结果 |
| WorkScheduler 的来源恢复、场景装配、平台发送与 Chat 私有字典写入 | 删除；WorkResumer 与 OneBot adapter 分别承担业务恢复与协议翻译 |
| SubagentScheduler 的全应用定位器、Memory/Runner 装配和执行体 | 删除；显式 SubagentExecution 依赖，调度器保留候选与容量 |
| Chat `_active_work` 任意读写 | 删除；ActiveWorkBindings 在激活内登记并按身份注销 |
| root/child 重复 ContextVar、心跳、恢复、结算与 lease 释放 | 共用 bind_work_activation，root/child 进入与 finish 差异保留 |
| MainAgentTurnService 再次调用自身初始化持久 invocation | 删除；DurableInvocations 调用已准备执行路径 |
| ContextAssembler 登记准备等待、计时与结算 Work | 删除；prepare_context 在调用边界停放原 Work |
| 通用时间等待依附 AutomationWorker | 移交 WorkScheduler 的独立时间维护循环；候选恢复循环不重复轮询；原 claim 前后防丢唤醒保留 |
| 沙箱 continuation worker 持有整个应用 | 删除；只依赖 WorkRepository |

原 Work 状态、journal、输入、effect、Automation cursor、lease fence、invocation key/hash
与预算继续作为权威事实。不新增表或 migration，不保留内部兼容 re-export、旧 Runner
fallback 或过渡 setter。旧 notice、legacy delivery、沙箱完成通知和来源专有结果桥接
仍有持久记录消费者，保留其实际行为。`work_state` 仅是来源桥接使用的派生结果，
不是平行可写状态机；ActivationOutcome、WorkActivationHandled 与 WorkRecoveryDeferred
仍保留不同退出和恢复含义。

关停先停止新执行，停止 workers，再取消和收拢现有执行；原恢复、预算计量及释放先于
数据库关闭。启动失败和退出取消均继续清理已启动资源。acquire 期间关停会释放已取得的
lease；关闭后新入口在准备或申请 lease 前拒绝。

WorkScheduler 的监督任务拥有候选恢复和时间维护两个循环。时间循环每 2 秒调用
`deliver_due()`，源码检索确认这是唯一生产调用；root 恢复等待多段模型请求时，其他
Work 的到期信号仍能登记原输入并入队。扫描异常保留独立 `wait_error_category` 并重试；
任一循环意外终止时记录错误、取消和收拢另一循环，health 的 `running`、`wait_running`
显示停止状态。关停等待监督任务及两个所属循环全部结束，二次关闭取消不打断正在进行
的收拢，再关闭数据库。

## 行为验证对应

定向阶段复用既有真实 SQLite、fake Provider 与当前 fixture；没有机械重复无关全量。
以下列出行为矩阵的检查归属，不把每行解释成新建了一整套测试。

| 场景 | 主要回归归属 |
| --- | --- |
| A01、A02、A17 普通聊天与准备/reset/隐私边界 | test_chat_context_preparation_gate、test_rollup_chat_wakeup_wire、test_capability_runtime_security |
| A03、A05、A13、A14 原 Work、重复通知、同步结果与预算 | test_runtime_work、test_work_protocol_continuity、test_main_agent_entrypoints |
| A04 跨 task 输入与旧绑定退出 | test_activation_bindings、test_work_input_preparation |
| A06、A07、A19 独立 child、失效权限、共享资源与临时状态隔离 | test_subagents、test_capability_runtime_security、test_yuki_runtime |
| A08、A09、A10 原 run/cursor 与 unknown | test_automation_run_admission、test_automation_timeout_certainty |
| A11 等待与 claim 释放竞争 | test_automation_runtime、test_runtime_scheduler_boundary |
| A12、A20 来源授权、固定合同与实际协议 | test_main_agent_entrypoints、test_automation_mutation_boundary、main_agent_wire_cases、两项 Automation 交付集成文件 |
| A15、A16 接纳后 cleanup、租约与心跳 | test_work_delivery_ownership、test_lease_heartbeat、test_activation_bindings |
| A18 开关、时钟归属、错误隔离与关闭 | test_runtime_scheduler_boundary、test_yuki_runtime |

新增关停/绑定核心 17 项定向通过；调度、恢复与既有媒体合同 83 个不同定向 case 通过；
上下文等待边界 13 项通过。Automation 交付、claim 竞争、真实 Person/SELF 装配以及主入口
的定向验证亦通过。终局本地完整 pytest 为 2053 passed、8 skipped（751.70s）；跳过项为
私有备份未提供及 Windows 无法验证的 POSIX 文件合同。全仓 Ruff、671 个源文件的 Linux
目标 Mypy、3.9.0 发布基线检查通过。其后独立审查修正关停期间嵌套入口的取消语义并定向验证；
最终源代码仍须通过最新 PR 的官方 Linux 全量 CI，不能把之前的本地数字冒充最终 head 门禁。

PR #214 初版提交 `4d4c187` 的官方 Linux CI 为 2061 passed、1 skipped。该 head 先于
独立时间维护循环的追加修正，不能作为追加修正后最新 head 的门禁或上线证据。

终局时钟核查使用固定时钟、Event 阻塞恢复器和真实 SQLite，先确认长 root 恢复会阻塞
另一 Work 的 timer；移交独立维护循环后，`test_runtime_scheduler_boundary.py` 单独重跑
8 项通过（2.50 秒）。其中 3 项新增回归验证阻塞期间到期信号与零预算、循环意外退出的
health 及所属任务收拢、二次关闭取消不打断收拢，其余 5 项是已有开关、幂等、扫描错误隔离
与媒体交付场景，不与
此前 83 个 case 重复相加。两份改动文件的 Ruff 检查/格式与 scheduler 的 Linux Mypy
通过；这些是本地追加修正证据，PR #214 最新 head CI 和生产部署状态仍须另行确认。

## 兼容与性能证据边界

旧源码固定到上述 SHA，在隔离 SQLite fixture 中创建 root/child、时间等待、未消费输入、
accepted/unknown 效果及原 Automation run/step。新程序有限激活原 Work、消费输入并交付原时间信号，
旧程序随后读取新事实。原 ID、script hash、来源与 certainty 保留；root/child 共享预算
从 9/5 累计到 11/7，Automation 预算 5/2 保留。没有重建目标或把 unknown 重新 prepare。
这证明该基线的数据字段兼容，不证明任意旧二进制可回退，外部回执仍须按原 ID 查证。

另用原 WorkSession 测试 fixture 验证 journal 旧写、新原链追加、旧继续写入：work、chain、
contract、source_revision 保留，模型/工具预算为 3/1 → 4/1 → 4/1。旧代码读取新追加内容，
再次查已 accepted 的 call 时返回原 receipt，禁止重跑的 fake tool 没有执行；随后原 lease
checkpoint/save 成功。这是合成 journal 兼容验证，不替代真实模型调用或来源授权验收。

离线同种子 DB、同有效配置、固定 20ms fake Provider 延迟的首模型检查：旧、新各 20 样本，
完整 messages/tools/native tools/model/settings 逐样本一致。首请求前两边均为 5 个 writer
事务、14 次应用 DML、2 次 BEGIN IMMEDIATE；旧 p50/p95 为 159.88/244.66ms，
新为 154.10/232.26ms。顺序执行存在系统与缓存噪声，这些数值只能作为该 fixture 的结构成本证据，
不能宣传线上提速。初轮样本因旧 checkout 的 dotenv 启用语音、有效配置不一致而作废；
公平样本禁用 dotenv 并比较完整请求，而非仅比较哈希。

带固定主合同及显式发送的第二个 profile 走 SocialService 到 FakeBot.call_api，旧、新各
20 个样本。完整首模型请求一致；fixture 装配 43 项工具，两边相同，不冒充生产完整清单。
首模型 p50/p95 为 193.54/285.03 → 198.42/279.70ms；首 fake 发送为
247.40/342.63 → 249.44/328.19ms。每个样本首请求前两边均为 10 个 writer 事务、
20 次应用 DML、4 次 BEGIN IMMEDIATE。中位数基本不变，仍没有稳定提速证据。

未在本轮离线性能采样中施加锁竞争，也未采生产首发送、空闲 tick/wakeup 或真实 Provider
长期缓存指标。容量、等待无额外模型调用、取消和副作用保护由行为回归验证，
不冒充相应生产性能测量。真实 QQ 社交效果另有验收范围，本轮不发送测试 QQ 消息。

## 发布和回退边界

PR 最新 head CI 通过后合并，使用合并 SHA 构建 linux/amd64 ops 镜像；不创建正式 Release/tag。
生产首次只读检查确认原 revision 为实施基线、schema 为 0084。部署脚本现场解析 Compose
labels 与最终 bot.image 所有者，加载并验证不可变镜像，在修改前再次复核原 Bot 基线。
只停止并替换 Bot；停止后用 SQLite backup API 保存主库和 participation 库，完整 integrity/FK
检查在备份上执行。代码回退固定原镜像、保留当前数据库，不 downgrade 或覆盖上线后数据。
健康检查包含 OneBot、root/child、插件、沙箱恢复、Automation 与冻结主合同；SnowLuma 的
ID、启动时间、重启次数单独核验。原 active Work 使用有界原 ID 快照对照，不能以健康绿灯
声称所有历史队列或真实外部能力已验收。
