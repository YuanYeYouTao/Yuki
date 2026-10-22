# SQLite 实现后复审 R2：公共运行时与资源所有权

起点：`f8fbe14dc2b78a4160d004e714d7f272641a6b40`。这是实现后的第二轮，上一轮由 root 负责的范围在这里独立轮转复核；10 月 22 日的设计审查不计入实现后三轮。

先按冻结源码只读复核。在正式 paired 性能窗口结束后，才修改下列产品和测试；没有运行真实模型、生产命令或平台消息。下文区分自己的修改与交给本轮 C owner 的修复。

## 本轮发现与修复

### 1. connector 取消会丢失实际 native 连接

`Database._connect_sqlite` 原来直接 `await driver`。aiosqlite 0.22.1 的 `_connect` 在被取消时先取消它等待的 Future、调用 stop 并将 `_connection` 清空；线程中的 `sqlite3.connect` 随后仍可能返回连接。已取消的 Future 不接收结果，而 stop 看到空 `_connection`，不能关闭那一条真实 native 连接。只等待 stop Future 不能证明这条连接关闭。

改为复用 `_complete(driver)`：先让 connector 结果归属同一个 driver，再沿已有 stop/physical_exit 收尾，最后传播原取消。没有全局连接注册表、观察 hook 或额外持久状态。

### 2. 完整 checkout 的取消边界需要包含 PRAGMA 与 pre_ping

只保护 creator 还不够。SQLAlchemy pool `__connect` 在 creator 返回后执行 connect listeners；`_checkout` 还可执行 pre_ping。这时 immediate_session 尚未拿到 raw.driver。取消/重复取消会进入 SQLAlchemy terminate 的 force-close 分支，后者调用 driver.stop 却不等待其 Future。

完整 `session.connection()` 与 raw 获取沿同一 `_complete` 完成，取得实际 driver 后再处理已捕获的取消。业务 `yield` 仍在原协程和原 ContextVar 中，不将业务 callback 放进屏蔽任务。正常连接继续复用，FIFO 仍在 checkout 前。

### 3. owned reader 的退出不能早于自己的实际 rollback

SQLAlchemy AsyncSession.__aexit__ 只建立一个 shielded close task，调用方在真实 rollback 阻塞时取消，可先退出而 close 留在后台。reader 入口改为显式拥有原 Session，并复用 `_complete(session.close())` 收尾；没有 eager checkout、写锁、自动 commit、只读拦截器或新 Session 子类。

新增真实文件/线程回归最初 4 项全部失败：native close 未发生；configure/pre_ping 被取消后丢连接、重建，且 pre_ping 出现 non-checkin GC 警告；reader 在 rollback 前完成。修复后 4 项与既有 14 项 writer 故障回归全部通过。红测泄漏的旧线程只存在于该隔离测试进程，未操作产品进程。

### 4. 生产 create 创建的 Database 被错当成外部注入

`ApplicationContainer.create` 创建 Database 后传入构造器，PersistenceModule 仅按“参数非 None”判为外部资源，生产 close 没有实际 owner 关闭它。现在由真实创建者显式传递 owns_database；直接外部注入仍由原调用方关闭。

create 初始化异常/取消使用 BaseException 收尾原 Database。原先在 lifecycle.start 前执行的身份 bootstrap/角色读取，纳入原 registry 的 application_identity start hook，位置在 database owner 后、所有业务 worker 前。其失败沿原逆序回滚关闭已创建 owner；没有新 registry、started 补丁或 fixture 运行时门禁。

真实 create→完整 start→close、外部注入仍可读写、create 在真实 override 读取后取消、无 Character 的真实启动失败，共 4 项通过，并核实际 driver.physical_exit 和 native connection 已关闭。

### 5. Automation 层不能吞掉带数据库阶段的 ValueError

AutomationToolService 原 except(PermissionError, ValueError) 总生成 mutation_committed=False，可能将真实根提交后 ACK 丢失当作未提交，还补失败创建审计。现在只有已确认回滚的领域失败继续原逻辑；带 phase 且未证明回滚的原异常交原执行边界。

真实调用链是 Chat 注册的 Automation binding→MainAgentBackend，并不经过 AgentTools。将统一边界按公共 committed/phase/retryable 事实映射的漏项交给本轮 C owner A 修复，产品已在共享树。这里新增真实 Main binding 回归：create/pause 实际根 commit 后 ValueError 丢 ACK，最终保 uncertain=True、mutation=None、原 commit phase、不重试；实际任务已创建/暂停，总 commit 仅 1 次。

