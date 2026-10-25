# 参与循环与后台扫描：10-25 初查记录

日期：2026-10-25，10-26 整理。状态：历史初查，未修改运行时代码、依赖或生产。

参与实施方案已由 [10-26 按需准备与窄快照](../Yuki-参与循环按需计算与窄快照方案-2026-10-26.md)替代。本文件只保留初查事实与同类后台扫描，旧时间键、稀疏锚及改变冷恢复的待实施内容已移除。

本轮以 Yuki `75e21a5c824876485d75e7fcd58e8111bdddeb4d` 为基线；参与库按
`pyproject.toml:30`、`uv.lock:1183` 固定的
`79d266918dc6ce19b7d81568565deaed450301d5` 阅读。下文独立库行号均指这个提交，
不是本机参与库工作目录当前分支。参与业务代码与先前生产审查基线 `35b49` 一致。

已核对 [开发约束](../development-contract.md)、[语义参与合同](../semantic-participation.md)
及[连续概率模型](../autonomous-participation-model.md)。这里给算法、数据表示和删除方向，
不以增加冷却、降低采样频率、后台队列或影子版本替代优化。

## 1. 先把问题说清楚

参与控制器每两秒推进时间是有用的：即使没有新聊天，来源会到期，暴露和活动会衰减，
已有候选仍可能产生自主机会，观察待办也会进入执行时刻。问题在于，每次推进都伴随
历史水合、回执分组、身份准备、重复解析、整份状态编码和落盘。

线上只读证据在
`D:/Code/Reports/Yuki/live-stall-20261025-1658/participation-idle-snapshot-audit.json`。
22:07:40–46 的六秒窗口中，八份当前快照分别增加了二至三次 revision；这些会话的
聊天事件水位没有变化。八份当前 payload 合计 447,351 字节。全库 41 份快照共
6,102,403 字节，这是磁盘 JSON 长度，不是进程 RSS。聊天水位未变也不能单独排除
迟到回执、撤回、人物合并、配置或观察状态变化；重复工作的证据还包括真实采样栈和源码。

现行主要链路：

| 位置 | 当前工作 | 成本/边界 |
| --- | --- | --- |
| Host `semantic_participation.py:1316–1368,1516` | 每两秒推进所有保留 session，并发二 | scope 数增加时，工作量至少线性增加 |
| Host `:1389–1396` | 同一批 Social 近窗先 sync，hydrate 后再次 sync | SQL近窗已共用，但分组、绑定和部分来源准备仍重复 |
| Host `:699–873` | 每轮重读最多64条历史，批读 Work/admission，再逐条投影 | 有边界，不是全群历史全扫；确实在重复处理旧页 |
| Host `:1404,1425` | 观察前后核来源 | 第二次核验有跨外部等待的真实语义 |
| Host `:1493–1502` | advance 后保存全量 State | 仅时钟推进也改变 payload |
| 库 `controller.py:1829–1908,2643–2793` | 推 replay fence、重锚 baseline、prune | 不仅 `now` 在变，基线时间和值也持续变化 |

旧的“重复收到相同效果仍触发一次 prune”不能再当作当前问题：固定版本的
`observe_committed_effect:2383–2406` 已跳过重复效果；Host
`participation_feedback.py:802` 也跳过未变事实。

## 2. 10-26 修订与现行入口

初查正确指出重复 hydrate、checkpoint 解析和整份编码，但曾建议用时刻替换 sample_sequence、少做无贡献重锚、恢复时按当前 tau 解释更早锚。该方向现已撤回：

- 固定提交的真实 Controller 证实十次旧衰减与一次大步衰减相差 1 ULP；跨 tau 冷恢复差异更大。
- 不保存原随机进度，会在重启后回到不同的 hash 流。即使 dt 或 total 为零，部分原分支仍递增 sequence。
- 不改数学也能少做工作：同次 preparation 共享，重准备按真实 scoped 事实/原到期边界触发，必要热数值与冷体竖向存储。
- 同一 SQLite row 拆两列仍可能重写大 BLOB；选同 Store、单 revision 的窄 head 与冷 body，不新增后台存储机制。

现行设计和完整验证以新主方案为准。证据分为[Host 增量](participation-20261026-incremental-host.md)、[38字段与存储](participation-20261026-state-storage.md)、[精确等价/WAL](participation-20261026-equivalence.md)，合成摘要见[JSON](participation-20261026-evidence.json)。旧 10-25 总证据 JSON 仅代表当时实验，不是新数学或恢复方案的验收。

