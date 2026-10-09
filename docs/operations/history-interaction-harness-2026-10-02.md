# 历史快照与长任务交互 Harness 交付记录

## 核查基线与授权

实现基线为 `513a263519f1c4d237840ffe5f27ea0697797db5`，分支
`codex/history-interaction-harness`。用户授权最后全面核查、任务书小修、实施、验证、
PR 合并及上线；具体能力验收由用户完成。本记录随进度更新，不把设计或测试冒充部署。
原施工任务书已退役；现行历史与交互规则见[主 Agent 合同](../architecture/main-agent-runtime.md)。

三个子 Agent 分别核查历史与实际协议、工作与发送回执、执行循环与验收。
直接复用 PreparedHistory/FrozenFragments、原 effect/Social 回执、steer、CAS 和统一 Runner；
局部调整实际请求来源、checkpoint communication 子路径和执行/收尾边界。
不增加第二 runtime、汇报线程、逐阶段台账、逐输入回复决定或定时群发器。

## 上线前自然流量与前缀基线

2026-10-02 03:48（Asia/Taipei）只读检查：Bot 仍运行
`ghcr.io/yuanyeyoutao/yuki-qqbot:ops-ea446d6`，OCI revision `ea446d6`，数据库 `0088`。
从容器标签读取完整 Compose 列表，SnowLuma ID 仍为
`9e7a1a89696eae3922e4b40daee295edebc0fb60e18c7c9bf5a15efe7a1c8b85`。
本轮没有切路由或追加测试模型/QQ 请求。

Bot 只读最近最多 5000 条 invocation，并限定上一版部署后成功、input 有计量的 `chat_agent`：

| 指标 | 数值 |
| --- | --- |
| 请求/Profile | 59 / `connection_2d4ba9aad301` |
| cache 已知 / 未知 | 53 / 6 |
| 全部 input | 2,264,476 |
| cache 已知样本 input | 2,042,270 |
| cached / 已知未缓存 input | 1,793,068 / 249,202 |
| 输入加权已知命中率 | 87.80% |
| 未知全按 miss 的保守下界 | 79.18%，不是观测到的实际 miss |

现存 prompt_projections 全为失效占位；符合 runtime 开启时跳过普通投影提交的 H1 缺口，
不能据此认定所有 cache miss 都由 H1 引起。不同来源的统计窗口包含不同任务，
缓存未报告项保持未知，不与 Bot 直接比较命中率。

按这些 invocation 的原 runtime_turn_id，使用已有索引逐轮读取最多 32 条 provider_start；
筛选实际带 task_control 声明的 Gemini 主请求，共得到 59 条、19 个 turn。
比较完整 system/tools/native 配置和按 role 展平的全部 contents parts，
已有 trace 对签名/媒体的哈希引用按原表示比较，不输出正文或签名。
同轮 40 对续接均保留完整旧 parts 前缀，40 对静态设置也保持；跨轮的相邻 Conversation 样本不是
actor/read-scope 匹配样本，不能把它当作所有跨轮必须完全相同的断言，尤其不能共享私有工具尾部。

新工具合同引起一次显式新链是预期成本。后续技术验证必须比较完整实际请求，
而非只比较 stable_prefix_hash 或前 64 项。上线后只观察自然流量，分别报告已知命中、
缺失计量、未缓存 input 和必要新链；没有可比样本时不声称缓存改善。

## 实施、验证与部署

首批实现提交为 `dac7619c`，补修提交为 `84ccc135`。主 Agent 在首批收束代码上独立运行
15 个相关套件：233 passed（226.97s）；覆盖新历史/沟通/游标/实际 wire 及原 Work、
交付、输入准备、来源守卫、压缩、子并行、主入口和固定工具面。
全仓 Ruff、format（1056 文件）、Linux 平台 mypy（683 源文件）、release_validate v3.9.0
和 diff 检查通过。Windows 原生 mypy 的 POSIX API 报错另按 Linux 目标验证，
没有修改不相关平台文件或放宽规则。SQLite schema 仍为 0088，无新迁移。

最后任务书逐项核对补出的实际修复：旧 consumed 输入不倒追催答、journal 与提醒标记
同事务发布、纯沟通不自证 state_change、非法/其他目标发送不免除阶段机会、每个输入
查原发送见证而不以 256 条展示页代替完整性、pause replay 延后新尾部，通用 Runner
保留原工具声明的对象及顺序。最新 head CI、PR/合并和实际部署另补，不能由这些本地
结果推断已上线。

