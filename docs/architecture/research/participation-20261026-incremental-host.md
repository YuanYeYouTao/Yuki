# 参与循环：轻推进与按事实增量准备

日期：2026-10-26。状态：研究，未改业务代码、配置或生产。Host 基线 `75e21a5c824876485d75e7fcd58e8111bdddeb4d`；独立库固定 `79d266918dc6ce19b7d81568565deaed450301d5`，以下库源码均从该提交读取。

本文遵守[开发合同](../development-contract.md)、[语义参与合同](../semantic-participation.md)和[连续概率模型](../autonomous-participation-model.md)。本轮选择保留原概率、采样频率、`sample_sequence` 哈希流和冷恢复参数语义；不采用 10 月 25 日研究中为减少保存而更换随机键、时间锚恢复口径的建议，不以空闲零写盘为验收。

## 1. 可行性与真实边界

可行的改动是把一次 tick 分成两类工作：

- 每个原 tick 仍推进现有 Controller、连续衰减、资格与到期、原随机流和必要持久化。关闭、Jev 不可用、没有候选也保留原 `advance` 分支与 `last_sample_at` 处理。
- 历史水合、来源投影、回执分组、配置完整装配和身份准备，仅在其实际依赖变化或原期限到达时重做。轻量读取当前事实，不能变成每两秒重新构建全部重对象。

不能直接把 `_dirty` 当完整依据。`semantic_participation.py:358–369` 目前只接收入站消息提示；撤回、ASR/图片补全、普通 admission、Main 自报、Social 回执、人物合并、群开关及 generation 都不是同一种新消息。现有系统也没有覆盖这些事实的完整细粒度失效通知。

因此第一版采用原两秒 loop 内的有界领域事实比较，加已有入口的局部失效提示。提示只加速，漏提示不决定事实。没有可靠版本的领域先比较必要原字段，不能用一个全局数据库 revision 或最后消息号冒充完整依赖，也不另建事件日志、后台队列、调度框架或持久 dirty 表。

保留原 `sleep(2)` 与实际时钟语义，不因为收到 hint 多调用一次 `advance`，不合并多轮随机机会，也不把多个 tick 的末端 rate 乘大 dt。整体计算缩短会改变实际墙钟时刻；验收按相同输入时刻序列比较原概率与随机状态，而非把不同调度耗时下的随机输出硬说成同一实验。

## 2. 当前重复工作的直接证据

`semantic_participation.py:1316–1378` 每轮保留所有 session，并发二；`1380–1505` 每个 scope 都执行：

1. `_scene`、Social 近窗与首次 sync。
2. `_hydrate`：近 64 条历史、身份、Work 与 ordinary admission、逐条投影。
3. 相同 Social 页第二次绑定；核验边界；配置与 binding。
4. 先准备身份，再调用 `ObservationSession.evaluate_due`。
5. 再核场景和配置、准备身份、核候选来源，处理 pending、advance、save。

固定库 `session.py:179–192` 在进入 `evaluate_due` 后，先做队列失效清理、quiet review 和 checkpoint，才检查 `now < retry_after`；所以 Jev 明确处在退避中，Host 仍已做完前面的大量准备。把整个函数跳过会漏清理，正确改法是把“便宜的是否需要观测准备”提前，与非观测推进分开。

本地小型计数使用真实 `_advance_scene`，把数据库、Controller 与 observer 边界替为计数 stub，无事件、无候选且 `retry_after` 为极未来。三次调用得到：

| 调用 | 三个 tick 次数 |
| --- | ---: |
| `_scene` | 6 |
| Social 近窗读取 / 两次 sync | 3 / 6 |
| `_hydrate` | 3 |
| `_validate_boundaries` / `_binding` | 6 / 6 |
| `prepare_actor_identity` | 6 |
| `evaluate_due` / 完整 runtime snapshot | 3 / 3 |
| `Controller.advance` / `_save` | 3 / 3 |

这是 Host 调用数量，不是实际 SQL 数、CPU 比例或线上性能数据；没有调用模型或真实回执。目标不是消掉最后一行，而是让前面的准备随真实变化发生。

## 3. 数据与失效由谁拥有

