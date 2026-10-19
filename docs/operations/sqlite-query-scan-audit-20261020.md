# SQLite 热查询扫描审计

2026-10-20，基线 `8368c286`，另含本分支的提及修复 `0142`。本记录是其他模块的只读调查，以下八组问题尚未修复；不把提及 PR 描述成全库查询优化完成。

## 调查方法

检查当前消费者、索引与实际生成 SQL，使用隔离的当前 schema 和合成资料，以 `EXPLAIN QUERY PLAN` 及每条 VM 指令的 progress handler 复核。固定实际命中数量，分别增加不相关账号、旧名称、已完成历史或暂停队列，观察是否仍线性增长。没有操作线上数据库、发送真实消息或调用模型。

身份模块的实际 API 在 Linux CPython 3.12.15／SQLite 3.46.1 验证；Memory、Code Mode 与投影实验在 Windows CPython 3.12.13／SQLite 3.53.1 验证；提及与插件暂停队列两种 SQLite 均复现。VM 数用于同一实验内比较，不代表生产耗时或 SQLite 锁等待。Code Mode 调用捕获原 SQL；投影实验使用发布回调的相同 ORM 聚合表达式与真实表、索引 DDL，未执行实际发布。

`SEARCH` 不等于只查一个目标：只用索引前缀筛平台或 Work，仍可能遍历整个平台账号或该 Work 的全部效果。扫描请求自身的少量 `json_each`、对两个候选排序，以及主动导出、迁移和重建的完整读取，不作为无关历史扫描问题。

## 已确认的问题

除首行旧名称规模为 1／1000／10000 外，表中三个数字分别对应规模 10／1000／10000；各组的规模对象不同，不能直接横向比较耗时。

