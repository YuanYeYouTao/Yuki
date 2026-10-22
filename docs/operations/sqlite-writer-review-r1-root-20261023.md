# SQLite 实现后复审 R1：公共边界与全局装配

起点：`109cbd40274ca8db1a0b381ac2db1e35c3cf1df3`，修复位于下一次 R1 冻结提交。
这是实现后的第一轮，实施前的三轮设计审查不计入这里。全局 R1 还要合并 A/B/C 交叉报告，不能拿本报告单独声称三轮完成。

## 实际审查范围

公共 Database/driver/UoW/hints/diagnostics，Automation 全链，WorkWait/通知同提交边界，
Self repository 的准备与逐结果结算，tool_results 文件和回执，应用装配/生命周期/健康，
CLI/离线 reset、Schema 与独立资源。Memory/Self 的交叉复审认 A 报告，Work/Social/插件认 B，
身份/控制面与独立小库认 C；不把静态调用命中当成语义审查。

D 域所有 110 个源码路径列在下表。D=实际数据库、生命周期或组合方法审查；I=接口/间接委托，
查其实际 owner；S=导入、AST、调用与基线差异分类后排除，不冒称每轮全文阅读。
公共 persistence、Automation、Self 和 WorkWait 虽分属静态 A/B/C，另在本轮实际审查范围，不被 D 表漏掉。

## 发现与修复

1. 外部注入 Database 的测试原来通过关闭应用来 dispose，随后还复用同一 owner。产品生命周期现按创建者拥有；
   冷恢复 fixture 改为只 dispose 连接池，真正的外部 owner 最终 close。SOUL reference 子进程漏关外部 Database，
   导致退出后 aiosqlite 线程仍在，已补实际 owner 关闭，原 10 秒退出测试通过，没有放宽超时。
2. Automation pause/resume/cancel/run_now 的完整 task 预读与重复 TaskSpec 校验没有消费者，已删；
   当前权限、状态和原身份仍在同 writer。更新审计需要原 before_hash，不能从已被 publish 改过的 ORM 再读。
   renew 的当前时间改在排队后取得，队列等待不延长原 lease。
3. 离线 canonical reset 先用原 conversation/generation/SELF/status SQL 定位，再解析真正要更新的 authority。
   保原版本，新增版本与 generation/reset receipt 同提交，原 batch 重入不再重置。
4. WorkWait 旧条件、Work source 与 Sandbox completion 的解码仍在 writer。已改 read preparation，
   supplied writer 必须使用原准备并在首次 DML 前证明实际 wait/source/member/absence，未保留旧解析 fallback。
   Sandbox 完成体证明只返回 count/bool，按真实绑定预算分段，原 unknown 结果不被改成成功。
   删除重复的扫描后整页再读取/解析，普通未匹配入站仍只开一次 reader，不启动媒体准备或 writer。
5. WorkWait event 当前判断只取实际身份/代际/方向/事件类型等元数据，不加载旧正文；
   当前 conversation 与所准备的原 wait 对齐，保事件内部 ID、原 input key 与单次投递。
6. Artifact 文件 publication 原来的 to_thread 被取消后会先释放 storage owner。现等待实际线程退出再传播取消，
   重复取消不提前放锁；未登记 UUID 走原孤儿清理，提交未知不 unlink 可能已拥有的文件。
   没有新文件账本、mtime 推测提交或业务重跑。metadata TTL 在实际准入后计算。
7. 公共 _complete 在取消与已完成 operation 异常同时可见时保原取消链；读入口不加入写锁、自动提交或自定义 Session 拦截。
   关闭/rollback/物理退出失败与 commit unknown 的实际分类保留；成功根 commit 的 hint 在释放 owner 后同步发布。
8. 测试旧 factory 迁移时，两处本来显式 BEGIN 的一致 reader 被误当 writer，已改回 reader；
   查询规模测试改测现行 owned-run 读取，保“不随无关兄弟历史线性膨胀”的原复杂度断言。
9. 沿所有 `validate_observations` 消费者确认 DAG 遍历只在 EventLedger/WorkSourceGuard 的 reader；
   journal writer 的 cross-source 核验不隐开 reader。Projection publication 的旧 `IN(existing_keys)`
   与已经备好的 wanted JSON 重复，且大选择集会耗尽 native bind budget；删 IN 和 outer join，
   用原 JSON inner join 做完整成员/字段证明。350+ 已选来源在真实 SQLite limit=300 下重建通过，
   与 source invalidation 同组 20 项通过。现行 chat-history 合同同步去掉旧 BUSY_SNAPSHOT 升级流程。
10. 最小 aiosqlite 依赖原为 0.20，低于实际使用的 stop/physical-exit 接口。将下界提高到已验证的
    0.22.1，uv 仍锁定同一版本；没有增加旧驱动 fallback。

## Schema、锁序和独立 owner

97 个 revision（连 env/template 共 99 项）的链仍到 0147，本轮没有修改冻结迁移或数据库结构。
现行 FTS、quota、projection、artifact refs 触发器与 FK 仍由原同提交承担，成本计入事务。
启动 OS lock→schema guard→同一应用 Database；关闭先停止业务与必要结算、排空诊断，再关闭应用自有 Database。