### 6. 离线资源收尾

两个只读原生 SQLite 工具改用 closing：`scripts/verify_work_backup.py`、`scripts/prepare_person_authority.py`。sqlite3.Connection 上下文只结束事务，不负责 close；新增 2 项持有实际 connection 的回归，工具返回后原 connection 已不能查询。

`migrations/env.py` 的 async engine dispose 纳入 finally；未改任何冻结 revision、迁移 SQL、版本号或数据库结构。

## 独立核查后排除的候选

- CLI `_runtime_snapshot` 仍调用 RuntimeConfigService.initialize，但当前 initialize 已是 `repository.list_all()` reader 和进程 restart 投影，无 seed/DML。删除它反而丢失 restart override 诊断，不按旧任务书机械删；本轮没有改 CLI。
- `WorkspaceStore.snapshot` 的独立 manifest.sqlite3 在最终 writer 内仅 lstat/新 UUID 的本地 os.replace 与元数据。正文复制、hash、flush/fsync 在外；保原短最终发布边界，不为“零文件调用”扩新 GC 协议。它不占主库 FIFO。
- participation SnapshotStore 是独立 sqlite 文件，AsyncSnapshotStore 用单专用线程建立/读写/关闭；save 的 JSON 编码先于 BEGIN，CAS 与原总容量核验同提交。取消的 queued work 不消失，close 排在实际任务之后。没有并入主库队列或改 snapshot revision。
- benchmark 脚本保对旧基线 factory/apply_snapshot_write 的受控适配，以同一个 harness 比较两版本；它不是生产兼容层。性能脚本和最终 paired 结果由 root 拥有，不在这里改。
- 旧 cache/migration 演练与 Monty 打包脚本的原生连接还使用事务上下文或正常尾部 close；它们是显式副本/隔离进程工具，本轮只核入口和数据范围，不扩大成历史实验工具的收尾重构。不能用本轮在线 owner 修复声称这些历史工具的异常退出也已验证。

## 实际边界复核

公共层逐项核 `_complete`、SQLiteConnection.stop/close、read_session/immediate_session、write_failure 三态、close、CommitHints、UoW、DiagnosticWriter/diagnostics、job claims 和 Schema guard。rollback/flush/commit/physical-exit 的确认事实不按错误名称重建；commit 失败 ACK 即使 BUSY 形状也 unknown；只有确认未提交的原生 BUSY 才 retryable。正常 cleanup 不推翻已确认 commit，最后 hint 在 owner 释放后同步发布。

Automation repository 全部实际读写方法、service 的 create/update/management/self 来源 helper、worker 的 claim→run→Work/原回执→结算与 lease heartbeat 已读。准备的 script/schedule/authority 在 writer 前编码；当前 owner/状态/version/原 run CAS 在同一 writer。claim 按 128 页短写，lease 取准入后时间；普通 task 时间仍是原请求时间。版本+task+审计、run+计数/下一次计划分别保原子单位；未知不重派模型或外部效果。无 supplied session 的 `_creator_context` 分开两个 reader，不在 chat 主热链，本轮未以缓存替代实际权限读取。

Artifact 的写入、媒体、读取授权、refs、清理/孤儿路径全部 DB 方法已读。T0 文件编码/hash/publication 在主 writer 外；storage owner 保到实际文件线程结束。未知提交不 unlink；GC 保 reference/deleting 两阶段和实际 128 页。读取文件前后核原授权/隐私/代际；保护祖先 Work 的保留条件不扩大读取权。纯 JSON 页格式函数只作结构分类。

Codemode 的直接 reader `_admitted/_vm_value/_parent_row/_live_source`、snapshot T0/T1、effects/dispatch/result 委托实际 owner 已追踪：SQL 在原 Work/Protocol owner，VM、dump、文件准备不在 supplied writer 回调里；未知子调用仍读原回执，不重跑 VM。WorkWait/notification 的额外独立挑战按轮转边界交本轮 C owner，不在这里双改。

Schema：当前单 head `0147`，97 个 revision；`c2ce7da6..f8fbe14d` 的 migrations/versions 无差异。metadata 聚合的主表、FTS、quota、projection、artifact refs/FK 与启动结构验证已核入口和实际触发成本；没有宣称逐字重审全部冻结迁移。普通启动只核版本/结构，不扫描业务全库 FK。

