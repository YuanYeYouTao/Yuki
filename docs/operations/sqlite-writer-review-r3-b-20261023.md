# SQLite 实现后复审 R3：公共 owner、装配与自动化

产品起点：`1fb34b85031dc3a7df1f8f5c7e7f0cdf60fc6966`。这是第三轮实现后轮转复审，范围为 `collect_surface` 原 D 的 110 个源码路径，另加公共 persistence、Automation、Alembic/离线 owner，以及 Code Mode 四个实际 reader 消费者。没有把前两轮报告、diff、AST 搜索当作本轮源码语义审查。性能在独立冻结副本执行，本轮没有修改性能脚本、运行真实模型或访问生产。

公共层和Automation修正被父任务纳入 `c0af6e82` 的性能冻结点；窗口结束后另完成下文旧GroupAdmin删除及status空context删减。本轮验证和源码复读分开记录；没有把性能冻结当作 PR、合并或上线。

## 本轮修正

1. **删除 SQLite checkout 的隐藏探针。** 主程序的 `require_canonical_schema` 明确只支持 canonical SQLite。除 Database 外，唯一取主库原生 driver 的业务消费者是 history read 的 `interrupt()`；没有将池中 driver 裸关闭后当作健康连接归还的生产调用者。独立小库自行建立/关闭连接。删除 `pool_pre_ping=True`，不增加替代探针或重连层；测试直接记录 native SQLite trace，不能仅据 Engine 的 SQL 计数声称已省掉探针。
2. **删除取得同一 driver 的第二个异步任务。** 实际 `session.connection()` 完成 checkout 后，原 `get_raw_connection()` 只通过 SQLAlchemy greenlet 返回已持有的 pool proxy。改为直接取 `connection.sync_connection.connection.driver_connection`。先保存实际 driver，再处理 remembered cancellation，原后续 rollback/physical exit 仍由同一 owner 等待。没有重新 checkout 的 fallback。
3. **离线临时连接显式关闭。** `scripts/verify_monty_packaging.py` 的原生连接上下文原来只结束事务；改为 `closing`。不改变打包条件、迁移 SQL 或历史版本。
4. **owned reader 也必须拥有实际 checkout。** 真实 SQLite trace 阻塞 `PRAGMA foreign_keys=ON` 后重复取消，原 reader 已返回而线程仍在工作。reader 现在在进入业务前完成并拥有那一次 checkout，保存同一实际 driver；不另借连接、不加 Session 子类或连接注册表。再加真实执行中的 SELECT 取消，发现 SQLAlchemy 的中断 invalidation 留着原 pool proxy，close 的 `ValueError("Connection closed")` 还会覆盖原取消。沿既有 Session invalidate 完成原 pool 归还，再 close、等同一 physical_exit；保原 CancelledError。测试同时证明返回前线程已退出、`pool.checkedout()==0`、后续 reader 可用且无 non-checkin GC 警告。正常 reader 新增一次 `_complete` checkout task，writer 删除一次 raw-get task；没有隐藏这个正确性成本。
5. **Schema 与 Alembic 的独立 NullPool 生命周期。** 两条实际延迟 connector 回归均先 red：finally dispose 仍可能在 native connector 返回前退出。现在用既有 `_complete` 等各自原验证/迁移及 dispose 完整退出，再传播取消。没有中间连接缓存或迁移重放；没有改冻结 revision。取消不承诺原迁移没提交，原 Alembic head/事务才是事实。
6. **Automation 创建 run 的 ACK 丢失。** 实际 COMMIT 后抛 ValueError，原 run 已存在，但局部赋值没有返回，原 `run is not None` 收尾分支漏掉它，唯一发生又阻止重新 claim，因此停在 RUNNING 直到重启。将现有结算移到 `_process_guarded` 的真实失败出口，以原 `(automation_id, scheduled_for)` 点选 occurrence；原 Work-owned exclusion、run/version/预算和完成 CAS 均保留。正常来源与来源已停用两个真实场景都结算原 run 为 uncertain，仅加一次实际计数，0 模型、0 消息，不重派已发生调用。删除局部 `run=None` 与重复异常收尾。
7. **删除旧群管理空链。** 全仓src/tests和文档反查后，移除无消费者的GroupAdminService整文件、AdminBundle.groups、AdminModule参数/装配、容器别名与导出。GroupRecovery仍用的SpaceAdminContext就近定义；真实`operations.group.set_enabled`及恢复/audit/原receipt不变。不保兼容别名、不写镜像该旧实现的新测试；用真实group/control既有回归验证。
8. **删status的空reader。** 无scope时不再进入read_session；有scope仍执行原projection查询与原“无检查点”显示。没有新缓存、资格谓词或模式。