Artifact storage→writer，Protocol 文件准备/发布→原 owner writer，SOUL/config 文件 owner→writer；
引用 helper 不反向取得 storage lock。SELF quiet/model slot/平台与文件 I/O 不在主库事务内等待。
独立 participation snapshot 保单专用线程/CAS，Manager jobs、SDK Outbox、web bridge、workspace/index
保实际文件/进程边界，不把主库的内存 FIFO 宣称为跨进程锁，也不合并不同 SQLite 文件。
跨库 feedback 沿原主库 seq/receipt 与 snapshot revision 推进。

## 已执行验证和未完成项

- Database 14 项真实文件 WAL/worker barrier，Artifact/refs 同组共 20 项通过。
- WorkWait/settlement/入站负路径/查询规模同组 76 项通过，含 70 个真实 Sandbox completion、
  SQLite native variable limit=256、准入前 source 变化后保原 wait、恢复后只发一个原 input。
- ACP/Wait/notification 联合 226 项通过（B 执行）；Automation 管理与历史组 97 项、
  新 lease/审计回归 3 项通过。离线 reset 新回归 1 项与 SOUL reference 1 项通过。
- 全量 integration 首轮重跑 795 passed / 6 failed / 22 skipped / 93 deselected：
  1 个 SOUL 子进程退出问题已按上文修复；其余 5 项要求 Windows 不提供的 fcntl，留给 Linux CI。
- unit 首次到 72% 时 2433 passed / 15 failed / 13 skipped，实际迁移与 fixture 问题已定向修复；
  余下部分 851 passed / 2 failed / 105 skipped，两项 WorkWait 测试随后在 76 项中通过。
  这些分段结果不相加冒充一遍全量绿，未执行的 POSIX/native 行为仍需 Linux CI。
- 随后在本机 WSL Debian 的隔离 Python 3.12 环境补跑 shared inbound media、sticker collection/
  preview 三个文件：32 项通过，覆盖前述因 fcntl 缺失而未运行的行为；全量 Linux/native 仍待最终验证。
- 全仓 Ruff/mypy 在初次实现冻结通过；R1 后再次全仓检查与正式相同负载性能对照待全域冻结。
  一次共享 mypy cache 并行检查出现内部错误，后续使用各自 cache，不把工具内部错误当产品失败。
- 尚未提 PR、合并或部署。本轮未发现而未交接的公共代码修复项为零；性能与后续两轮独立复审尚未完成。

## D 域逐路径分类

| 路径 | 等级 | 实际边界 |
| --- | --- | --- |
| `src/yuki/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/agent_core/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/agent_core/loop.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/agent_core/model_boundary.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/agent_core/types.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/application/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/application/lifecycle.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/application/modules/__init__.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/admin.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/admin_api.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/asr.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/automation.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/conversation.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/media.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/model_runtime.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/persistence.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/plugins.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/application/modules/web.py` | D | 共享注入、owner、启动关闭顺序；无第二主库实例 |
| `src/yuki/asr/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/asr/audio.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/asr/provider.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/asr/schema.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/asr/service.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/cli.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/codemode/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/codemode/api_projection.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/codemode/capacity.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/codemode/contract.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/codemode/driver.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/codemode/driver_types.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/codemode/engine_monty.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/codemode/limits.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/codemode/snapshot_binding.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/codemode/tool_visibility.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/config.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/container.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/domain/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/domain/audio.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/domain/control.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/domain/conversations.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/domain/identity.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/domain/memory_config.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/domain/messages.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/domain/profiles.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/domain/tool_actor.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/event_prompt.py` | I | EventRecord 渲染；查询归 ledger，不是 writer |
| `src/yuki/health.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/http_environment.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/anthropic_messages.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/base.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/deepseek_responses.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/fake.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/gemini.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/http_errors.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/json_http.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/openai_compatible.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/openai_responses.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/protocol_state.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/llm/vendor_policy.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/logging.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/main.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/operations/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/operations/reset_conversations.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/plugins/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/plugins/ai_chat/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/plugins/ai_chat/matcher.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/plugins/ai_chat/metadata.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/prompting/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/prompting/compiler.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/prompting/contracts.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/prompting/contributors.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/prompting/metrics.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/prompting/models.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/prompting/serializer.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/settings_domains.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/time/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/time/formatting.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/time/models.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/time/schedules.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/time/service.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/tool_results/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/tool_results/access.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/tool_results/artifacts.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/tool_results/recorder.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/tool_results/redaction.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/tool_results/schema.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki/vision/__init__.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/vision/base.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/vision/fake.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/vision/models.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki/vision/qwen.py` | S | 导入/AST/调用和差异分类；纯协议、类型、Provider、渲染或无数据库入口 |
| `src/yuki_plugin_sdk/__init__.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/api.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/context.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/errors.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/events.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/features.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/models.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/namespace.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/observation.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/permissions.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/plugin.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/registrar.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/results.py` | D | 数据库/生命周期/来源与恢复方法；正文见本轮边界和发现 |
| `src/yuki_plugin_sdk/sessions.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/testing/__init__.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/testing/contract.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/testing/fake_context.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
| `src/yuki_plugin_sdk/testing/fake_services.py` | I | Host 委托与结果合同；fake/types 不创建数据库 |