## 3. 其他后台循环的同类审查

下列均为源码风险或排除项，没有新线上采样，也没有证明其在本轮故障窗口触发。
P表示当前未处理对象数，H表示历史对象数。分页是按真实候选继续处理的算法，不是
把任务做一半后标完成，更不能因为一页内没有成功就放弃其余对象。

| 循环/位置 | 当前形状 | 结论与方向 |
| --- | --- | --- |
| `CompletionReceiver:44–70` → `external_agents/service.py:727–766`，每2秒 | SQL筛需要retire的原operation，全部materialize后每条建task；进行中的_retiring也仍被SQL取出 | 新发现P级瞬时列表和无界同轮启动；按原operation稳定keyset页处理，排除当前正在retire的原ID；不取消原任务/新建执行、不按历史终态重试外部操作。原操作实际确认再出候选 |
| `plugin_host/background_turns.py:103–105` → `notification_repository.py:951–973`，每60秒 | 取全部可结算job ID，再每条writer重读/结算 | 新发现O(P)列表与逐项事务。按job稳定ID分页，页内准备原Work结论，writer复核原job/Work后批量结算；不在writer里扫描历史，不为通知重新运行Work |
| `automation/worker.py:87–110` → `automation/repository.py:513–549` | 到期候选先全部ID入内存，再切128条更新；worker全部启动 | 新发现分段写并未消除全量候选列表。直接以(next_run_at,id)读取候选页、按实际领取时间核租约，逐页交原执行者；同一次occurrence仍唯一、同一轮未领取项不能被本地cursor永久越过 |
| `subagent_scheduler.py:90–97` → `subagent_repository.py:161–191`，每秒 | 未通知revision候选先全量ID再逐项acquire/finish | 新发现候选列表随积压增长；按原work ID/revision分页和原结算入口批处理。后面的过期root树回收`:195–248`已有16条候选边界，不将其误报为全量materialize |
| `sandbox/continuation_worker.py:39–59` | 每2秒ready LIMIT20，按实际请求路由，finally rotate | 有分页和原ID，不是空闲全量保存。rotate会真实改变未就绪ready行的公平次序，不能只为少写而直接删除 |
| `emoji/delivery.py:286–299` | wake后按100条游标扫未closed intent，空闲等待Event | 没有两秒空轮询。wake时会再读正在deliver的intent，才由_deliveries跳过；可在原pending查询排除进行中原ID，仍保原closed/receipt与失败恢复。不是本轮确定CPU根因 |
| `plugin_host/notification_delivery.py:298–349` | 空闲1秒claim→prepare→claim；prepare每次从0扫未prepared页 | claim有候选LIMIT128、空结果不进writer；路由不可达的旧前缀可能每轮重复准备。可沿实际路由可达性join过滤并保持后续对象可处理；不能设失败次数门槛或把不可达记成已送达 |
| `memory/worker.py:126–155`、embedding/rebuild workers | wake/原周期只领取ready batch或一个executing run | 已有批处理/无任务直接返回，没有证据需要另建统一timer框架。提取/embedding模型请求不是本地CPU算法耗时 |
| `autonomous_groups.py:145–166,168–199` | 真实输入驱动的单组去抖，完成移除state | 不是全量后台tick；保原输入交接，不能为“空闲清理”删掉最后changed事件 |
| `processing_status.py:125–132`、TG长轮询、飞书ping | 协议活动、网络长等待/心跳 | 无历史全量计算，不通过降低协议心跳规避算法问题 |
| `diagnostic_writer.py:124–134`、插件EventBus`:143–165` | Queue.get阻塞，完成释放payload | 空闲不扫历史；瞬时诊断复制另按原owner审查，不扩大为第二业务队列 |

另外，`environment_supervisor.py:31–46` 在终端进程退出/信号清理时从/proc得到父子关系，
反复扫全部pairs直至闭包。链很深时为 `O(N×depth)`，可以一次建parent→children邻接表
再遍历，降 `O(N)`、空间`O(N)`。它不是空闲timer，也不在Bot主进程；保留同一真实
进程快照语义，不能借它解释当前Bot持续CPU。`children(child)`在`:141,149`的调用点是
结束清理事件，进程树变化与PID身份仍按现有真实操作处理。


10-26其余复核：上表后台形状保留为初查。现行A12已按消费者区分页内批SQL/原lease结算、窄ID分页及仍未解决的活跃task上界；分页不能单独证明全部积压压力已解决，详见[总任务书A12](../Yuki-算法与资源开销优化任务书-2026-10-25.md)。
