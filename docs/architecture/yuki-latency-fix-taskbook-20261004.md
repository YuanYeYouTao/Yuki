# Yuki 回复延迟修复任务书

日期：2026 年 10 月 4 日  
复核修订：2026 年 10 月 4 日 第二版  
适用基线：`a48b8d1387c62c2bcafda0e4faca056619311079`  
配套审计：`yuki-latency-audit-20261004.md`  
状态：2026-10-05 按用户授权实施；原审计及第二版复核是历史背景，当前交付状态见下表。合并、上线与自然回复验收分别记录。

## 2026-10-05 实施核对

用户本轮明确授权替换开发约束、核对任务书、修复、合并与部署。下列完成项以当前源码与隔离回归为依据，不能使用本文件原审计测试数充作修复验收。

- [x] 共同开发约束已用用户提供的精简版原文替换；模块合同继续保留必要细节。
- [x] 已有实现复核：增量 selection、只读 source guard、同连接健康 ingest 热路径、空维护 fast path、非阻塞 soft rollup；本轮保持这些语义。
- [x] T01：单次 frozen ID 集、批量 observation 构建与回归；保留无条件 fresh 校验。
- [x] T02A：同一完整请求估算与同视图渲染复用，包含 schema 数值类型/负零变化、完整 native/media/opaque 输入；最终 Runner hard check 保留。
- [x] T02B：单次准备 immutable alias/timezone DTO，expected turn 四维校验在显式 BEGIN 内且先于历史正文；Runner 使用本轮 timezone 刷新 clock。关系仍走原有 get_or_create，合法首次创建保留；SELF/actorless 不借用普通 Person DTO。
- [x] T03A：阶段时钟、连接取得、锁、请求/发送计量。
- [x] T03B：有界消费者承接诊断来源检查、编码和提交；冻结归属与隐私、真实线程收尾。
- [x] T03C：Provider 成功/原异常/取消与诊断失败隔离。
- [x] T04：0093 候选匹配索引、完整形状预检、ORM/schema guard/版本基线一致。
- [x] T05：冷探测前释放外层连接，冻结原连接/Presence/Binding 版本，恢复 CAS 与 fresh admission 复核；保留热路径和已接纳消息的持久账本规则。
- [x] T06：无效 reflection 映射准备期剔除，全无效只读返回；writer 竞争复核保留。
- [x] T07：prefix/items 准备复用，旧 snapshot 的 view/payload/stamp 绑定和 writer 最终 CAS 保留；manifest/chunk 存储重构未满足自然 WAL 写放大进入条件，本轮不实施。
- [x] T08：先交付维护前置和串行恢复分段计量；跨 Conversation 并发恢复缺少自然队头阻塞证据，本轮不启用。
- [x] T09：PR242 已按后续用户授权实现普通私聊的旧轮抢占。无 accepted Work、无已开始本地效果、无 native dispatch 时精确取消原 task 并等待真实退出；效果保护持续到原 task 退出。群聊、Work 和已开始发送保持原策略；发送后解除保护未实施。
- [x] T10：分页分组/ProtocolStore 锁粒度改造需实测条件，当前不满足，保留完整来源和原锁语义。
- [x] T11：普通聊天无显式自动预取成本；不增加预取或新的恢复假设。
- [x] 定向回归、重放与独立审查；仓库 Ruff、Linux mypy（699 源文件）、发布/schema 基线检查通过。

- [ ] 新版本自然回复速度验收；健康检查与合成回放不能替代此项。

PR241 交付原任务书的 0093 基线；后续 PR242 的全部 CI 已通过，已合并为 `00ff0dbd401d61f5433fd2b21cb5116124197894`。PR242 另加 0094（down_revision=0093）的 `ix_runtime_work_inputs_abandoned` partial index，仅覆盖 `state='pending' AND ready IS 0`，后台发现条件、writer 复核、原 Work 状态与预算保持不变。实际上线须核对容器 revision/StartedAt 与 0094 迁移回执；真实控制测试与自然速度验收分别记录，不用本地验证或 CI 代替。

当前隔离证据：T02B alias+timezone 阶段从 12 SELECT 减至 10，删去普通准备独立 generation status 和 Runner 第二次 timezone 解析；不将父阶段与子阶段相加。T05 单连接池可在 probe 内另取连接，身份/版本变化拒绝、probe/CAS 前取消、提交确认丢失或提交后取消均不盲重试/反向删除，已接纳后仅 socket 断开仍可入账。T04 真实完整 ORM 候选查询在 187/2000/20000 items 合成 fixture 中由 SCAN 改为 covering SEARCH，VM 步数 4369/33377/321377 → 1441，完整结果及 LIMIT 前缀等价；这不是生产提速比例。T06 全无效准备为 2 SELECT、零 BEGIN IMMEDIATE/DML。

独立定向组：root 101 项通过，随后 metadata 最终 7 项、cold/group-metadata 最终 16 项、旧调用入口与完整历史 56 项通过，覆盖重叠不累加；T04/T06 为 85 个不同用例。后续最终验收记录应使用最终树的结果。

纯准备五次合成分布：T01 固定 F=32/E=32/K=256（含重复输入 512）旧逐项 load 为 256 次/41088 items，批量为 1 次/288 items；median 431.42→2.08 ms，items/messages 完全一致。T02A 完整请求估算 6→2 次，含复用检查和两次快照 deepcopy，median 32.77→26.14 ms。该本地纯准备分布不代表线上总时延或 Provider 速度。