复用数据只属于原 `_Session(conversation_id,generation)`。不复制 Controller、typed checkpoint、整份 JSON 或身份授权状态来做第二个长期真相。按组件持有已有准备结果及其必要依赖；失效后由原准备函数重建。相同内容的多条 hint 可合并，已提交结果认原账本。

| 实际依赖/触发 | 现有 owner / 入口 | 可复用什么 | 必须失效或保持的边界 |
| --- | --- | --- | --- |
| 新输入、直接入口标记 | `processor.py:925` → `observe_context` | 同一历史页中未变事件的投影 | 直接标记不伪造事件；后到输入不得被旧批次清空 |
| 新事件、正文修订、撤回、音视频补全、删除、reset | Event ledger；`ConversationReadVersion` 的 generation/floor/prompt_source_revision | 未变时不重读 64 条全文和重复 `_event` | 只认本 Conversation 的来源版本；修订不一定有更大 event ID，删除也没有新行 |
| 场景、群启停、Presence/SpaceBinding/route 变化 | `_scene:371–437`、identity/routing 领域 | 不变场景的装配数据 | 群禁用不再取在线群配置；generation 改变重建原 session，不挪用旧 proposal |
| 控制器 owner/master/external/epoch | `AutonomyRepository` 与 `_binding:542–565` | 未变 binding 的准备 | 新效果仍由当前 owner/CAS 接纳，已接纳 run 不随开关抹掉 |
| 参与相关配置 | `RuntimeConfigService.snapshot:1492`、conversation policy | 参与策略及其有效来源的窄投影 | 全局和本群 override 的新增、删除、rollback 都要发现；不因无关模型/网页配置刷新全部 scope |
| 当前人物合并与目标归一 | `participation_identity.py`、`current_person_ids` | 同一准备阶段的 actor roots 与 ID 收集 | actor/target/basis 集改变或相应 Person 归属改变时重算；不把 roots 当跨 await 的资格许可 |
| 普通真人 admission、continuation 归属 | `ordinary_admission.py`；`on_ordinary_admitted` | 原已提交 admission 的 unit 投影 | hint 不是 commit；当前页事件相同也可能刚完成 admission；冷恢复仍查原持久行 |
| Main 局部 self report/quiet/stay | `participation_ordinary.py:525–608` | 未变单元的派生准备 | 仅原 unit/来源生效；quiet 不广播全群；后到自报不恢复已撤销来源 |
| 真实发送、caption、event FK、普通表达锚 | Social receipt；`participation_feedback.py:197–337` | 逻辑效果分组及原绑定准备 | status/event_id/presence/原 parent proof 改变均失效；父 receipt 在窗口外也可能被 caption 引用 |
| 自主 Work、模型计数、子任务、Memory tool receipt、持久 feedback | 原 Work/Autonomy/Memory owners；`reconcile_page:772`、`_read_facts:405` | 未变 run 的事实投影 | 不用“Work 终态”证明不会再有晚到效果；原 sequence、tree charge 和迟到真实回执照常处理 |
| 留存 event/memory SourceRefs、停止与释放依赖 | `_sources_current:878–931`、`_validate_boundaries:933–943` | 无依赖变化的纯准备 | 包含窗口外但仍被 unit/boundary 引用的源；Memory valid_from/valid_until 也是时间依赖，不新增 Memory 轮询候选 |
| 人格/数学参数 profile | `_refresh_model_parameters:175–204` | 相同原 profile 的解析结果 | 目前每 tick 只读一个原子文件；保原 hash/last-good 处理，不靠未经合同保证的 mtime 永远缓存 |

### 3.1 轻量事实比较的范围

每轮只读取仍保留 session 的必要依赖，支持原 owner 批读；读到的当前值用于判定准备是否过时。字段比较是派生缓存的证明，不是新增业务 revision。