## 本轮实际边界

- Database：逐方法核 `_complete`、creator、完整 checkout、BEGIN/body/autoflush/flush/commit、rollback/invalidate、Session close、driver stop/physical exit、FIFO 与 shutdown。writer 的业务 body 仍在原调用协程，不把业务 callback 交给屏蔽任务；只有真实 driver/session 收尾操作等待完成。未知 ACK 不因异常类型变成未提交，最后 CommitHint 在确认根提交及释放 owner 后同步发布。
- UoW/diagnostic：supplied session 原样复用，owned reader 不 flush/commit；DiagnosticWriter 只承载丢失可接受的准备和诊断 SQL，不接管执行、付费回执或隐式重试。队列的字节 reservation 保留到真实 encoder/commit 退出。没有恢复 holder、首写或观察用连接包装。
- 装配：`ApplicationContainer.create` 的真实 Database 由应用持有，外部注入仍由原调用者关闭；create 的 BaseException 收尾和 identity start hook 的逆序回滚均核实际调用。worker 注册在 database 后，停止顺序反向；health 分别报告读连通与 writer 接纳，不能以 SELECT 1 代替写健康。
- Automation：repository 全部实际方法、service、control/context/authority、worker claim→唯一发生→Main/Work→原回执→结算及 heartbeat 全链重读。正文/hash/authority 准备在外；writer 核当前资格、租约及原 version/run。claim 的 lease 取准入后时钟，首次安排仍保原请求时间。任务+版本+审计，以及原 run+任务计数/下次安排，各保原子单位。
- 模型付费链：`llm/json_http.py` 全文核 HTTP 之前的 dispatch/account，响应后的 finish/trace，以及原 transport retry 范围。annotated 数据库 ValueError/OSError 不属于 transport retry 类型，不能重新购买模型。各协议子类只排除各自 wire codec，没有把这个付费基类列为纯渲染。
- Code Mode：全文核 driver；`_admitted/_vm_value/_parent_row/_live_source` 是四个真实 reader。历史 receipt/source 的 decode、artifact 文件读取与 VM 都在 writer 外；T0 准备 private object，T1 原 Work owner 同事务发布父 checkpoint、snapshot refs 与 child intent，T2/T3 复用原 Invocation，T4 只消费原回执。没有重建子调用 ID、原预算或 VM。
- Tool artifact：所有 DB/文件/授权/GC 方法逐一读；纯 JSON 展示函数不声称全文语义审查。storage lock 保文件发布及元数据提交，未知提交不 unlink；deleting 围栏、真实 refs、活动祖先 Work 保留及页级 GC 不改成普通缓存。文件前后 reader 授权核验不搬到 writer。
- 离线：CLI 每个 Database owner、reset 的 generation/floor/SELF authority/reset receipt、Schema metadata 检查和 Alembic engine lifecycle 均核具体入口。冻结 revision 未改；普通启动仍不扫描业务全库 FK。独立文件/跨进程不纳入主库 FIFO。
- 跨 owner：Memory claim 的准入前 timestamp 问题由本轮 Memory reviewer 修复；其唯一两消费者及泛型 helper 删除归对方，本报告只在合流后复核 delta，不并发改文件。

## 验证