主程序 OS lock→Schema guard→同一个应用 Database；CLI 和离线 reset 是原进程/调用者自己拥有的实例，FIFO 不能跨实例或跨进程。reset 的 generation/floor/SELF 原 authority 新版本/reset receipt 同提交，原 batch 重入不重置；没有为离线诊断、迁移或 reset 加在线停机门禁。

独立资源另核 Manager jobs.sqlite3/CompletionOutbox、workspace manifest、sticker search.sqlite3、SDK Outbox、Web BridgeState 与部署 setup 的 Database finally。Manager 的 launch intent/terminal+completion 原事务保留，网络/终端等待在 SQLite 块外；completion 回执不按普通缓存清空。客户端 Outbox 不当 server Work，Web/贴纸缓存不当聊天原件。各文件实际 owner/锁序保留，不宣称统一 FIFO 覆盖所有文件。

## 验证与剩余交付

- `test_database_acquisition_cancellation.py` + `test_database_writer.py`：18 passed，0.73 秒。
- 12 个生命周期/Automation/Artifact/Schema/离线既有文件：79 passed / 5 skipped，43.04 秒。包括新的 lifecycle 4 项和真实 Automation binding 2 项。
- 离线三处收尾后补跑 backup/authority/Schema：12 passed / 5 skipped，15.91 秒；这是重跑，不另算唯一通过数。
- 原生离线 connection owner 新回归：2 passed，0.67 秒。
- 5 个跳过均为本机未构建 pinned Monty worker/binding；不把它们当 Linux/native 已验收。既有 schema 反射/环形 FK 和 Python datetime adapter 警告保原事实。
- 4 个主要产品文件独立 mypy cache 检查通过；本轮 7 个产品/离线文件和 4 个测试文件 Ruff 与 format 检查通过，git diff --check 通过。
- 本轮修正确认的实际缺口；主热链性能削减与修正 probe 后公平 paired 对照由 root 继续，不把先前 +34ms 测量写为最终结果，也不宣称新版已快于旧版。
- 尚未进入 R3、未提交/推送/提 PR/合并/上线。这里只是 R2 的公共/装配范围结果。

## 可复核的源码位置

| 位置 | 事实 |
| --- | --- |
| `src/yuki/persistence/database.py:75`、`:134`、`:157`、`:292`、`:411` | 一个 `_complete`，分别收 connector、reader close、writer acquisition/事务收尾与池 dispose |
| `src/yuki/persistence/sqlite_connection.py:25` | 保留实际 stop Future；正常 writer 不逐次 stop 连接 |
| `src/yuki/application/modules/persistence.py:55`、`src/yuki/container.py:92`、`:810`、`:965` | 实际 Database 创建者、显式 owner 交接与原 registry 的 bootstrap hook |
| `src/yuki/automation/tools.py:407` | 带阶段且未证明回滚的异常不被业务 ValueError 分支吞掉 |
| `src/yuki/admin/config_service.py:612`、`src/yuki/cli.py:403` | 当前 initialize 是 reader + 进程 restart 投影，CLI 旧写候选已排除 |
| 已安装 `aiosqlite/core.py:177`、`:90`、`:63` | `_connect` 结果归属、stop、取消 Future 的真实线程处理；版本 0.22.1 |
| 已安装 `sqlalchemy/pool/base.py:888`、`:1265`；`connectors/asyncio.py:397` | configure/pre_ping/terminate 的真实取消路径 |
| 已安装 `sqlalchemy/ext/asyncio/session.py:1068` | AsyncSession 原 __aexit__ 单次 shield close；真实 rollback 阻塞回归验证差异 |

## D 域逐文件分类

F=全文已读；M=全部真实 DB/owner/生命周期方法与消费者已读，纯格式/构造分支结构分类；I=接口/间接委托，已核实际 owner 与排除依据；S=AST/import/call/差异或 Schema 定义分类，不冒称全文。所有 110 个路径逐项列出；没有真实数据库方法只以 I/S 代替实现复核。

计数：F=20，M=6，I=18，S=66，合计 110。