- ledger 可复用既有 scope-local `generation/starts_after_event_id/prompt_source_revision`。后者由当前来源触发器覆盖更新与删除；不把 `last_event_id` 单独当版本。它改变后重新检查实际保留 refs 与当前近窗，未变投影可继续共享。
- scene 使用原 `_scene` 查询的真实字段，允许批取并删除同轮重复装配。不能因省查询而丢失 coalesce route、Presence、binding status 或 space.enabled 的含义。
- config owner 提供只包含参与策略所需 key 与实际 scope 解析的窄投影；同一轮全局值可共享，不为每群重建全部 RuntimeConfigSnapshot。删除通过当前集合缺项发现，不能只用 `MAX(updated_at)`。此窄投影接口需实施，不把现有 snapshot 谎称已有缓存。
- 身份 owner 批读本轮实际 actor/target 的当前归属；`current_person_ids` 本身就是一条 `coalesce(merged_into_person_id,id)` 查询。优先同轮批量与局部复用，不先发明身份版本服务。必要资格检查仍沿原原人物/当前 root 的执行处规则。
- Social 先读所需标量 proof 与 live FK，不取 `prepared_json` 等未消费大字段；字段未变则不重分组。当前记录加 `social-caption` 原 parent 的依赖闭包，保留旧查询窗口与顺序语义。不以 count 或最大更新时间证明所有行相同：同时间更新、成员替换、删除与 FK 置空都可能漏掉。
- active run 的新旧事实仍由原 `_read_facts/reconcile_page` owner 确定；缺少完整轻量变化合同的部分不提前跳过。可先省无 active 的空调用和未变事实的解析/投影；不能用通用 commit hint 推导 run 成功，也不能另建只增不删的 receipt 镜像。

有界比较也有成本，通常仍随活跃 scope/实际依赖数量增长；不能声称空闲 tick O(1) 与群数无关。收益来自少搬正文、少解析、少分组和少做大对象准备，不来自关闭必要读取。

### 3.2 提示与漏提示

已有 `Database.commit_hints` 是可丢的全局 bool（`persistence/commit_hints.py`），trace 写入也会发布，不能收到一次就把全 scope 标 dirty。若复用它，只表示本进程可能有提交，随后仍做 scoped 事实比较；它不是内容版本。队列满会断开并发 False，不能当作“没有变化”。本方案不需要再订阅一个后台消费者，原 tick 已负责比较。

已有业务入口完成提交后，可将原 conversation/generation、event/run/person 依赖映射到对应 session 的准备失效；回滚不发布已发生事实。跨进程写入、漏通知或通知丢弃仍由下一原 tick 的当前行集合/来源版本发现。没有完整比较的领域继续沿原读取，不用经验性“隔若干轮补查”来遮盖。

本轮处理开始先取走其输入提示，await 期间新提示留在下一批；不要在收尾无条件 clear 整个 scope。失败时合并未完成项，不能覆盖更晚的 direct 标记。该机制只管理原准备工作，不增加持久队列、随机序号或业务任务。

## 4. due 由原期限导出

不使用新经验冷却。Controller 自身的连续衰减、prune 与资格仍每 tick 发生；Host 的 next_due 仅指出哪些重准备/观察工作现在有必要，并不决定是否推进 Controller。

### 4.1 Jev 的 cheap readiness

由库 `ObservationQueue/ObservationSession` 提供非消费型 ready/due 投影，Host 不拷贝调度算法。现有单个 pending 的观察准备边界来自：

```text
debounce_due = min(last_dirty + 3, first_dirty + 20)
request_due = max(debounce_due, last_call + 12, retry_after)
expires_at = event.at + 75
```

取仍存在且未失效 pending 的最早可请求边界；有 in-flight 时不再开始第二个请求。首次 `last_call=-inf` 不预等 12 秒。`expires_at <= now` 先丢弃，若 request_due 已不早于 expiry，就没有合法的新观测机会，仍保留 expiry 清理边界。实际选择继续按原 priority 和时间顺序，不把“最早 due 的 pending”直接换成最终被选者。

这只是能否需要准备的廉价投影：不调用 `take` 来探测，因为 take 会推进 sequence/last_call 并冻结 snapshot。真正发请求前仍走原 queue 选择、当前来源与 actor 检查；请求一旦在途，原 snapshot 不因后到输入被改写。

retry_after 未到时跳过的是仅为新 Jev 请求服务的身份、上下文冻结及调用准备。到期/撤销清理仍推进；quiet review 依赖的单元、自报、身份或有效期变化时仍处理，不能连 `discard_unavailable` 一起跳过。清理引起的 checkpoint 变化仍按原保存语义持久化。