- 真实 red：首次 reader configure、Schema late connector、Automation run 根 COMMIT 后 ACK 丢失最初 3 项全部失败；独立 Alembic connector 取消也 red；执行中 SELECT 重复取消追加用例发现 close 覆盖 CancelledError 和未归还 proxy，未以“测试 passed 但 GC 警告”称完成。
- 修后 `test_database_acquisition_cancellation.py` + `test_database_writer.py`：**21 passed，0.90s**。覆盖 creator、configure、执行中 reader、reader rollback、native trace/pool reuse/explicit invalidation、FIFO、实际 driver BEGIN/execute/autoflush/flush/commit 取消、未知提交、真实 physical close、关闭失败停写及 shutdown。最后一次没有 non-checkin GC 警告。
- Schema/Alembic 新真实取消及上述公共写入口联测：前一组 **21 passed，5.16s**（当时还未加执行中 reader 场景；和上一项有重叠，不相加）。完整迁移产生原 reflection/datetime adapter 警告，原 schema head 和 revision 未改变。
- Automation 新原发生 ACK 丢失及真实 Main create/pause binding 首次定向 **3 passed，1.73s**；正常/来源已停用参数都在最终广组通过。中间 core/Automation/lifecycle 曾 **62 passed / 1 failed**，那唯一失败正是新增的执行中 reader 用例；未将这次结果计作完成。
- 最终公共层/Automation/Artifact/Schema/Code reader/真实应用生命周期组：**148 passed / 8 skipped，56.84s**。含两个Database故障文件、真实Schema/Alembic取消、offline owner、diagnostic writer/privacy、tool receipts/artifact refs/GC、Code child查询、Automation发生与原版本/计数、Main真实create/pause及外部注入/create/start失败回滚。8项skip全因本机未构建pinned Monty worker/binding，不冒称native Code执行已验收。
- 最终services/group/control组：**173 passed，143.98s**。覆盖native group recovery、group route、control当前视图/跨平台/个人入口、Compact当前checkpoint/status、Memory admin原receipt、plugin session material、Subagent lifecycle/result/scheduled recovery。旧GroupAdmin删除后走真实operations.group与GroupRecovery，不为死代码写镜像实现测试。
- 新增 reader native physical close 真实失败定向 **1 passed，0.18s**，亦已纳最终广组：等原线程close结论、保原CancelledError、停止后续writer接纳。最终两组无non-checkin GC/ResourceWarning；其余为原expression-index反射、循环FK排序及Python datetime adapter警告。
- 最终8个修改产品文件的独立cache mypy通过；15个本轮修改产品、迁移/离线和测试文件Ruff及format通过，`git diff --check`通过。全仓src/tests已无GroupAdmin类、旧方法、旧模块import或空DI字段。临时日志仅在`D:/Temp/yuki-sqlite-validation/r3-b-final-{core-automation,services,mypy}.log`，未加入git。
- 同一代码仅切换 pre_ping 的原生 trace 对照：3 reader + 2 writer，5 条业务 SELECT、1 条复用 native connection，pre_ping=True 多 **4** 条 `SELECT 1`，删除后 **0**。Engine 计时不包含这些原生探针，不能只看 Engine SQL 差分。最终聊天/混合延迟由父任务的公平冻结副本测量，本报告不据此直接承诺更快。
- 跨 owner 的 Memory claim delta 已独立读原两个调用与新领域 helper：不再保泛型 model 参数，实际 writer 后 `next_updated_at` 经 RETURNING 返回原 DTO。对应 R3 C reviewer 的 `test_memory_claim_lease_starts_after_waiting_for_the_writer[memory|memory_batch]`，实际排队推进6分钟，旧两项red、新两项green，原job/event/attempts及complete token保留；其Memory组 **385 passed**。这是对方验证，不并入本报告自己的通过数。

## 覆盖等级

`F`：本轮全文读实现。`D`：全文读该文件全部 DB/外部效果边界方法，纯格式/类型余项按结构排除。`I`：核实际装配/间接消费者及调用 owner，未宣称全文。`S`：经完整路径 inventory、imports/calls/结构分类后排除的纯类型、渲染、协议 codec、测试 fake 或导出；不是逐字语义审查。分级反映真实阅读范围，不以 AST 等价于全文。