| 任务书项 | 本轮技术证据 | 边界 |
| --- | --- | --- |
| T01–T03 | Gemini runtime 开/关×HTTP 重试；普通投影/真实 Work 恢复、私有隔离、SQLite 重开、CAS/来源守卫 | 准备/派发不证明模型阅读或平台投递 |
| T04 | 同 actor 选集和插件上下文收窄、源删除后重开、Profile/固定合同变化；原 generation/容量回归 | 选集收窄不是完整 ACL 实模端到端；普通图片投影组合未独立全演 |
| T05–T06 | 真 Runner 调用次序/并行 READ/委派围栏、未执行不计费、失败/unknown、真实 Social typed 结果后保存失败及原 call 无重发 | 原生服务端工具不能由本地逐次拦截 |
| T07–T09 | 阶段与新输入合并、quiet/legacy、新旧水位与断点恢复、保存前后崩溃；原输入/CAS/命令恢复回归 | 不设逐输入结清；连续 steer 跨所有压缩/重启组合及报告语义质量待行为验收 |
| T10–T12 | 明确退出有限反馈、final/状态修改证据区别、原 artifact 附言和子任务/前台容量回归 | 不宣称模型语义目标已达成或 QQ 体验已验收 |
| T13 | Chat、DeepSeek/OpenAI Responses、Claude 实际发送：reply 后原 ID/budget 继续业务和显式退出；完整 Gemini 前缀 | Claude 仅已知 cache_control 标记移动单列；其余内容/工具/系统严格比较 |
| T14 | 9 项/跨 Work/未 staged/平台字符串/错误目标在副作用前拒绝、>256 回执、原子失败与重开、typed 保存失败 | 新元数据复用原 privacy 清理，非独立第三账本；无真实群测试 |
真人 QQ 交互、阶段汇报质量和长任务语义效果由用户验收。

## 04:40–04:41 普通轮报错核查

2026-10-02（Asia/Taipei）用户提交的两轮，生产仍是 `ops-ea446d6`。
只读按内部 turn/event ID 查询原 invocation 和 trace：

| 原 turn | 来源 event | 真实送达 | 后续失败 |
| --- | --- | --- | --- |
| `c9f0fa84f68e455e81a9378d6773f8fb` | 79535 | 79536–79539，4 条 | 已送达后的空响应被多重试一次，下一请求本地容量预检失败 |
| `1137de34f90c4f7dbef47e29269d6cfa` | 79542 | 79543–79545，3 条 | 首次引用不可用，纠正发送成功后，下一请求本地容量预检失败 |

两轮 trace 的 `work_id` 均为 NULL，没有已接纳的持久 Work。
异常是 `WorkCapacityError("model_request_capacity")`，出自完整请求估算超过输入预算的派发前检查，
不是模型 HTTP 返回的超窗错误，也没有特殊 token 解析或网关抽风的证据。
模型对原因的聊天猜测不作诊断依据；成功发送回执不会因后续异常被否定。

补修中性 WorkControl 的普通轮空响应边界，已接纳 interactive Work 仍须明确退出；
普通轮容量不足复用现有 capacity 分类并给准确状态，保留已有结果，不自动重发或伪称完成。
任务书 T11 同步纳入该回归。部署前重新跑最新 head CI，旧 head 的结果不能替代。

另核查到初始投影容量原用 `window_tokens * 3` 字符数，未扣完整 system/tools/资料。
旧生产投影尚未接通，因此不能把旧冻结快照增量说成上述两轮原因；但接通后的新路径
必须修正此风险。改为准备阶段按完整 composition 与固定函数工具使用同一请求估算器，
保留原压缩比例和 4096 token 余量，旧快照超限时记录 `capacity` 并开启新 epoch。
这不是扩大窗口或改写已派发续接；Runner 仍核验实际全部内容，真实链尾容量不足可以停止。
准备阶段复用已编译的 system/rollup/current 插槽，不先重新编译超大的旧历史。
当前 fresh 成本本已超过保守准备预算、但真实上限仍可容纳时，按真实上限判断旧投影；
没有可回收材料或 fresh 已无法容纳时，不空耗新链。该边界单独验证，避免因准备余量
不可实现而每轮换链，真实上限和最终预检保持。
fresh 本身超真实上限、旧投影又超过编译字符预算的交叉情况，保留原投影、不给恢复
journal 提前加估算门禁；只有编译器真实的 required-dynamic 容量错误采用 typed
`PromptCapacityError` 并按 capacity 反馈。负预算、重复贡献等编程错误仍按原错误处理，
不泛化捕获 ValueError，也不靠异常字符串判断容量。

上述补修的统一相关验证为 13 套件、111 passed（96.00s），含 4 项初始容量、2 项
发送后空响应和 6 项容量状态/精确分类，以及历史、原恢复、发送、输入与主入口回归。
新代码全仓 Ruff/format（1059 文件）、Linux mypy（683 源文件）、v3.9.0 release baseline
及 diff 检查通过。最后 fresh-hard/编译器交叉新例由主 Agent 独立验证 1 passed；
子 Agent 的该文件完整 5 passed。使用隔离的模拟连接容量和 HTTP/网关回执，证明无新增
HTTP/Work/重发、原 epoch/revision/payload 和已发送事件保持；不冒充线上数值预算。
最新 head 全量 CI 结果见下方交付核验；这些模拟检查不替代真实模型行为验收。