## 本次复核变更

本修订任务书取代同日首轮任务书。main 基线仍为上述 SHA；首轮主报告及 DOCX 的源码事实、隔离测量保留，其中的实施建议以本修订和现行开发合同为准。已交付 ZIP 保留为首轮冻结证据，不代表内含任务书已同步本次修订。

- T01 将局部集合、batch 与 lazy fresh 拆开；lazy 跳过未选候选校验属于独立语义决策，不阻塞前两项交付
- T02 改用完整请求的同口径估算值；资料 DTO 只复用现有依赖，不新增统一版本协议或展示资料变更重试策略
- T03 以冻结身份、隐私防回灌和业务结果隔离为不变量，推荐在同一个有界消费者中完成冻结 ID 核验、编码与条件写入；同步修改两份模块合同及旧测试，补原始资源上限与真实线程收尾
- T04 细化索引对象形状与缺失索引的降级拒绝；T05 区分探测取消、提交未知、已接纳后的连接断开与路由判定零额外 DML
- T08 补维护前置与恢复串行分别计时；验证按改动风险选取，不机械重复无关全量检查

复核依据：[开发约束](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/AGENTS.md)、[共同开发合同](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/docs/architecture/development-contract.md#L180-L235)、[执行轨迹合同](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/docs/architecture/execution-trace.md#L35-L67)、[Canonical 入站合同](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/docs/architecture/canonical-runtime.md#L52-L72)。

本次复核针对现有 telemetry 参数化测试先单独运行成功分支 1 passed，随后完整两分支 2 passed；两次覆盖重叠，不累加为 3 项。结果只确认“成功模型后非 SQLAlchemy 诊断异常仍冒泡”的旧行为，不是修复通过。其余新增验收均待实施，未跑全量测试、迁移链或生产验收。

## 1 实施目标与不可改变的边界

第一批目标是去掉普通上下文构建中的超线性工作、同请求重复准备和可丢诊断关键路径等待，并修复一个确定的维护查询索引缺口及冷入口连接生命周期。没有承诺固定线上提速百分比；最终收益由自然流量验收。

实施前必须确认目标源码与本报告 SHA 的差异。已经存在的增量 selection、只读 source guard、健康认证入口、空维护 fast path 和非阻塞 soft rollup 不应重新实现。

所有任务遵守以下合同：

- 原 conversation、event、actor/read scope、generation、privacy generation、run/operation/call ID 不换身份
- 原 request reservation、累计预算、journal、效果/发送回执保持必要持久性，不能进入可丢诊断队列
- 当前 epoch 冻结 wire/messages 与工具顺序不变；只有原有显式边界允许换 epoch
- source/privacy/CAS/lease 失效照常拒绝；提交确认未知不得盲重试，不能重复模型、工具或发送
- 不用删除历史、限制 observation 总数、跨轮授权缓存、扩大 busy_timeout 或隐藏异常替代修复
- 性能脚本只对隔离库运行；任何生产部署、数据库迁移或观察都另按授权范围执行

建议负责人角色为熟悉公共主 Agent 与 SQLite 合同的工程师；安全边界和恢复变更由另一位工程师复核。以下人日是实现、定向测试和复核的粗估，不包含等待 CI、生产观察、部署授权或新增需求。

## 2 第一批任务与依赖

| 任务 | 建议次序 | 工作量估计 | 主要风险 |
| --- | --- | --- | --- |
| T01 批量构建与局部集合 | 集合/batch 最先；lazy 独立审查 | 1.5–3 人日 | 顺序、epoch 选择与重复 ID 语义 |
| T02A 请求内估算与渲染复用 | T01 后 | 1–2 人日 | soft/hard 容量口径与分组边界 |
| T02B 短生命周期资料批读 | T02A 后，可单独延后 | 2–4 人日 | 快照一致性与权限重新核验 |
| T03 阶段计时与诊断解耦 | 计时先行；编码与异常语义单独提交 | 3–5 人日 | 隐私冻结、有界内存与一次性发布 |
| T04 Compaction 匹配索引 | 可与 T01/T03 并行 | 0.5–1.5 人日 | 迁移兼容、候选语义 |
| T05 冷入口连接生命周期 | 基础计时就绪后 | 1–2.5 人日 | owner/connection 在探测中变化 |

T01 的局部集合/batch、T03A 计时与 T04 索引先建立最小结果；T01 lazy 单独作语义审查，T03B 的推荐职责调整须与模块合同及测试一起实施。T02B 和 T05 按自然流量热点选择，不必等待所有后续重构才能交付。每个提交必须记录原始版本、修改点、已跑测试与尚未覆盖场景，不能将彼此重叠的 suite 次数相加。

## 3 T01 批量 Observation 与单次冻结 ID 集

### 目标和依据

普通五轮集成已证实 observation 累计与 old/fresh 重建可达。当前逐项追加约 O(K·F + K²)，逐历史行重建事件集合约 O(R·E)。先改纯内存结构处理，不改数据库、授权或投影格式。

修改位置：[history_projection.py L224–326](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/history_projection.py#L224-L326)、[frozen_fragments.py L21–143](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/conversation/frozen_fragments.py#L21-L143)。

### 实施步骤

1. 在一次 immutable frozen 候选的 ledger 分页循环前绑定 `frozen_event_ids = frozen.event_ids`；所有该候选 membership 过滤复用此集合
2. 增加具体的批量附加方法或临时 builder：一次建立已存在 observation ID 集，按输入顺序添加首次出现的新 ID，最终一次 `FrozenFragments.load`/验证/深拷贝
3. 先以 batch 替换逐项 loop，完成输出等价回归；不要同时调整重复 ID/version 冲突策略
4. lazy fresh 作为第三个独立提交评审：只有明确允许“未选候选不验证”，才能在 extended hard 不 fit，或 soft 不 fit 且有可采用 ready rollup 时才构造 fresh；保留原软/硬决策与显式 epoch 切换
5. 在该语义决定和测试完成前，保留无条件 fresh 的现有校验，先交付局部集合和 batch。实际采用的候选及 publication CAS 始终完整核验；不得捕获 `ProjectionConflict` 后退回旧前缀，吞掉来源、隐私或协议错误
6. 记录 F/E/K、候选建立次数、批量 load/copy 次数；不要引入跨轮全局缓存或新持久状态

### 必须保持

既有 observation identity-first no-op、eventless note 与 snapshot-tagged event 顺序、分组 event overlap、完整 current envelope、summary/source ownership、actor/read grant、最后 publication CAS。不得通过 LIMIT K 或按年龄丢观察达到指标。

### 现有回归与新增用例

现有重点：`test_projection_selection_delta.py`、`test_projection_capacity_io.py`、`test_context_observation_sources.py`、`test_foreground_rollup_adoption.py`、`test_foreground_rollup_nonblocking.py`，以及真实 wire 的 `test_work_reporting_runner_wire.py`、`test_work_reporting_runner_gemini_wire.py`。

新增必须覆盖：

- 0/1/32/256/512 observation，已有 ID、同批重复新 ID、同 ID 不同 version，eventless 和 tagged 两种形状
- 连续分组、重叠 event IDs、mixed actor/read scope、普通无 Work 多轮累计
- hard/soft 都 fit、仅 soft 超限无 ready rollup、仅 soft 超限有 ready rollup、真正 hard 超容量
- bootstrap、显式 rebase、restart、source/privacy/reset 在 prepare→publish 间变化，失败 CAS 不推进 selection
- 正常/超容量/ready_rollup 三条路径比较实际序列化 wire/messages、tools/native tools、固定设置，而非只比较 hash
- 未使用 fresh 的 individual coverage 缺失、images/reasoning 重建项；必须采用 fresh 时同类非法输入仍拒绝。lazy 未获语义批准时保持旧拒绝行为
- 输入 items/dict/list 在调用后改变不能污染冻结结果；fresh 与 extended items 相同但 summary/coverage 不同，先锁定旧行为，不顺手改变选择语义

### 验收与回滚

确定性门槛：稳定候选集合只构造一次；新增 K 项不再逐项复制整个前缀；batch 保留原选择与 identity-first no-op 行为，包括已存在 ID 的 version 差异。输出、reason 和 epoch 与合法原输入一致。只有 lazy 独立语义审查通过后，才增加“不采用 fresh 时不构造”的门槛；它不阻塞集合/batch 的交付。

本次内存候选与原版各通过同样 43 个既有 node；部分 node 只检查 repository，这不是 43 项专门等价证明。hard-fit tagged synthetic 基准的输出等价也不能替代上述最终补测。

回滚为撤回纯内存变更，不涉及迁移或清数据。发现 wire/order/source 或取消语义变化时停止灰度，保留既有 journal 和数据库。

## 4 T02A 请求内 Token 与渲染结果复用

### 目标和代码位置

当前 composition 每轮 6 次完整 estimator，常见不换 epoch 后 4 次内容相同；同一 snapshot 在 uncovered/final/bounded 视图里重复 render。

位置：[main_agent_turns.py L239–316](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/main_agent_turns.py#L239-L316)、[history_projection.py L284–316](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/history_projection.py#L284-L316)、[context_assembler.py L1865–2049](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/context_assembler.py#L1865-L2049)、[capacity.py](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/model_runtime/capacity.py#L70-L160)。

### 实施步骤

1. 在当前 prepare 闭包中，对同一完整候选请求按现有 estimator 口径估算一次，再分别比较 soft/hard 预算；这是估算值，不是 Provider 精确 token 用量
2. 只在候选内容、工具/原生工具、模型请求设置或 opaque state 变化时重新估算
3. 同次 immutable history 中复用 `_UncoveredPromptView`、rendered output 与已知 token；需要 individual view 时复用同 renderer 的逐行结果
4. 首批只做当前闭包内结果复用，不新增持久 cache/key 系统。保留 fixed-prefix、maintenance_budget 与两个阈值的现有含义；不能把 schema 与消息估算简单相加而未证明整体序列化口径等价
5. 保留无 `context_hard_fits` callback 调用者的既有 fallback；Runner 最后完整请求容量检查继续保留，先不重写跨页分组计量

### 测试与完成条件

现有：`test_projection_capacity_io.py`、`test_foreground_rollup_nonblocking.py`、`test_foreground_rollup_adoption.py`、`test_rollup_chat_wakeup_wire.py` 及完整 wire 组。

新增：软硬阈值恰好相等及 ±1、媒体/opaque/native tools/工具 schema 变化、同数量不同内容、相同消息但 summary/current envelope 不同、Provider profile 变更、无 hard callback 的 fallback、两个同 sender 页边界、时间跨度与日界、回复引用与 external cause。确认估算调用无依赖重复执行的副作用；完整请求不被误判可容纳，不改变回退或辅助请求预算。

完成条件：相同候选 soft/hard 只估算一次；不重复 render 同一个未变视图；序列化请求与旧版相同。墙钟报告同 fixture 多次分布，不用一次“更快”判定。

风险为错误复用造成容量低估；回滚复用逻辑即可，不改数据库。

## 5 T02B Generation 与资料读取收敛

### 目标和修改范围

小规模 fixture 的 generation status 为 6 SELECT、snapshot 为 7、aliases 为 3；current_time 9 SELECT 且同轮两次。不是全部查询可删除，目标是合并同一准备快照中的重复 canonical 解析。

位置：[ContextAssembler](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/context_assembler.py#L677-L945)、[snapshot 读取](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/context_assembler.py#L1708-L1753)、[Runner setup 前时间读取](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/chat.py#L1694-L1722)、[事件绑定资料](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/context_assembler.py#L1277-L1345)。

### 实施步骤

1. 设计单次准备的最小 immutable metadata DTO，仅放本次需要的 alias、timezone 及已有 canonical owner/read_version 等真实依赖；不含 ORM/session，不作为授权，不新增统一 metadata revision
2. 将 expected turn 校验收敛到 `load_prompt_snapshot`，在显式一致读快照中读历史正文前核验 scope id、generation、transport/runtime key 全部绑定，不能只比较一个 generation 整数。普通 AsyncSession 本身不等于已建立 SQLite 一致快照
3. 先合并 canonical person/alias/timezone 的重复读，不要把原必要创建或变更伪装只读
4. timezone 本次复用，当前时刻从 clock 重新获取；跨轮时区修改仍生效
5. 可选多 @ 批读保留同 Person 多 Binding 折叠、SELF/external 排除、真实群成员和歧义拒绝
6. 保留 prepare 后 source/read-version 复核与真正 dispatch CAS；不以 `asyncio.gather` 全部调用替代读模型设计

### 验收与回滚

现有：`test_chat_context_preparation_gate.py`、`test_context_observation_sources.py`、`test_external_event_runtime_fences.py`、`test_plugin_result_access.py` 和普通聊天组。

新增：真正 source/privacy/generation/read grant 变化发生在 prepare 与 dispatch 之间时，既有围栏仍拒绝；alias/昵称/时区等普通展示值变化按现有冻结规则处理，不统一触发重试或新增版本协议。另测完整 scope/transport/runtime key 错配、当前时刻跨日/时区变更、多 alias/多 Binding/无效 targets；actorless、SELF、插件窄 read grant 与普通人物不能混用 DTO。

记录每个独立阶段的 SELECT 数，assembler/composition 总计与子项不相加；不能把 205/216 个全轮 SELECT 当成全部待删目标。要求实际减少已标明的重复读，最后安全围栏照常拒绝变化。回滚 DTO/查询路由，不恢复旧状态或放宽权限。

## 6 T03 阶段计时与可丢诊断解耦

### 目标和依据

有界 DiagnosticWriter 只异步化最终写入；TraceRecorder 在调用端仍 await 编码和来源读，且 Provider 名额持有到这些步骤完成。现 invocation 成功/失败口径不一致，SELECT 与连接池等待也缺测。

位置：[Recorder L191–324](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/execution_trace/recorder.py#L191-L324)、[Executor L631–985](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/model_runtime/executor.py#L631-L985)、[JSONHTTP L93–175](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/llm/json_http.py#L93-L175)、[AgentRunner guard](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/services/agent_runner.py#L1042-L1119)、[SQLite diagnostics](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/persistence/sqlite_diagnostics.py#L69-L218)。

### T03A 先统一时钟和阶段

1. 明确 logical call、physical attempt、Provider slot 等待、dispatch prepare、payload/trace prepare、transport、response prepare 的起止点
2. 对现 `latency_seconds` 选择向后兼容或版本化方式，统一 success/failure/cancel 的口径；旧记录缺字段标 unknown，不伪造历史分段
3. 为 conversation lock 增加 request/acquire，不能继续只从 chat_processing 起点看等待
4. 在应用 acquisition 开始前计时，区分 checkout/pre_ping 与首 SQL；checkout 事件本身无法恢复排队起点
5. 只读 cursor 增加固定操作类计数/耗时，禁止正文与 SQL 参数；承认其中仍混有驱动调度，不能命名为精确 SQLite 内部锁等待
6. 同 turn 增加 N/F/E/K、字节、estimate/candidate 次数及 protocol lock；保留物理 commit/rollback 边界。单调时钟只计进程内区间，跨重启 age 与持久定位继续使用原 UTC/epoch 时间；attempt 等待、retry backoff、slot hold 与业务 queue age 分开，嵌套段不相加

### T03B 推荐把可丢准备移入现有有界消费者

原审计基线的 [execution-trace](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/docs/architecture/execution-trace.md#L47-L60) 把逐条来源核验固定在生产者，这是可调整的模块分工。核心不变量是冻结原身份与隐私代次、只核验原来源、不回填已删除内容，以及不让可丢诊断改变业务成功。原推荐方案为复用同一个有界 DiagnosticWriter 完成冻结 ID 核验、编码和条件提交；PR241 已修改模块合同并交付该实现，后续按现行 execution-trace 合同核验。

1. 调用端从可信上下文冻结原 source/turn/work/activation/execution、conversation/event ID、generation、原 privacy generation、事件时间及有界独立 payload；不携带 live ContextVar/session/control，不用 `create_task` 包住旧 append 后才冻结
2. 复用已有单消费者和进程内队列，不新增永久表、第二诊断层或恢复器。消费者只核验被冻结 ID 的类型、存在、归属及原投递回执/出站事件依赖，只能接受或丢样；不能从当前上下文推导、补填新 owner，或消费时重新捕获 privacy generation
3. 来源/投递只读 session 结束后，才进入原短写事务和 privacy-generation 条件 INSERT。冻结后来源改属/删除或隐私代次不匹配时丢弃旧样本并计缺口，新轮正常。核验顺序与编码位置以少做可丢工作为准，不跳过核验
4. 原始准入上限在保留大 payload 前生效，覆盖独立副本、排队项、active item、编码输出和临时峰值；不能沿用压缩体大小或固定估计声称原始内存有界。必要的有界快照 CPU 仍计量，不宣称调用端零成本
5. 队列已满或输入超限直接 omitted/drop，明确字节/hash 未知口径；不得先完整 deepcopy/JSON/hash 巨大正文再丢弃。消费者核验、编码或写入失败只影响样本与缺口，不重试业务请求或未知诊断提交
6. 模型响应返回和 Provider 名额释放不等待可丢编码、关联 SELECT 或诊断 writer。实际 dispatch/授权、预算、request checkpoint、journal、强制审计与真实 Social/Work 回执仍在原业务同步边界，不能混入可丢队列
7. 不把 Provider slot 缩成仅 HTTP post 作为捷径，保持重试、前后台容量和调用生命周期。dispatch guard 的 prepared no-op 另按原边界审查；如新增最后时刻业务只读复核，必须与一次性 reserve/publish 分离，不重复计费或 call ID
8. 关闭先停生产者，再按既有队列政策排空/丢样；编码线程的真实收尾和引用释放须管理并测试。取消 `asyncio.to_thread` 的 await 不等于线程退出，不以 close 返回或计数归零伪称物理编码已经停止
9. 补丁同步改写 execution-trace 的生产/消费职责，以及 [main-agent-runtime 的诊断失败例外](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/docs/architecture/main-agent-runtime.md#L499-L527)，将“新目标合同”与“当前实现尚未达到”分清；旧测试随新语义调整，不能只改文字宣布达标

若需分阶段交付，可先移编码、保留生产者核验作为过渡；此阶段仍有诊断连接等待，必须明确未完成 T03B 全目标。模块职责改写和冻结 ID/隐私/取消验收完成后，才交付完整方案。

### T03C 成功响应后的可丢诊断错误不能丢弃业务结果

当前 [executor.py L728–750](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/model_runtime/executor.py#L728-L750) 在 Provider 已成功、telemetry 抛非 SQLAlchemy 异常时仍会再次抛出；[现有测试 L468–503](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/tests/unit/test_model_telemetry_failures.py#L468-L503) 明确锁定了 CanonicalIdentityError/TypeError 冒泡的旧行为。本次定向测试及随后两分支重跑仅确认该行为，生产触发频率未知。

最小修改是区分实际 dispatch/授权失败与成功之后的可丢 telemetry 失败：前者仍按原合同阻断；后者记固定类别和缺样，不丢成功响应、不重试原模型。保留原 Provider 失败不被诊断错误覆盖的语义，不捕获 CancelledError/SystemExit，也不改变真实预算/journal 的异常处理。同步删除 main-agent-runtime 中“成功响应后身份/编程诊断异常仍抛出”的例外，并改写锁定旧行为的测试；此项不需要新架构。

### 现有回归与新增故障矩阵

现有：`test_chat_preparation_timings.py`、`test_model_telemetry_failures.py`、`test_sqlite_phase_timings.py`、`test_sqlite_physical_exit.py`、`test_work_protocol_latency.py`；审计脚本 `runtime-evidence/test_runtime_audit.py` 和 DB SELECT 盲区检查。

新增必须独立阻塞：编码默认线程池、诊断来源/投递关联读取、writer、关闭线程。在推荐方案及同步模块合同变更上，断言可丢编码、关联 SELECT 或 writer 受阻不延长 HTTP 成功后的业务返回、名额释放及下一 HTTP 派发；不能靠绕过实际业务授权达成。仅移编码的过渡阶段仍单列来源等待，不标为完整达标。

另覆盖：privacy erasure 在排队中、冻结 ID 删除/改属、新 owner 不被接受、队列满、压缩失败、commit 结果未知。调用后原 dict/list 改变不得污染记录；多生产者的大字符串、深嵌套、媒体大块、不可压缩或原始巨大但压缩很小的载荷均须满足真实容量边界，队列已满时不得先拷贝大正文。

编码线程 barrier 下取消/shutdown 必须验证真实线程完成、引用/字节生命周期和无迟到写入，不把取消协程当物理退出。排队旧 payload 在隐私删除后不能落库，新轮正常。

成功 + TypeError、成功 + diagnostic source gone/kind mismatch、Provider failure + 同诊断异常以及 cancellation 分别断言：单次请求不重跑，成功内容保留，原失败仍为原异常，取消继续传播。诊断缺失不能使工具或发送被重试。

计时矩阵：N=1 排队、guard barrier、0/1/2 重试、HTTP 错误、本地容量拒绝、native 工具不确定、取消。非重叠 phase 与 wall 在合理测量误差内一致；嵌套段另标，不相加。

### 验收与回滚

重跑注入 120 ms 编码等待的诊断实验，并新增慢来源/投递关联 SELECT 实验；推荐完整方案不再因这些可丢准备延长第二请求派发。原业务权限/回执检查不计作可丢准备，取消与进程终止继续传播。队列与 active 编码的完整资源生命周期有界、丢样可见，真实线程完成与敏感引用释放可验证；不设置虚构生产绝对门槛。

计时、队列编码与成功后异常语义分别提交，便于独立回退；来源职责变化另附模块合同修订。回退不动业务 journal/回执，不把诊断失败扩成模型/效果重试。任何隐私、丢成功结果或重复效果回归为停止条件。

## 7 T04 Evidence Compaction 匹配索引

### 目标和代码

相关 NOT EXISTS 用 fact_id、evidence_before、status；现有索引均以 run_id 开头。完整 ORM SQL 的相关子查询扫描历史 items。

位置：[候选查询](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/memory/evidence_compaction.py#L298-L372)、[ORM 索引](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/memory/dream/db_models.py#L301-L321)，迁移放当前链的下一个可用版本；不修改已冻结历史迁移。

### 实施步骤

1. 选择 `(fact_id, evidence_before, status)` 完整复合索引作为最小方案；如比较 terminal partial index，记录对 FK/非终态覆盖的差异
2. 新增独立 additive migration，并同步测试 schema 的 ORM Index
3. 迁移预先检查 sqlite_master 同名 table/view/index、真实表、列序及 expression、unique、origin、partial、ASC/DESC、collation；参考现 0092 的完整形状检查，异常在首次 DDL 前明确拒绝
4. 保留原唯一 `(run_id,fact_id)`、外键、status 过滤、CAS 和删除语义；downgrade 只删除本次拥有的索引
5. 对实际 ORM 生成的完整查询做 EXPLAIN 与候选输出对照，不只测试手写简化 SQL

### 测试和验收

现有：`test_evidence_compaction_idle.py` 与 Memory/Dream compaction 相关回归；补独立迁移升降级文件。复现脚本：`db-evidence/compaction_query_benchmark.py`。

新增：terminal completed/skipped/failed 与 pending/processing，evidence_count 改变，零候选、旧 run/item 恢复和删除级联；同名 table/view、错表、unique/origin/partial 差异、DESC、NOCASE、expression，以及多个索引预检失败时零 DDL。保留原 unique。明确重复 upgrade 的既有兼容规则；downgrade 完整核验后只撤自有索引，索引已缺失时明确拒绝，不要求重复 downgrade 成功，更不得误删其他对象。

确定性门槛：相关子查询由 SCAN 变为预期覆盖 SEARCH；完整候选 ID 和顺序相同，原事实不改。记录 187/2000/20000 item 下 VM 量与多次时长，不能把 20000 的合成收益套用旧生产 187 items。

全量 evidence/source 聚合和排序仍存在，索引后报告剩余成本。部署需另行授权；回滚撤本索引即可，不恢复旧库覆盖新事实。对大真实库建索引的锁/I/O 时间应在隔离副本评估，不假定在线零影响。

## 8 T05 冷入口释放连接后再探测

### 目标和代码

只处理 fast path 未命中的冷 pin、失效 pin 或其他 Presence 恢复；保留 #240 同 session 热路径。

位置：[ingress.py L103–182](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/identity/ingress.py#L103-L182)、[routing.py L996–1022](https://github.com/YuanYeYouTao/Yuki/blob/a48b8d1387c62c2bcafda0e4faca056619311079/src/qq_ai_bot/identity/routing.py#L996-L1022)。

### 实施步骤

1. 将入站解析分成同 session 热判定与冷恢复计划，计划保存原 authenticated connection 和必要 route/owner 版本
2. 退出 outer read session 后执行远端 probe 和已有路由恢复 CAS，probe 使用有界 deadline
3. 完成后新开短 session，重新确认原连接、presence、owner、pin、space binding 仍合法；否则拒绝或按原恢复语义返回
4. probe/准备阶段取消不进入路由 CAS；一旦进入提交，按原持久事实确认结果。提交确认未知不能说成未安装，不盲重试或反向删除路由；不新增长期恢复计划或路由 receipt
5. 合法接纳后的账本写入仍核验持久 pin/Presence/owner；仅原 WebSocket 随后断开不撤销已经收到的消息。路由暂停或改绑仍拒绝，不把每次入账都要求原连接在线作为新条件
6. 增加 cold/warm 标签和 probe/checkout 分段，不以长期缓存替代网关 reachability，禁止以增大 pool 或 timeout 当作修复

### 验收和回滚

现有：`test_ingress_membership_latency.py`、`test_group_metadata_contention.py`、身份/routing 相关回归；审计中的两个 cold pool/probe 检查。

新增：pool_size=1、overflow=0 下冷恢复可完成或明确权限拒绝；probe barrier 期间没有 outer DB session。既存健康 pin 的路由判定保持一次 checkout、零额外 probe/DML；完整 pre_admit 的新 Person/Conversation 合法创建不受“零 DML”限制。

分三阶段测试：probe 中 owner/presence/connection/pin 变动、重连、模糊候选或取消；路由提交中取消与提交确认未知；已合法接纳后仅 WebSocket 断开。前两者按原 CAS/持久事实处理，最后一种不撤销消息，后续仍受持久路由暂停/改绑围栏。shutdown 不得导致盲重试、反向删除或错误身份接纳。

回滚仅恢复冷分支，热路径保留；不回填或重写历史入站身份。发现错误 owner/pin 接纳即停止。

## 9 后续任务的进入条件

### T06 无效 reflection 回填提前只读返回

工作量约 0.5–1 人日。`evidence_compaction.py:143–163` 在 prepared 阶段过滤所有非唯一/缺失 expected_runs；无可写项直接返回，保留留下项的 writer 内复核。新增模糊/缺失零 BEGIN IMMEDIATE、唯一→模糊竞争拒绝与唯一合法回填。回滚纯控制流，不能删 receipt 或伪造映射。此项小而明确，但生产出现频率未知。

### T07 Projection 准备复用与存储重构

先做 1–2 人日的准备复用：把已经准备的 items 传给 prefix 检查，复用旧 projection 的不可变副本及版本，删除多余 JSON loads/dumps。保持当前实现所需的冻结前缀校验和最终实际 wire 顺序；不把内部 JSON 存储键顺序升格为新的永久业务不变量。

只有自然数据确认完整 UPDATE/WAL 写放大是显著瓶颈，才进入约 5–10 人日以上的 manifest/chunk 方案设计与实现。须保留 exact wire、epoch/revision/source/privacy、journal/selection 同事务、每份 summary parent artifact ownership、容量回收、崩溃恢复和兼容迁移。迁移回滚不得丢新消息，若无法兼容必须先做明确设计。不要将 JSON 编码错误地描述为现 writer 内工作。

现有回归：`test_projection_selection_delta.py`、`test_projection_capacity_io.py`、`test_context_observation_sources.py`、`test_work_protocol_latency.py` 与完整 wire 组。新增绑定字节、WAL 页数、append 顺序、半提交/恢复、旧新版本读取与 summary parent 转移竞争。

### T08 跨 Conversation 的有界 Work 恢复

进入条件是自然 queued→resume 等待显著且跨会话队头阻塞成立；先把候选读取前 repair/wake/reclaim/ProtocolStore.cleanup 的维护时间与前一个完整激活时间分开，不能把全部 queue age 归给串行 resumer。约 2–4 人日。在现 scheduler 内使用有界激活集合，原 Conversation 去重和 lease CAS 保留；只领可执行数量，释放/关闭必须 cancel/join，不能建立第二恢复状态机。

测试：慢 A 不阻止独立 B；同 Conversation 单有效租约；错误 resumer、lease 过期、输入 steer、取消、shutdown 不漏名额；wait loop 独立；N=1 前台优先，N>1 后台不超过原保留策略。复用 `test_runtime_scheduler_boundary.py`、`test_yuki_runtime.py` 和审计 resumer barrier。回滚 scheduler 并发，不清 Work/journal。

### T09 普通过期纯生成轮的受控抢占

原审计将此列为待确认的产品策略。后续私聊锁等待证据和用户明确授权已满足进入条件，PR242 已实现普通 private、无已接纳 Work、无开始或未知副作用、无 native dispatch 的原 task 精确取消与真实 join；其他情况继续原因果/回执合同。只读记忆查询后的未发送模型请求也可被新普通私聊抢占，不按工具名称扩大效果权限。

测试连续三条、同群不同 Person、dispatch 临界点、部分发送、native 工具未知、Work 已承诺任务、/stop、/new、privacy/shutdown。原 HTTP 费用/用量未知不记零，不重新发旧效果。当前实现没有独立功能开关；回退需撤回对应策略源码，不清除 journal、预算或发送回执。此次实施源于后续用户授权，不能推导成其他会话或发送后的任意取消。

### T10 更深读分页与 Protocol 锁粒度

Compaction 索引后仍需全 evidence/source 聚合；后续约 2–4 人日评估 fact ID 有界页、固定 high-water/巡回游标，再对本页 fact 读取完整来源。不得 LIMIT evidence 截断计数，必须保证公平性、持续新增和旧 item 恢复。

Protocol publication 先量化等待再决定更窄对象锁。约 3–5 人日以上，需设计安全锁序和 GC 屏障；文件验证→ref/journal 提交保持防删除保护。覆盖 publish-vs-GC、损坏/缺失/替换文件、取消线程 join、commit 确认丢失。不能直接删除全局锁。

### T11 可选 Automation 准备复用

低优先，只有显式资料预取/重入是热点才进入，约 2–4 人日。默认 automation 无该预取成本。先定位原 invocation/source，再决定是否运行昂贵准备；保留 creator/SELF 授权、run/step、预算、效果与 return-to-caller。必须补真正持久 dispatched-journal 重入，不把现有两个 synthetic 分支探针当作恢复缺陷证明。

## 10 可重复基线与验证命令

以下命令在隔离审计副本运行；不修改产品代码，也不指向生产库。环境应为 Python 3.12.14 与固定 `uv.lock`。完整依赖和材料布局见证据包 `evidence/REPRODUCE.md`。

```sh
AUDIT_DIR=/workspace/shared/yuki-latency-audit-20261004
cd "$AUDIT_DIR/repo"
git rev-parse HEAD
git status --short
sha256sum uv.lock
../.venv/bin/python --version
```

预期 HEAD 为本任务书完整 SHA，uv.lock 哈希为 `b1e2b7469099fe84d3c2dad56850c75c28575774e211ec86963dc1a993718b16`。先解决任何版本差异，不能对另一提交套用本报告行号和结果。

### 10.1 运行时既有回归

```sh
../.venv/bin/python -m pytest -q \
  tests/unit/test_ingress_membership_latency.py \
  tests/unit/test_rollup_scheduling.py \
  tests/unit/test_chat_preparation_timings.py \
  tests/unit/test_model_telemetry_failures.py \
  tests/unit/test_commands_and_chat.py \
  tests/unit/test_social_concurrency.py
```

审计基线已完成 109 passed。修改后的结果必须重新记录，不能沿用本次数值。

### 10.2 数据库既有回归

```sh
../.venv/bin/python -m pytest -q \
  tests/unit/test_work_source_guard_readonly.py \
  tests/unit/test_ingress_membership_latency.py \
  tests/unit/test_evidence_compaction_idle.py \
  tests/unit/test_short_state_readonly.py \
  tests/unit/test_projection_selection_delta.py \
  tests/unit/test_work_protocol_latency.py \
  tests/unit/test_sqlite_phase_timings.py \
  tests/unit/test_sqlite_physical_exit.py
```

审计基线已完成 77 passed；与其他组合重叠，不能累加。

### 10.3 新增隔离复现与基准

```sh
PYTHONPATH="$PWD/src:$PWD" ../.venv/bin/python -m pytest -q -c pyproject.toml \
  ../context-evidence/test_context_integration.py
PYTHONPATH="$PWD/src:$PWD" ../.venv/bin/python -m pytest -q -c pyproject.toml \
  ../runtime-evidence/test_runtime_audit.py
PYTHONPATH="$PWD/src:$PWD" ../.venv/bin/python -m pytest -q -c pyproject.toml \
  ../db-evidence/test_db_audit.py
PYTHONPATH="$PWD/src:$PWD" ../.venv/bin/python -m pytest -q -c pyproject.toml \
  ../review-evidence/test_entrypoint_coverage.py
../.venv/bin/python ../context-evidence/context_bench.py
PYTHONPATH="$PWD/src:$PWD" ../.venv/bin/python ../runtime-evidence/benchmark_sync_stages.py
../.venv/bin/python ../db-evidence/compaction_query_benchmark.py
```

这些脚本部分写入固定的审计结果路径。重跑前保留原证据或使用独立审计目录，避免覆盖基线结果；它们不会替代最终产品改动的测试。算法原型脚本不是 pytest test 数，运行相同 node 的原版/候选对照也不能累加。

审计执行记录为上下文集成 1 项、运行时新增 6 项、DB 新增前 6 项与后补 1 项分别执行、automation 2 项。上述合并复现命令是交接时可运行的入口，不宣称本次曾以一个总 suite 运行。

### 10.4 最终修改后的门槛

在拟提交的最终代码上按实际改动风险选择：受影响 unit/真实 SQLite、实际 wire、来源/隐私/lease/CAS/commit-unknown、取消/重启/GC，以及仓库要求的静态检查和 CI。记录每项选择依据；已经通过且未受影响的检查不机械重跑，无关全量检查不作为本次文档复核任务。索引任务按迁移风险验证 fresh install、相关升降级链及错误同名对象，不把定向结果冒充完整迁移验收。

既有 43 项内存候选对照只作为方向证据。新增等价与失败用例未全部完成前，不能标记 T01 可上线。相同规则适用于其他任务：本次审计通过不等于拟议补丁通过。

## 11 自然流量验收与交接清单

后续获准部署时，先核对实际 commit、schema、profile、并发与诊断 queue health，使用自然消息，不创建第二个 active Bot 或主动灌真实模型/QQ 流量。

按普通新轮/被取代轮/Work 续跑、文本/媒体、冷/热入口、模型次数分组。每组同时报告样本数、取消、仍在进行与缺样；分别展示 ingress→首模型派发、首确认发送和整轮。不能只挑成功快轮，不能从总耗时减去混口径的模型时间后称为数据库等待。

交付每一项时附以下记录：

- 精确实现 SHA、关联任务号、具体改变及保留合同
- 原/新确定性计数、输出等价结果、完整测试命令与 pass/fail/skip
- 单独标明合成/自然、机器/负载/样本数，不报无证据的稳定 p95 或固定收益
- 已知风险、未覆盖项、回滚步骤和是否涉及 schema
- 若已部署，实际部署 SHA/schema 和自然观察结果；缺样保持“待速度验收”

所有任务共同停止条件：出现错误身份/授权、冻结前缀变化、丢失已承诺 Work、重复模型或外部效果、隐私内容在延后诊断中泄漏、无法判定提交结果而自动重放。此时保持原持久事实，停止扩大灰度并按该任务的回滚方案处理。

最终诊断定向集合 131 项通过；最后 invocation 拒绝可见性与确定性 slot-release 断言修订后，相关 36 项通过，覆盖重叠不累加。120 ms 离线来源/编码故障注入验证 Provider 返回不等待消费者；这不模拟生产的 41 秒长尾，也不构成线上速度验收。