| 路径 | 等级 | 实际覆盖 / 排除依据 |
| --- | --- | --- |
| `src/yuki/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/agent_core/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/agent_core/loop.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/agent_core/model_boundary.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/agent_core/types.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/application/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/application/lifecycle.py` | F | 实际数据库/生命周期/来源与回执方法全文，结论见正文 |
| `src/yuki/application/modules/__init__.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/admin.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/admin_api.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/asr.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/automation.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/conversation.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/media.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/model_runtime.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/persistence.py` | F | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/plugins.py` | M | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/application/modules/web.py` | M | 实际共享 owner 装配/生命周期已核；无自行创建第二主 Database |
| `src/yuki/asr/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/asr/audio.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/asr/provider.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/asr/schema.py` | S | DDL/trigger 定义已读；无运行时 session 或独立 writer |
| `src/yuki/asr/service.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/cli.py` | M | 全部 Database 创建/诊断/初始化/Memory 命令及 finally；提示/参数渲染作结构分类 |
| `src/yuki/codemode/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/codemode/api_projection.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/codemode/capacity.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/codemode/contract.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/codemode/driver.py` | M | 全部直接 reader、snapshot publication、原 effects/dispatch/receipt 委托；VM 纯执行分支作结构分类 |
| `src/yuki/codemode/driver_types.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/codemode/engine_monty.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/codemode/limits.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/codemode/snapshot_binding.py` | F | 实际数据库/生命周期/来源与回执方法全文，结论见正文 |
| `src/yuki/codemode/tool_visibility.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/config.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/container.py` | M | 完整核 create/start/close/cleanup 与 owner 注册；其余构造和插件 facade 按调用委托分类 |
| `src/yuki/domain/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/domain/audio.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/domain/control.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/domain/conversations.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/domain/identity.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/domain/memory_config.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/domain/messages.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/domain/profiles.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/domain/tool_actor.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/event_prompt.py` | I | 只消费 EventRecord 进行渲染；查询归原 ledger reader |
| `src/yuki/health.py` | F | 实际数据库/生命周期/来源与回执方法全文，结论见正文 |
| `src/yuki/http_environment.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/anthropic_messages.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/base.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/deepseek_responses.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/fake.py` | F | 真实 dispatch guard 委托；无数据库连接或写入口 |
| `src/yuki/llm/gemini.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/http_errors.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/json_http.py` | F | 实际 paid/dispatch/accounting 回调与 HTTP 重试边界；无持有数据库事务跨网络 |
| `src/yuki/llm/openai_compatible.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/openai_responses.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/protocol_state.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/llm/vendor_policy.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/logging.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/main.py` | F | 实际数据库/生命周期/来源与回执方法全文，结论见正文 |
| `src/yuki/operations/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/operations/reset_conversations.py` | F | 实际数据库/生命周期/来源与回执方法全文，结论见正文 |
| `src/yuki/plugins/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/plugins/ai_chat/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/plugins/ai_chat/matcher.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/plugins/ai_chat/metadata.py` | F | 实际数据库/生命周期/来源与回执方法全文，结论见正文 |
| `src/yuki/prompting/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/prompting/compiler.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/prompting/contracts.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/prompting/contributors.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/prompting/metrics.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/prompting/models.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/prompting/serializer.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/settings_domains.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/time/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/time/formatting.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/time/models.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/time/schedules.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/time/service.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/tool_results/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/tool_results/access.py` | F | 实际数据库/生命周期/来源与回执方法全文，结论见正文 |
| `src/yuki/tool_results/artifacts.py` | M | 全部存储/授权/refs/发布/GC 方法全文；纯 JSON 页格式函数作结构分类 |
| `src/yuki/tool_results/recorder.py` | F | 实际数据库/生命周期/来源与回执方法全文，结论见正文 |
| `src/yuki/tool_results/redaction.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/tool_results/schema.py` | S | DDL/trigger 定义已读；无运行时 session 或独立 writer |
| `src/yuki/vision/__init__.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/vision/base.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/vision/fake.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/vision/models.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki/vision/qwen.py` | S | AST/import/call/差异分类：纯数据类、Provider 编解码、提示、配置或无数据库入口；不冒称全文 |
| `src/yuki_plugin_sdk/__init__.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/api.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/context.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/errors.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/events.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/features.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/models.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/namespace.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/observation.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/permissions.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/plugin.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/registrar.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/results.py` | F | 结果 mutation 三态定义已读；没有数据库 owner |
| `src/yuki_plugin_sdk/sessions.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/testing/__init__.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/testing/contract.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/testing/fake_context.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |
| `src/yuki_plugin_sdk/testing/fake_services.py` | I | SDK 接口/类型/Host 委托或 fake；AST 无数据库创建、session 或 SQL执行 |

## D 表之外的实际复核路径

| 范围 | 覆盖 |
| --- | --- |
| persistence/database.py、sqlite_connection.py、unit_of_work.py、commit_hints.py、sqlite_diagnostics.py、diagnostic_writer.py、job_claims.py、worker_recovery.py、schema_guard.py、instance_lock.py、metadata.py | F：当前完整实现/定义，含实际第三方连接收尾源码 |
| automation/repository.py、service.py、worker.py、control_context.py、context.py | F：全部真实 DB 和恢复方法 |
| automation/tools.py、authority.py、task_spec.py、creation_key.py、models.py、gateway.py、__init__.py | M/S：工具调用、错误映射、准备及纯合同；无另一个 DB owner |
| services/participation_snapshot.py、已安装 yuki_participation/store.py（pin addfcdde） | F：独立线程/SQLite/CAS/关闭全文，不改库 |
| sandbox/persistent.py、completions.py | M/F：全部实际 DB、owner/receipt、恢复、文件请求/close；纯 Docker/PTY 业务按结构排除 |
| workspace/store.py、emoji/sticker_index.py | M：全部连接/事务/发布/GC/索引方法，未扩为主队列 |
| client_sdk/outbox.py、web/bridge_state.py | F：独立 SQLite 实现全文 |
| deployment_setup/service.py | M：两个实际 Database 创建/调用/finally，其他部署交互结构分类 |
| admin/config_service.py | M：initialize/snapshot/list_all 当前 reader 与 restart 投影，仅用于排除 CLI 旧候选 |
| migrations/env.py、scripts/verify_work_backup.py、scripts/prepare_person_authority.py、scripts/start.sh | F：实际迁移/只读/启动 owner，全正常与异常退出 |
| migrations/versions/*.py + script.py.mako | S：97 revision 链/单 head/不变差异与依赖导入；不宣称当前全文重读全部历史 SQL |

## 操作脚本分类

以下逐路径核 AST、调用目标和运行方式；F/M 项额外读实际 DB/资源方法。历史 probes/证据实验、JSON 旧报告和示例不被冒称生产 writer，未运行它们。

| 路径 | 等级 | 边界 |
| --- | --- | --- |
| `.env.example` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `Dockerfile` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `alembic.ini` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `deploy/sandbox/configure-manager.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `deploy/sandbox/connect-host-proxy.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `deploy/sandbox/validate_runtime.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `docker-compose.yml` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `pyproject.toml` | M | aiosqlite >=0.22.1,<1 与 uv 0.22.1、参与库 pin 一致 |
| `scripts/__init__.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `scripts/benchmark_sqlite_writer.py` | I | root 的同 harness 性能比较；受控旧版本 API 分支不是生产旧入口 |
| `scripts/build_codemode_seccomp.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `scripts/build_release_bundle.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `scripts/evaluate_search_memory_gold.py` | M | 仅离线副本/评测/打包；已核真实 DB 方法与收尾，不纳主应用队列 |
| `scripts/export_monty_notices.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `scripts/prepare_person_authority.py` | F | 实际 owner/连接/收尾或启动顺序全文，见正文 |
| `scripts/release_smoke.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `scripts/release_validate.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `scripts/retire_media_snapshots.py` | M | 仅离线副本/评测/打包；已核真实 DB 方法与收尾，不纳主应用队列 |
| `scripts/start.sh` | F | 实际 owner/连接/收尾或启动顺序全文，见正文 |
| `scripts/verify_monty_isolation.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `scripts/verify_monty_packaging.py` | M | 仅离线副本/评测/打包；已核真实 DB 方法与收尾，不纳主应用队列 |
| `scripts/verify_work_backup.py` | F | 实际 owner/连接/收尾或启动顺序全文，见正文 |
| `tools/cache_probe_materials.py` | M | 仅离线副本/评测/打包；已核真实 DB 方法与收尾，不纳主应用队列 |
| `tools/cache_probe_replay.py` | S | AST/配置/调用分类；无主库运行时写者或仅部署/构建/诊断 |
| `tools/rehearse_current_material_migration.py` | M | 仅离线副本/评测/打包；已核真实 DB 方法与收尾，不纳主应用队列 |