05:25:10（Asia/Taipei）部署前再次只读复核，Bot 仍为 `ops-ea446d6`，健康且零重启，
34 个 Compose 标签文件存在，SnowLuma 原 ID/运行状态保持。原 6 Work（5 suspended、
1 waiting_external）以及 60 effects、20 inputs、6 journals/budgets/recoveries 原 ID 和
基线哈希均保持。最新修改不涉及迁移/schema guard/表结构；停写一致性备份与原回执对账
流程可复用，尚未执行镜像替换。

首批全量 CI 为 2261 passed、7 failed、1 skipped：7 项失败均为新 accept 初始化
`communication.input_feedback_through_id=0` 后，旧测试的 checkpoint 精确期望缺该字段。
独立复现确认原 checkpoint 其余内容、预算、journal 和 unknown effect 保留；仅更新精确期望，
不删除或忽略字段，相关三套重跑 31 passed。

## 合并与交付核验

最新 head `84ccc135c4e140a588ce11f63897affd4eb37d5f` 的
[CI 36928934111](https://github.com/YuanYeYouTao/Yuki/actions/runs/36928934111)
六个 job 全部通过；全量 pytest 为 2281 passed、1 skipped（1030.92s），
包含固定安全门禁、插件合同、WebUI、类型/格式检查和 0088 fresh install。
[PR #218](https://github.com/YuanYeYouTao/Yuki/pull/218) 于 2026-10-02 05:48:19（Asia/Taipei）
合并为 `8ca2e17a893f3b46b50b09f7a8e335ee9f624543`；合并树与已验证 head 完全相同。

正式镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-8ca2e17` 从该合并提交构建，
amd64、包版本 3.9.0、完整 OCI revision 与合并提交一致。
13 个关键已安装源码文件按 LF 归一化后与合并源逐项一致。
传输归档 SHA256 为 `d23af5ac4e4db6c7abaee0e1b15e9abb979046ee504ace594498466e8475e536`。
正式部署仅停止/重建 Bot；停写备份路径为
`/opt/yuki-qqbot/backups/pre-history-harness-8ca2e17-20261001T215053Z`。
备份包含原 Compose/配置、数据库和工作证据目录；SQLite backup 的 integrity、外键及
schema 0088 校验通过，引用核验确认 774 个 protocol objects、0 个 tool artifacts。

新 Bot 实际 StartedAt 为 `2026-10-01T21:55:23.469019063Z`（台北 05:55:23），
镜像 `ops-8ca2e17`、healthy/running、0 restart。部署 SSH 输出连接中断后没有重跑部署；
重新只读核实停写/启动后的持久对账文件，`comparison.ok=true`、errors 和 changed_counts
均为空，随后再次按原停写基线对在线库复核也一致。原 6 Work、60 effects、20 inputs、
6 journals、6 budgets、6 recoveries 保留原事实；没有重置预算、原 ID 或重发旧回执。
SnowLuma 原 ID 保持 running/0 restart；没有路由更换。

上线后健康核验：`/healthz` 的 status/database=ok、OneBot 已连接，automation/emoji/
runtime-work/wait/subagent workers 正常运行，runtime error category 为 NULL；
`/livez` 200、`/ui/` 200。启动后截至 06:01 的有界日志共 104 行，包含 startup complete
和 OneBot connected 标记，没有 ERROR/CRITICAL 或非 JSON traceback 行。
记忆诊断 `memory_consistency_healthy=false` 单列核查：现行谓词唯一非零项是
superseded_without_chain_count=162，停写备份与在线库计数相同，其余六项均为 0；
该旧数据指标不是本轮新增，不将它包装为系统全部健康，也未扩展修改记忆数据。

补齐 T13 的 Gemini interactive wire 组合：新增独立测试验证原 Work 关联 reply 后
继续业务、内部正文获得一次退出反馈、显式 complete；五次真实 serializer/MockTransport
请求的 system、全部 tools、toolConfig、generationConfig 稳定，展开的完整 role/parts
只追加，原 thoughtSignature 与 functionResponse ID 保留。原 Work ID 及累计 5 requests /
3 tools 不重置，发送次数不增加。主 Agent 独立重跑 1 passed（1.06s）。这仍是模拟协议
证据，不冒充真实模型或 QQ 行为验收。

自然缓存观察使用实际启动 UTC `2026-10-01 21:55:23` 作为 cutoff。
截至 05:59:40 的有界查询没有上线后的 invocation，因此命中率为 NULL、普通首请求
比较 0 对；投影当前仍是旧失效行，没有自然新轮可以验收。本轮不制造保温/测试请求，
不宣称上线后缓存改善或达到某个百分比。
计量缺失不得记作 0；input 缺失的成功请求不在使用量样本中。普通初始请求比较按
内部 actor/conversation/generation 分组，是样本内下一条可核实请求，可能跳过 UNKNOWN
记录，不声称实际相邻；相同 source 的重试另看，read-scope/epoch 无严格关联时为 UNKNOWN。
同轮 prepared snapshots 仅作为辅助，不能替代实际 ordinary initial 的 operation/response
关联，更不能将私有工具尾部跨轮复用。最终阶段报告、连续 steer 和真实 QQ 能力由用户验收。