Jev 健康从不改变 SELF owner，不把 `external_enabled/host_available/intrinsic_allowed` 设为 false；无凭据时无新观察，但真实活动、既有反馈与 intrinsic 原推进保持。401/403、429、422 继续沿原分类和退避，不增新重试规则。

### 4.2 其他 due 与失效

- Social 近窗使用 `updated_at >= now-600s`，成员越界时需要更新该派生页；恰等于边界仍属于窗口。caption parent 保留取决于实际子引用，不能机械按 parent 的 600 秒删除。
- hydrate 的 600 秒选择和 retained SourceRef 的真实有效期不同；窗口退休、memory valid_from/valid_until、unit/boundary/support 等变化按其原 owner 处理，不把某个统一 600 秒当所有来源寿命。
- Controller、queue、receipt 或配置改变时重新导出受影响的最早期限：offer/replace/discard/retry/finish、Jev 完成/失败、晚到反馈、自报、来源修订、identity 变化、generation 变化及参数热更都可能使旧 due 失效。删除最早元素后也必须更新，不能只在新增时算一次。
- 本地状态可以线性找最早期限，先不加 heap。时间和值在原精度下比较，保留 `>`/`>=`；next_due 恰等于 now、过去或不存在均有明确含义。跨过多个期限只处理当前到期事实，不补发历史模型请求。
- 重启不恢复 dirty 或 next_due；原快照照常恢复，由当前 queue/state/原事实重新计算。保原 retry_after、last_call、sample_sequence 和冷恢复参数口径，原 admission/run 按原 ID 查询。

## 5. 一轮执行顺序与当前授权

不另起循环。原 tick 在真实处理 scope 时读取当前轻量事实，按需准备，并保留当前影响率/候选的应用次序：Social 效果应用、hydrate、原第二次关联、观测、原 post-await 核验、pending 查询、advance/save；末尾 reconcile 仍按原顺序，不借增量化把本来下一轮的效果提前到本轮 rate 里。

同一 Social 页前后两次 sync 的用途不同：第一次应用真实效果，hydrate 后第二次绑定新 SourceRefs。可复用分组、原 proof 和绑定准备，或在依赖没有变化时省掉第二次纯重复工作；不能因为 SQL 页相同就删掉新锚关联。

tick 头批读的数据等待 semaphore 或其他 scope 的 Jev 后可能过时。它可以作为准备材料，不能当实际执行时的场景/配置/身份许可。原 `_sources_current` 明确限定一次核验阶段；Jev 外部 await 后继续核当前 scene、generation、policy 与真实来源，最终接纳仍走原 repository writer/CAS。普通 admission 和 SELF proposal 不拿跨 tick 的 `allowed=True` 作为资格。

pending proposal 原持久接纳查询先于丢弃/模式处理，保持原行为；不增加 proposal 过期时间或“Jev 恢复后才准接纳”的门槛。确定入口、原 Work/wait、普通 continuation 不排进新的后台队列。

同一次同步 evaluation 内，可显式传递已解析的 participation checkpoint、分组和 pressure，删除连续重复 `model_validate/model_dump`；不能把原 raw 与 typed 结果各常驻一份作为默认方案。中途发生实际状态写入后，局部值按原变更重建；保原 namespace 的未知 extension 和单 unit reader 语义。当前 Record 为浅冻结，不能凭 `frozen=True` 认定内嵌 dict 不可变，或把可变 typed 图交给线程继续使用。

## 6. 生命周期与验收