以下逐路径覆盖以原 D110 为固定全集；本轮额外范围另列。

覆盖计数：D 3 / F 31 / I 1 / S 75，合计110。

| 路径 | 等级 | 范围/排除理由 |
| --- | --- | --- |
| `src/yuki/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/agent_core/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/agent_core/loop.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/agent_core/model_boundary.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/agent_core/types.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/application/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/application/lifecycle.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/application/modules/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/application/modules/admin.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/application/modules/admin_api.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/application/modules/asr.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/application/modules/automation.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/application/modules/conversation.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/application/modules/media.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/application/modules/model_runtime.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/application/modules/persistence.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/application/modules/plugins.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/application/modules/web.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/asr/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/asr/audio.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/asr/provider.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/asr/schema.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/asr/service.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/cli.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/codemode/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/codemode/api_projection.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/codemode/capacity.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/codemode/contract.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/codemode/driver.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/codemode/driver_types.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/codemode/engine_monty.py` | I | 实际VM checkout/termination/restore生命周期；无SQL owner，其余wire/资源映射结构排除 |
| `src/yuki/codemode/limits.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/codemode/snapshot_binding.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/codemode/tool_visibility.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/config.py` | D | database/SQLite path、domain projection与startup相关配置；其余纯Settings声明结构排除 |
| `src/yuki/container.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/domain/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/domain/audio.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/domain/control.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/domain/conversations.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/domain/identity.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/domain/memory_config.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/domain/messages.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/domain/profiles.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/domain/tool_actor.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/event_prompt.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/health.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/http_environment.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/llm/__init__.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/anthropic_messages.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/base.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/deepseek_responses.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/fake.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/llm/gemini.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/http_errors.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/json_http.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/llm/openai_compatible.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/openai_responses.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/protocol_state.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/llm/vendor_policy.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/logging.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/main.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/operations/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/operations/reset_conversations.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/plugins/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/plugins/ai_chat/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/plugins/ai_chat/matcher.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/plugins/ai_chat/metadata.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/prompting/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/prompting/compiler.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/prompting/contracts.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/prompting/contributors.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/prompting/metrics.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/prompting/models.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/prompting/serializer.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/settings_domains.py` | D | Persistence/Automation/quiet相关字段与约束；其余纯Settings声明结构排除 |
| `src/yuki/time/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/time/formatting.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/time/models.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/time/schedules.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/time/service.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/tool_results/__init__.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/tool_results/access.py` | F | 全文；执行/调度/外部调用边界，无内部独占writer |
| `src/yuki/tool_results/artifacts.py` | D | 所有DB/文件/授权/GC方法；末尾纯JSON/文本展示函数结构排除 |
| `src/yuki/tool_results/recorder.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/tool_results/redaction.py` | S | 纯类型/导出/渲染/算法；imports与calls无DB owner，未宣称逐字全文 |
| `src/yuki/tool_results/schema.py` | F | 全文；实际owner/当前来源/回执/关闭边界见上文 |
| `src/yuki/vision/__init__.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/vision/base.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/vision/fake.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/vision/models.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki/vision/qwen.py` | S | 协议wire/数据结构或HTTP适配；模型付费入口已沿JSONHTTPProvider核，媒体服务调用无supplied writer |
| `src/yuki_plugin_sdk/__init__.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/api.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/context.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/errors.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/events.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/features.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/models.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/namespace.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/observation.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/permissions.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/plugin.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/registrar.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/results.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/sessions.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/testing/__init__.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/testing/contract.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/testing/fake_context.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |
| `src/yuki_plugin_sdk/testing/fake_services.py` | S | SDK协议/类型/命名/测试fake；无主库实现，实际host消费由其他域owner核 |

## 额外范围与跨域消费者

| 路径 | 等级 | 实际边界 |
| --- | --- | --- |
| `src/yuki/persistence/database.py` | F | creator、acquisition、reader/writer、commit三态、driver真实退出、hint、close；本轮修正见上 |
| `src/yuki/persistence/sqlite_connection.py` | F | 单 stop Future、原生 close 成功/失败及重复取消 |
| `src/yuki/persistence/unit_of_work.py` | F | supplied session 不开启或提交；owned reader/writer；实际时钟token |
| `src/yuki/persistence/commit_hints.py` | F | 确认根提交后提示；满队列断开；不生成可靠任务队列 |
| `src/yuki/persistence/sqlite_diagnostics.py` | F | 原生错误码和内存阶段计时；无holder状态与SQL内容 |
| `src/yuki/persistence/diagnostic_writer.py` | F | 准备、字节reservation、短SQL提交、取消drain、隐私条件与不重试 |
| `src/yuki/persistence/worker_recovery.py` | F | 仅SQLAlchemy或实际phase的迭代失败；取消不被吞，不授予效果重放 |
| `src/yuki/persistence/schema_guard.py` | F | 仅SQLite，版本/结构检查，无业务全库FK扫描；独立engine取消收尾 |
| `src/yuki/persistence/instance_lock.py` | F | Windows/Linux OS owner，实际文件锁持有/释放，非主库FIFO |
| `src/yuki/persistence/metadata.py` | F | 单一ORM聚合，各域schema导入；不偷偷建第二个Session factory |
| `src/yuki/persistence/artifact_refs.py` | F | 原segments投影、INSERT/UPDATE trigger与FK来源 |
| `src/yuki/persistence/event_repository.py` | I | 唯一额外 native driver 消费 `_history_read` 只interrupt，不裸close；其余A域owner |
| `src/yuki/persistence/job_claims.py` | I | 本轮Memory owner删除泛型，仅有两个实际Memory调用；合流已核 |
| `src/yuki/memory/job_claims.py` | I | 准入后真实next_updated_at/returning token，保原live event及job CAS；不复用旧prepared时间 |
| `src/yuki/memory/repository.py` | I | 唯二claim caller用returning映射替换DTO.updated_at；其余本轮Memory reviewer |
| `src/yuki/automation/__init__.py` | S | 导出，无DB实现 |
| `src/yuki/automation/authority.py` | F | 当前真实actor/权限与SELF场景 |
| `src/yuki/automation/context.py` | F | 原会话/primary alias只读绑定，无重建 |
| `src/yuki/automation/control_context.py` | F | 执行身份及supplied session复用 |
| `src/yuki/automation/creation_key.py` | F | 原Host invocation调用键，不按正文推身份 |
| `src/yuki/automation/gateway.py` | F | 纯投递错误及unknown事实 |
| `src/yuki/automation/models.py` | F | 唯一发生/状态/版本/计数投影与Schedule定义 |
| `src/yuki/automation/task_spec.py` | F | 完整任务准备/hash在writer外，原时间/目标 |
| `src/yuki/automation/repository.py` | F | 全读写方法、任务版本/审计、claim/lease、run/settlement CAS |
| `src/yuki/automation/service.py` | F | 全create/update/manage/read及来源准备 |
| `src/yuki/automation/tools.py` | F | 全工具声明/执行及typed异常phase传播 |
| `src/yuki/automation/worker.py` | F | 原发生→Main/Work、heartbeat、取消/失败/完成结算与释放 |
| `src/yuki/services/main_agent_backend.py` | I | 真实Automation binding的phase三态映射；产品归本轮C owner |
| `src/yuki/capabilities/results.py` | D | 实际render/archive/envelope消费链；原mutation/unknown/paid结果保留，纯展示函数未冒充全文 |
| `src/yuki/services/plugin_events.py` | F | lifecycle通知仅同步enqueue，捕获普通派发错误；不是外部await阻止DB close |
| `src/yuki/deployment_setup/service.py` | D | `apply_pending_plugins/_bootstrap_pending_plugins/load_plugin_setup_states`全部DB owner方法；其余UI/文件配置排除 |
| `migrations/env.py` | F | 原offline/online迁移、显式根事务、历史索引准备、finally dispose与取消 |
| `scripts/verify_work_backup.py` | F | 自有只读原生连接、原引用核验及明确close |
| `scripts/prepare_person_authority.py` | F | 显式旧副本只读export，不授予线上资格，明确close |
| `scripts/retire_media_snapshots.py` | D | 全DB扫描/参考/原manifest写与manager写边界，既有closing及外部文件核验；CLI纯装配排除 |
| `scripts/verify_monty_packaging.py` | D | application角色迁移及自有原生SQLite owner；native包装/镜像验证不是主库runtime，本机未运行镜像 |

本轮检查包括当前 `.venv` 的 aiosqlite 0.22.1 connect/stop 实现、SQLAlchemy async session/connection、pool checkout/listener、sqlite terminate/invalidate。它们是实际连接取消证据，不是新增第三方兼容机制。冻结 `migrations/versions` 无本轮改动，未将97个历史revision逐字重审当成已完成。

## 静态复读与排除

- 反复调用 Session.connection 复用同一事务/connection，不是第二次checkout。新 owned reader eager取得它，是新增一次Task边界；不能将此成本写成“零变化”。供给 session 不走该 owned入口，不额外提交。
- 当前 read_session 正常关闭不主动stop池中健康driver；只有SQLAlchemy已经开始physical_exit的真实错误路径，才先完成原Session invalidation再归还pool proxy。删除pre_ping没有靠手动裸关连接制造下一次失效。
- Schema/Alembic的屏蔽只涵盖各自本地metadata/迁移生命周期，没有把业务、模型、网络放进主库writer；原迁移事务可能完成，取消保持取消，当前head保实际事实。
- Artifact的清理unlink发生在原deleting围栏后，UUID文件名不复用；没有为普通GC取消再造孤儿journal、容量门槛或全局revision。保当前storage lock/file publication真实线程等待。
- 主程序OS锁、外部注入数据库、独立workspace/Manager/cache文件分别保owner。没有将SQLite FIFO宣传成跨数据库/跨进程协调。
- 没有新增paid journal、可靠hint、通用prepared plan、经验阈值或二次结果准入条件；没有删除原paid/receipt/source/version事实。

## R3 补充审查：指定 services 消费者

父任务在主范围收束时追加以下20个路径，作为本报告的 supplement，不计入 D110，也不宣称覆盖原C全部75个services文件。性能冻结窗口期间只读；窗口结束后按授权删除旧群管理空链和status空reader。

| 路径 | 等级 | 本轮实际内容 |
| --- | --- | --- |
| `services/admin/__init__.py` | S | 纯导出；旧GroupAdmin导出已删除 |
| `services/admin/config_admin.py` | F | 配置/审计委托同RuntimeConfigService，不自行开session或重试 |
| `services/admin/control_auth.py` | F | DecisionContext/当前principal能力谓词，无DB或模型 |
| `services/admin/group_admin.py` | F→删除 | 全部方法已无src/tests消费者，整文件及DI/导出已删除；仍用类型别名移GroupRecovery |
| `services/admin/group_recovery.py` | F | live route probe在writer外；writer核actor、Presence、原binding/route状态及registry connection，再同事务保存修改/audit/原request receipt |
| `services/admin/memory_admin.py` | F | 全部显式读、授权、变更、legacy组合写、prepared Memory接口和后台管理委托；supplied session沿actor/fact/audit复用 |
| `services/admin/preference_admin.py` | F | 原事实ID选定，set/delete进同MemoryMutation owner；无独立audit提交；无事实不新增写事务 |
| `services/commands/__init__.py` | S | 模块说明，无实现 |
| `services/commands/admin.py` | F | 原事件canonical request ID→CommandExecutor，typed catch只生成人类展示，不制造未提交事实或重试 |
| `services/commands/conversation.py` | F | status只读projection/统计；已删无scope的空reader，有scope保持原查询 |
| `services/commands/group.py` | F | 语法，不授予执行或数据库身份 |
| `services/commands/help.py` | S | 帮助/语法字典，结构排除，未冒称全文语义审查 |
| `services/commands/identity.py` | F | 稳定原内部事件ID/RequestId，复用现有operations及原revision/receipt |
| `services/commands/memory.py` | D | 全实际路由、管理/重建/Dream委托与typed catch；纯帮助和显示排版不冒称全文 |
| `services/commands/models.py` | F | read-only诊断repository，未知usage不当零；无模型调用 |
| `services/commands/plugins.py` | F | 原request+revision交现有plugin owner；不在命令层包writer或重派外部插件 |
| `services/commands/work.py` | F | 原Work ID与CommandExecutor回执，语法不生成新执行 |
| `services/plugin_sessions.py` | F | 实际reader身份、用户/assistant transcript写；模型在仓库事务外，父Work辅助付费沿原reserve；错误不形成再次模型调用循环 |
| `services/subagent_execution.py` | F | 原child Work/lease→bind_work_activation→共同Runner；终端取消用原run ID且在reader外，派生paid及finish交既有owner |
| `services/work_resume.py` | F | 原journal/receipt/source恢复、当前lease及generation；WorkConflict的phase传播和恢复失败留原异常；没有新模型重放层 |

继续追到 `admin/operations/base.py` 的 prepare_chat_command/database_command/external_command，原事务和外部效果的ACK路径仍点读原receipt；到 `runtime/work_control.py` / `work_supervisor.py` / `activation_outcome.py` 的真实恢复委托与failure分类，unknown不会获得SQLite BUSY重备资格。`profile_commands.py` 只在原Preference路径外作语法/权限装配。以上为跨域调用证据，未据此将这些额外文件整体计成全文审查。

明确排除：AdminChatCommands/MemoryCommands的泛“操作未完成”字符串不等于typed `mutation_committed=False`，它们不创建SDK执行结果、不重派调用、不修改receipt。不能为了所有人类提示新增phase框架；本轮未把此措辞当作数据库正确性缺口。原请求和回执仍由实际owner处理。

删除前全仓反查旧GroupAdmin类、两个公开方法、DI字段和SpaceAdminContext，仅见类定义/导出/装配/类型借用，没有src/tests业务消费；删除后旧引用全部清除，仍用类型别名在GroupRecovery定义。文档中的同名引用只在旧CHANGELOG、日期任务书和既有轮次审查表，均按当时基线保留，未将历史文档改成当前代码声明。真实群管理走 `operations.group.set_enabled`，恢复仍走GroupRecovery。

## 收束

已再读本轮所有产品delta、真实exception/physical exit和原receipt消费；GroupAdmin删除后的装配/type alias/status分支也复读，未留下已确认未修缺口。本报告的补充services审查和跨owner Memory delta边界保持单独列示；没有把纯格式结构分类当成全源码全文，也没有把本机8项Monty跳过写成通过。

本轮产品/测试修改已停止，等待父任务统一全仓验证与交付。`c0af6e82`已包含此前公共修正；之后GroupAdmin/status删减、本测试补项及本文由父任务统一提交。未自行提交、推送、提PR或部署；最终公平性能与整个任务是否完成，由父任务的总记录说明。

## R3 追加：W09、W37–W39 插件原 producer 收尾

父任务随后在原C全域补审中发现 `PluginManager._stop_one_unlocked` 的真实窗口：先从 `_running` 移除原实例，再写 `stopping`；这笔DB写失败或提交确认丢失时，没有进入原 `plugin.stop`、Host context close、后台任务 cancel/join。外层 stop 也随第一处异常退出，后续插件可能仍持有共享DB，而应用继续关闭资源。本节是该明确缺口的追加修复，不将前一收束点写成已经覆盖它。

| 路径 | 等级 | 本次追加实际边界 |
| --- | --- | --- |
| `src/yuki/plugin_host/manager.py` | F | 重新全文读 discover/definition/start/enable/disable/stop/rollback/background，修改原实例收尾所有权；没有新增重试、任务队列或持久状态 |
| `src/yuki/application/modules/plugins.py` | D | `register_lifecycle` 的插件启动/关闭顺序；PluginModule真实manager/context/repository装配 |
| `src/yuki/application/lifecycle.py` | F | 启动前先将entry纳入rollback集合；启动失败也关闭本次已开始的插件owner，且逐资源继续close |
| `src/yuki/plugin_host/facades.py` | D | `HostPluginContext.close_host_resources`、原Main tasks、SDK scheduler create/stop、实际SDK storage入口；其余原C范围仍由该轮owner审查 |
| `src/yuki/plugin_host/storage.py` | F | BoundStorageFacade向真实PluginStateRepository委托，不在SDK层另建数据库owner |
| `src/yuki/plugin_host/repository.py` | D | installation get/status/failure与state get/set真实调用，普通状态写走公共writer；无外部plugin调用在writer内 |

修复保留原生产者：`stopping` DB失败仍完成这个已经取出的原实例，Host context和原managed tasks在finally链收尾；公共 `_complete` 只等待同一次原收尾完成，记住调用者重复取消。批量stop继续收尾其余原实例，再抛第一处原异常对象；后续清理错误只加备注，不替换原phase或将unknown说成未提交。DB异常不增加插件failure_count，也不因元数据失败重调plugin.start/stop。

相邻启动路径也有两个实际问题。starting/running等元数据写的原SQLAlchemy异常或带公共phase的OSError曾落入插件业务失败分支；现在清理本次实例后原样抛出，保原enabled、failure_count和DB三态。真实plugin.start取消后，原rollback的stop等待、context关闭和任务join还可被再次取消截断，最后`approved`写失败也会覆盖最初取消；现仅把本次rollback作为一次完整收尾，保最初CancelledError，清理/状态错误作为备注。普通插件业务失败及原超时设置继续沿原合同，不新增状态机或经验阈值。

新增回归在既有 `tests/unit/test_plugin_runner_isolation.py`，不是模拟空容器：

- 两个实际加载、批准、启用的插件，各有SDK scheduler任务和注册managed task；四个原任务均先通过真实SDK storage写共享DB，再等待关闭。SQLite trigger实际拒绝`stopping`、真实commit后丢OSError确认、真实commit后取消三种路径，均验证两插件stop一次、原四任务全部完成、context关闭、注册表清空、原DB phase/committed事实保留。
- `starting`实际SQLite trigger和真实commit后OSError，两项原先被吞成插件业务失败，修后保原异常和phase，context已关闭、enabled保留、failure_count为零。
- 两插件真实启动后的外层取消遇到`stopping` trigger失败，继续清理原全部实例并保同一个最初取消。
- 真实plugin.start和SDK任务barrier中的取消：`approved`实际SQLite trigger原先把CancelledError覆盖成IntegrityError；重复取消原先打断plugin.stop barrier。两项均先红后绿，现保第一次取消参数、原stop完成、SDK原任务退出、context与注册表清理。

初始stop的execute/commit和starting的execute/commit也已在修复前确认红，失败是原资源未收尾或原DB异常被吞；fixture未成功启动造成的配置/权限错误未记作产品红证据。最终目标集为runner isolation、offline configuration、admin write certainty、retired approval permissions、frozen binding五文件：**19 passed / 1 skipped，2.94秒**。跳过仅为未构建本地pinned Monty；不写成通过。manager单文件mypy、两文件Ruff/format及`git diff --check`均通过。此前321项是上段主范围验证，目标集有重合，本节不作唯一用例总数相加。

再次静态复读了本次manager完整delta及真实caller：Application Lifecycle已经包含失败中的entry，普通DB启动错误由原lifecycle执行stop；取消路径在manager内部也保原owner。原metadata写结束后才运行plugin外部stop，不持主库writer等待；没有新模型、消息、外部paid重放或独立owner。未留下本节已确认未修缺口。源码和目标测试已冻结，已通知父任务并停止本地测试；后续只补文档，等待统一性能、提交与交付。