| 位置 | 固定结果、增加的非候选 | 实际 VM 步数 | 触发范围 |
| --- | --- | --- | --- |
| [入站名称观察](../../src/yuki/identity/person_state.py#L113) | 同一发言账号，旧名称 1／1000／10000；读取结果 3／1002／10002 行 | 62／14048／140048 | 每条入站准备；不是全库扫描，但读了该账号全部旧名 |
| [私聊身份回查](../../src/yuki/conversation/hydrate.py#L82) | 固定一个目标，增加其他账号与归属阶段 | 338／29048／290048 | 缺内部 Assignment ID 的私聊 scope 回查；不是所有原生私聊 |
| [路由 Presence 候选](../../src/yuki/identity/routing.py#L805) | 固定一个 Yuki 账号，增加普通账号 | 170／12050／120050；群候选为 70／3040／30040 | 路由发现、接管和合并准备；已有 pin 的群回复不逐次走这条路径 |
| [插件暂停任务轮询](../../src/yuki/plugin_host/notification_repository.py#L1384) | 全部关联合法暂停的 Work、没有可执行 Job | 662／55112／550112 | 默认每秒 claim；先读整个 pending due 池再逐项检查原 Work，最终返回零项 |
| [Memory 到期发现](../../src/yuki/memory/repository.py#L1427) | 固定一条到期，其余 active 事实永不过期 | 509／5459／50459 | 默认每 300 秒；扫过 `valid_until IS NULL` 的全部非候选 |
| [Reflection 空轮询](../../src/yuki/memory/self_reflection/control.py#L333) | 已完成且已投递历史，无待办、无重试、无报告 | claim 56／4016／40016；retry 46／3016／30016；reports 141／11031／110031 | 默认每 60 秒；retry 在非计划小时查，reports 仅有 social 时查 |
| [Code Mode 子效果](../../src/yuki/runtime/work_repository.py#L2143) | 固定一个 parent 的一个子效果，增加同 Work 其他效果 | 122／6062／60062 | 恢复或加载 composition 子效果；JSON 路径绑定参数导致既有表达式索引不能按 parent seek |
| [上下文投影容量统计](../../src/yuki/conversation/projections.py#L455) | 固定一个活跃 view，增加已经失效的旧 views | 69／5017／50017 | 发布新投影的回调中执行全表聚合；发生在发布写事务内 |

### 修复方向

1. 入站名称观察先构造本条消息真正要写的规范键，再按 Account、阶段、scope、name 批查这些键。后续 apply 本来只使用这批键，无需读旧名全集或维护另一份缓存。
2. 私聊回查从 `(platform, external_account_id)` 唯一 Account 开始，再查原 active person Assignment。当前 canonical repository 的 `find_assignment` 在相同规模均为 57 步，可复用其查询形状；保留原 active 与 person 语义，不因复用而改变 disabled 的处理。
3. 路由候选从少量真实 Yuki 角色／Presence ID 查找，再关联平台。`_space_candidates` 当前的 `select(PresenceModel)` 也扫描全部 Assignment；不能只换一下 JOIN 书写顺序便认为修好了，要验证实际驱动集合与索引。
4. 插件轮询先筛可调度的原 Work，再取其 Job；没有关联 Work 的 Job 和过期 processing 回执分别按自己的索引分支处理。保留原 Job、Work、预算、回执及 CAS，不加“暂停超时”、重试次数限制或另一个 readiness 状态来掩盖扫描。隔离 Linux 的 10000 条暂停池约需 54 ms，这是特定合成规模的测量，不是现网积压证明。
5. Memory 到期发现可用 `(status, valid_until, id)` 范围索引。隔离库增加该普通索引后，相同 10000 条、相同结果为 492 步；无需新增任务或状态。
6. Reflection 按现有 status、retry_state／next_attempt_at、delivery_state／trigger 建立候选索引。隔离库只加普通索引、不改 SQL，10000 条历史的三次空查询降为 40／24／65 步。必须用真实参数化谓词验证，避免 partial 谓词不匹配。
7. Code Mode 的 JSON 路径属于固定程序结构，应作为 SQL 常量，用户值继续绑定。隔离实验仅固定路径、不加索引，既有 parent 索引命中后三个规模均为 112 步。其他 JSON 条件也要核真实编译结果，不批量替换数据参数。
8. 投影聚合与淘汰只需覆盖活跃 views；可沿现有 active 谓词及淘汰顺序组织索引，避免每次遍历旧失效行。不另存全局计数器，不修改上下文前缀、已冻结投影、预算语义或失效事实。

优先处理逐条入站、投影 writer 内聚合和每秒暂停任务轮询；其他问题按实际调用路径处理。除投影发布明确在 writer 内外，以上读扫描不能直接表述成“占着写锁”；长读、写入维护和锁等待需分别取证。

## 本次已修复和已排除的情况

- 提及名字查询固定由请求名称驱动；平台账号从 10 增至 10000、固定两个候选，Windows SQLite 3.53.1 的名字／最近发言／同查询定位 Conversation 分别稳定为 253／168／189 步，Linux SQLite 3.46.1 为 226／176／197 步。显式 Account 在两种环境均为 47 步。详见[提及修复记录](mention-resolution-0142.md)。
- 新的真实消息部分索引替代旧同用途索引，原泛历史、作者、回复、隐私查询仍有自己的通用索引；本次没有删除它们的索引。
- Embedding 的固定一条到期、其余未来待办场景均为 72 步；Dream 的 run/status/id 查找和表情队列时间游标没有发现同类无关历史增长。
- 通知 outbox 正常先 claim，再在没有可 claim 项时准备未 prepared 项，不能将“全部 prepared pending 堆积”的异常池笼统写成每秒常规全扫。
- Work 已有按 work/key、更新时间、状态、JSON 结果和子树的索引；不能仅凭冻结 `work_schema_v1` 没有写出这些索引便断言缺索引，当前安装器与真实 schema 才是依据。

## 交付状态

此轮完成调查与隔离复现。八组其他模块问题仍待实施，没有混入提及修复，也没有声称已上线修复。