| 情形 | 必须成立 |
| --- | --- |
| 空闲、非空候选与无候选 | 每个原 tick 的概率、sample_sequence、arrival/selection 顺序及 save 语义不变；重准备次数下降 |
| Jev 退避、无凭据、in-flight | 不为必然不会发出的观测做完整准备；expiry、quiet 纠正、真实 feedback 与 intrinsic 不停 |
| 新 scope / `/ai new` 无新消息 | 保留 start 全量发现、原约 10 秒发现与 generation 退休，不能只等消息 dirty；新 scope 从原入口冷装配 |
| scope 被淘汰/停用/重启 | pins、in-flight、旧快照真实 revision 与提交确认保持；派生缓存随 session 退休，不跨代挪用 |
| 修改、删除、ASR/视觉补全 | 同一 event ID、没有新消息水位时仍失效；旧 basis/boundary 源同样覆盖 |
| 配置删除/rollback、群/route/Presence 变化 | 下一原 tick 的 scoped 比较发现；跨 await 当前检查阻止旧许可产生新效果 |
| 人物合并、unit/basis/目标变化 | 只重备依赖该人物/新 ID 集的 scope；原 Person 与历史执行身份不重写 |
| 迟到 caption/父锚、终态 run 的晚到成功 | 按原回执补事实，不重发，不因父窗口外或 run 终态漏记 |
| hint 丢失、同字段值再次通知、准备期间新事件 | scoped 事实比较仍能恢复；旧批次不清新批次；没有依靠定期全重建维持正确性 |
| next_due 替换/取消/恢复、恰好到期 | 保原严格比较和优先顺序；不提前消费 queue，不新增随机机会 |

先构造固定时钟与固定外部返回的旧新配对回放，逐 tick 对照原 State、proposal、sample_sequence、observer sequence、run/admission 与回执引用。再做 1/8/32 scope 的空闲、突发、Jev 长退避和慢请求计数：ledger 全文行/字节、runtime 完整装配、身份查询与重复 ID 收集、Social 分组、checkpoint parse/dump、save 编码次数，以及 CPU、峰值分配和 loop 延迟。

每个指标区分轻量当前事实读取、实际重准备、Controller 必要推进和持久化；不能把 mock 调用数当 SQL 收益，也不能因仍保存 sample_sequence 就认为优化失败。参数热更/冷恢复、故障重启和原 pending 接纳查询必须和数学不变性一起过审。

## 7. 容量复审：认原逻辑编码，不认物理冷热体积

旧 Store 的 1 MiB/scope、16 MiB 总量按完整 `State.model_dump_json().encode('utf-8')` 计量。最简确定实现可以在冷准备时，从同一冻结 State 取得原完整编码 `old_bytes` 和具有原叶序列化类型的物理热编码 `hot_bytes`，保存可重建的静态差额：

```text
K = len(old_bytes) - len(hot_bytes)
每次热保存：logical_bytes = K + len(new_hot_bytes)
```

布局、字典顺序、存在性和冷热归属不变时，两种编码中每个动态叶的 JSON token 相同，只有包装与冷身份等固定字节不同，故 K 不变。热区可含固定包装与嵌套 typed 数值向量，不必逐叶计算长度或再存 wrapper/leaf 数目；不能把 K 误称为物理 body 长度或假设它必为正。布局/存在性改变时用 full save 同时更新 body、K 与热区。计数器保 Python int，last_action 按原字段类型属于热叶；未知扩展与长身份的字节贡献由原完整编码纳入，不能凭固定小头估算。

实际 Pydantic 反例：`x: float = 0` 的运行默认值虽是 Python int，原模型输出 `0.0`，泛型 `Any` 向量却输出 `0`；`1e-6` 的 stdlib 表示为 `1e-06`，Pydantic 为 `1e-6`。因此必须用原 float/int/Optional 类型与相同配置的 typed 热 schema；仅换成同品牌 JSON 库还不够。负零保 `-0.0`，非有限值按原 `ser_json_inf_nan`，不能先统一成 int/普通字符串。类型与布局固定后，逐热值改变验证公式等于原完整输出字节长度，包含指数切换、任意长整数及 null 边界。

head 的 logical_bytes 与原 CAS 同事务保存；总量 SUM 所有原 scope/generation 的 head 长度元数据，继续扣旧长度、计新长度，不另建全局增减账本。旧迁移初始 logical_bytes 保留原 payload 的实际长度，首次真实 save 才按原模型规范化后的长度更新；不能因升级重排 JSON、清退旧字段或忽略退休 scope 而偷偷改变全局占用。

这条最简公式允许 cold/full-save 时做一次原完整计量和一次新 body 编码；热提交只编码热区，不访问 C。两次冷遍历必须如实纳入冷成本，不能同时宣称 full-save 只编码冷材料一次，也不为省这次低频工作引入自制 JSON 拆片器。容量阈值前/等/后、非规范旧 payload、全局含旧 generation、超长身份/转义/未知扩展以及生产者在 await 后继续修改，均与原 oracle 和原提交顺序对照。
