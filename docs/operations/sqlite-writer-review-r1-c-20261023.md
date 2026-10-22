# SQLite writer 实现后交叉复审：第 1 轮，原 A 域

基线：`109cbd40274ca8db1a0b381ac2db1e35c3cf1df3`。日期：2026-10-23。
审查者此前实施 C 域，本轮轮转原 A 域。本文只记录实现后的第 1 轮，不把 10 月 22 日的设计审查计作实现复审。
本轮允许直接修复，未提交、推送、访问生产、运行真实模型或发送平台消息。

候选来自该基线重新生成的 `surface.json`：原 A 域现为 **240 个 source 文件**，不是旧清单的 239 个。
下表逐路径区分全文、数据库方法、间接调用、结构排除和转交其他 owner。
AST 用于核候选全集和间接调用，不代替函数行为审查；未全文读取的文件不记为全文。

## 已修正的问题

| 编号 | 实际问题与改动 | 任务 / 场景 |
| --- | --- | --- |
| A-R1-01 | 8 个 `preparation_changed` 捕获循环只看异常类型便 `continue`。现在仅在公共 `write_failure_committed(exc) is False` 时重新准备；未知提交和已确认提交沿原异常退出。涉及 People.delete、admin forget/change、client claim、canonical append、ClientWorks.submit、Config rollback 及其 admin 外层。 | W06/W13/W15/W16/W24；V22/V28/V30/V36 |
| A-R1-02 | cold ingress 在 writer 中解码原回复正文、ASR 和 segments。现在先提交冷身份/Conversation 修复，再点读同 Conversation 的原内部 reply ID 并渲染。已有身份的 reader 快路不增加查询。 | W11/W12/W13；V27/V28 |
| A-R1-03 | 普通 Person/Space route 的 insert/transition 只重核 route CAS，准备后的 Binding 或 Presence 可以失效。writer 现在沿原谓词点核当前 Binding 归属/阶段、Person root/enabled、Presence 平台及 Registry 账号/能力，再写原候选。没有新探针、重选策略或持久状态；merge route 仍在同 writer 按当前真实候选重算。 | W11/W15；V03/V28 |
| A-R1-04 | feedback 外层虽然已经准备证据，`record_feedback` 仍在 supplied writer 编码 payload，`get_runs`/返回值仍解整段 sources/support JSON。现在 writer 前准备 payload 与原 run 投影；writer 按原 run 的实际字段、sources 语义、state/sequence 和原回执核验，只装载可变小列，返回复用的原 run 投影。64 refs/页、4 页/tick、来源变化和 no_reply phantom 语义保留。 | W18/W19/W70；V28/V49 |
| A-R1-05 | `ClientPrompts.finish` 在 writer 里解析原消息并 hash。现在 reader 准备原 segments/hash，writer 比较实际原源；源改变只重新准备结算，沿同 request/event ID，不重跑 Main。`associated_work` 的授权改用 SQL 提取实际 actor/trigger/conversation，不再解析整个 Work source。 | W13/W24/W69；V22/V28/V36 |
| A-R1-06 | derived-text writer 的 `_event_record(row) != prepared.event` 会重新解完整 segments/external payload。现在准备实际原字段/raw JSON 值，writer 直接比较，保原 Conversation/source token；ASR 分词、正文构造仍在 reader/准备阶段。 | W14/W23/W73；V28/V29/V35 |
| A-R1-07 | fresh canonical/account-fact/scoped append 会解码刚在外编码的 JSON。`_event_record` 现在可接已解析 segments/payload；新行返回复用原准备，原回执兼容路径保已有事实。没有新缓存。 | W13/W14；V01/V28/V29 |
| A-R1-08 | Client/Canonical/Control/Plugin 业务异常即使带未知 commit 阶段也被 HTTP 映成普通业务失败。现有 typed handler 先认公共未知提交事实，返回 `unknown`；确认回滚仍保原业务错误。没有新增泛异常 handler。 | W06/W24；V22/V36 |
| A-R1-09 | `external_command` 在点读原成功 receipt 前把可缓存业务异常写成失败；若终态 commit 已落、ACK 以同类异常丢失，会覆盖成功。现在只有无 DB phase 的原业务失败，或已确认未提交，才记缓存失败；其他情况先按原 receipt 恢复。 | W06/W16/W24；V22/V36 |
| A-R1-10 | `validate_self_initiative` 的所有 6 个生产调用方都不用返回 DTO，但 supplied writer 仍解完整 run JSON。删除该无用返回，原 run ID 只查 Conversation/generation/Space/Presence，再沿同 session 核当前 scene；owned 路径也从两个 reader 合为一个。不会重新要求当前 controller owner 或把已结算状态变成执行门槛。 | W18/W19/W70；V28/V49 |

`test_unknown_native_mention_records_account_without_creating_person` 仍用旧的同步 apply 形态，实际没有 await 新 bind；
已改为 `(await apply(session))()`，保原“只记 Account、不造 Person”的真实断言。
SELF retained-wait 测试对无用 DTO 的依赖已删除，改为通过原 repository 点读实际 run 状态，资格校验继续单独执行。

## 复核的原子单元与边界

- Canonical receipt、ChatEvent、账号/昵称观察、Person/membership、FTS、media index、Conversation 水位及 generation 保同 writer。
  App Work 输入的 canonical event、mailbox/resume 接纳和 personal command receipt 仍同笔；原 request、canonical UUID、Work/lease 不重建。
- merge 在首次 DML 前核 source/target root、完整来源族、目标冲突集合、alias absence、实际配置 row/version 和将丢弃行的原内容。
  当前 route 继承在 writer 按真实 Binding/Presence/Registry 重新绑定；不会用 reader ORM 改旧行。
- forget 在首次 DML 前核实际 ownership history、private Conversation、原 event/mentions、复制到 Memory/tool/artifact/Work 的源、initiative、SELF、audit 及 ResourceCleanup closure。
  selection proof 比较真实成员与原字段，包含新增引用/资源 phantom。已 close 的 ACP 回执不因纯 DB 重备扩大关闭范围。
  整次隐私删除、脱敏、FTS、来源清除仍是罕见但真实的完整原子操作。
- Config rollback 的旧 audit JSON/迁移谱系在 reader 准备，writer 核原审计字段、实际相关 moves 成员及原 JSON；现代 row_id/version 快路保留。
  unrelated config move、人物无关昵称或格式变化不升级成全局门槛。
- 管理数据库命令的领域变更、audit 和原 receipt 共用 writer/savepoint；外部命令先 reserve、退出 DB 执行 effect，再结算。
  真实可缓存失败保留；commit unknown 不覆盖成功，也不重发 effect。
- Media lock → reader/文件准备 → writer 的顺序保留。下载、hash、rename、unlink、native probes、SOUL 读写/热装配均不等待在主库 writer 中。
  Upload/cache 最终文件不会因提交结果未知而被当作临时文件删除；GC 沿当前 receipt/index/source 的真实引用判断。
- history、projection、config snapshot、个人 GET/SSE 继续使用 reader；原需要真实快照的显式 `BEGIN` 保留。
  supplied session 不暗开第二个 reader/writer，原 writer 正常 autoflush；owned reader 不自动 commit。
  `ClientViews.stream` 订阅后先 sync，逐次短 reader，等待 hint/keepalive 时不持 session。
- TG/飞书的 `get_me`/membership 与消息处理在 writer 外，首次 Presence/Space 和 TG migration 才短写；App/ACP 继续原入站管线。
  协议转换、HTTP/CLI/SDK 不自行保存另一份主库事实。

## 性能与本轮没有改成新机制的地方

1. feedback 短 writer 中，外层 witness 检查和 repository 当前 source 检查目前各有一次点查；
   相比原 DTO 解析减少 Python codec，但多了窄 SQL 往返。它在背景分页路径，仍须纳入 root W63 性能记录。
   没有为省这次检查新增“已验证”状态、revision 或另一个框架。
2. 正常重复 receipt 已有 reader 快路；极少数并发重复进入 writer 后仍须读取原选定回执/内容并做原兼容校验。
   本轮只删除 fresh 行的重复 codec，没有把准备输入冒充原事件返回。
3. cold identity 仍有原选定账号/mention 元数据的局部投影；没有网络或历史正文扫描。
   本轮移走原回复大正文解析，没有声称 writer 完全没有 Python 构造或任何小 JSON。
4. 整次 privacy purge 仍有 SQL JSON selection、触发器和跨表删除成本；这由真实原子性决定，不能按页拆成部分遗忘。
   projection 容量淘汰保原缓存原子单位；没有根据本轮小样本新增索引或性能阈值。
5. `People.observe/observe_canonical_person` 当前生产源码无调用，只有身份回归测试使用；不是 Main 热路径。
   其名称观察只查该账号/群关系，不扫描会话正文。是否进一步删除这组旧便利入口留后续轮决定，未擅改其公共表面。
6. Workspace manifest、SDK/ACP outbox、StickerIndex、BridgeState、SQLiteApplicationLock/离线 setup、DDL 与公共 Database owner
   由 root 独立范围复核；它们不因本报告主库收口变成共享同一个 asyncio FIFO。

## 验证记录

以下组别互有重叠，不相加宣称唯一用例数。所有数据库操作均在本机测试库。

- 真实 forget commit 已落、ACK 以 `PrivacyPreparationChanged` 丢失：修前没有抛出原异常，修后保原异常/phase，原人物已删除且只准备一次。
- privacy + Person + client link 一组 **35 passed**；cold ingress/canonical 一组 **36 passed**。
- route/client/config 一组原有 **116 passed**；新增 route 8 个 case 单独 **8 passed**。
- feedback/SELF/source 原组 **41 passed**；删除 SELF DTO 后的同类组 **44 passed**。
- feedback 外部 codec + sources 格式/无关字段不增门槛新增组与原反馈测试 **17 passed**。
- Client/derived/feedback 一组 **29 passed**；typed HTTP + derived + route 一组 **25 passed**。
- 新 fresh 入站/出站/external JSON 复用断言及 identity boundary **7 passed**。
- ClientPrompts 两个原源变化/未变化结算场景，连同 privacy/merge 等 **49 passed**；模型调用数不增加。
- 管理 database/external 两条真实 typed commit ACK 丢失场景，连同 admin API/external effects **24 passed**。
- 18 个本轮修改的生产文件定向 mypy 通过，相关源码与测试 Ruff 通过。
- 最后统一定向集运行结果见本文收尾记录；root 的全仓检查、性能对照和后续两轮不计入以上完成项。

## 跨域交接

`EmojiDelivery.validate_claim` 的 supplied writer 会调用 C 的 intents repository 返回完整 DTO；
source/intent/selected JSON 解析和 `validate_source` 的后续实际读取已交给当前 C reviewer，不能只把 A consumer 标为“没有直接 SQL”便排除。
root 负责再核 observation DAG validator 的所有 writer 消费者；A 当前 EventLedger 自开 reader 路径保原快照/取消语义。
`OrdinaryAdmissionRepository.commit` 的 supplied `admission_allowed` 回调会进入 C 的
`continuation_current/actor_identity`。该处 controller events/boundaries/actors 扫描由当前 C reviewer
接手移到 reader 准备，writer 只核原固定 actor 家族与绑定事实；本报告不把回调无直接 SQL 当作无 writer 工作。

## 逐文件分类

- F：本轮全文或该文件全部可执行实现已读；结构声明也计入读取，未只看 grep。
- M：该文件全部实际主库入口/写方法、准备与调用链已读；没有宣称整个格式/配置/纯算法部分全文。
- I：按当前 AST/导入/调用结构排除自有主库事务，并沿领域委托核消费者；不是全文。
- S：模型/Schema/SQL 表达式定义或纯结构，主库生命周期/迁移归 root 核；不是运行时新 writer。
- X：本轮明确归 root 的公共 owner、独立数据库、Automation 或离线生命周期，不能算作本审查者独立验证。
- N：完整候选结构排除，无自有主库入口；协议/纯数据/格式实现不虚记全文。

表中 M/I 的范围说明是本轮实际依据；完整候选清单不等于把所有 N/S 文件渲染代码逐字再读一遍。

| 当前路径 | 行数 | 等级 | 本轮范围 / 排除依据 |
| --- | ---: | --- | --- |
| `src/yuki/acp_adapter/__init__.py` | 6 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/acp_adapter/agent.py` | 394 | I | HTTP客户端/SDK outbox和原session request委托；本地主库owner不在此 |
| `src/yuki/acp_adapter/cli.py` | 51 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/activity.py` | 21 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/card_parser.py` | 122 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/media.py` | 77 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/message_id.py` | 9 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/normalizer.py` | 472 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/provider_adapter.py` | 152 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/sender.py` | 168 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/adapters/onebot/social.py` | 132 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/__init__.py` | 23 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/audit.py` | 242 | M | audit append/history/get 与 supplied/owned transaction；小审计投影 |
| `src/yuki/admin/authority/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/capabilities.py` | 110 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/command_types.py` | 341 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/commands.py` | 85 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/decision.py` | 114 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/json_types.py` | 50 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/operations.py` | 118 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/operators.py` | 114 | M | authenticate 的原凭证/Person reader，不增加 last-used 写 |
| `src/yuki/admin/authority/principal.py` | 119 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/problems.py` | 62 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/targets.py` | 61 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/authority/tokens.py` | 38 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_files.py` | 482 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_owners.py` | 16 | M | resolve_space_owner 只读点查；Config writer 原 session 复用 |
| `src/yuki/admin/config_registry.py` | 160 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_service.py` | 1981 | M | 所有 repository 读写、initialize/snapshot、rollback 准备/来源/actor/谱系与 mutation owner |
| `src/yuki/admin/config_spec_helpers.py` | 77 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_specs_asr.py` | 77 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_specs_future.py` | 45 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_specs_hot.py` | 525 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_specs_planner_plugins.py` | 172 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_specs_protected.py` | 122 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_specs_restart.py` | 512 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_specs_social.py` | 7 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/config_specs_tooling.py` | 64 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/control_resolution.py` | 270 | M | Person/Space/recovery principal 与实际 Binding reader；其余是能力声明/数据装配 |
| `src/yuki/admin/models.py` | 296 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/operations/__init__.py` | 39 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/operations/base.py` | 346 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/admin/operations/config.py` | 194 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/admin/operations/groups.py` | 35 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/admin/operations/identity.py` | 573 | M | 全部领域写/外部资源关闭组合、merge/forget prepare/apply、原control receipt及Registry提交后更新 |
| `src/yuki/admin/operations/models.py` | 56 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/admin/operations/plugins.py` | 154 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/admin/operations/work.py` | 40 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/admin/permission_catalog.py` | 781 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin/queries.py` | 442 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/admin_api/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/admin_api/cli.py` | 342 | I | 调用管理HTTP client，无主库session；离线生命周期归root CLI审查 |
| `src/yuki/admin_api/client.py` | 145 | I | HTTP原request/query/unknown行为；领域提交不在客户端重做 |
| `src/yuki/admin_api/http.py` | 646 | M | 所有路由的领域委托与typed异常handler；没有主库事务另开 |
| `src/yuki/automation/__init__.py` | 6 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/authority.py` | 61 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/context.py` | 72 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/control_context.py` | 152 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/creation_key.py` | 14 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/gateway.py` | 12 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/models.py` | 152 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/repository.py` | 1289 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/service.py` | 790 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/task_spec.py` | 62 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/tools.py` | 481 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/automation/worker.py` | 334 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/capabilities/__init__.py` | 65 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/binding.py` | 43 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/catalog.py` | 124 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/coordinator.py` | 235 | I | 统一工具执行/回执由root和C复核；本文件无SQL session owner |
| `src/yuki/capabilities/exposure.py` | 5 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/invocation.py` | 173 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/media.py` | 30 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/models.py` | 143 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/policy.py` | 62 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/provider.py` | 327 | I | 固定工具装配委托；执行处授权/存储归相应领域 |
| `src/yuki/capabilities/results.py` | 694 | I | 三态mutation结果由C实施及本轮C reviewer复核，本轮未并发修改 |
| `src/yuki/capabilities/runtime.py` | 119 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/capabilities/validation.py` | 151 | I | admit是能力预算对象方法，不是SQL session；执行写委托领域 |
| `src/yuki/character/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/character/db_models.py` | 38 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/character/repository.py` | 265 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/character/soul.py` | 354 | M | read/reserve/finish/save/reload 全事务与文件锁/发布边界；顶部文件工具按结构归类 |
| `src/yuki/client_api/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/client_api/db_models.py` | 40 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/client_api/errors.py` | 10 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/client_api/files.py` | 143 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/client_api/http.py` | 324 | M | 认证/消息/Work/ACP/媒体/取消/SSE的全部领域委托；cancel_prompt 仅reader核会话 |
| `src/yuki/client_api/http_errors.py` | 73 | M | 全文；已有typed handler按公共未知phase映射，未加泛异常handler |
| `src/yuki/client_api/identity.py` | 804 | M | 全部鉴权、pair/code、claim/merge、transfer/revoke、receipt 与cleanup入口 |
| `src/yuki/client_api/messages.py` | 145 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/client_api/models.py` | 111 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/client_api/openapi.py` | 30 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/client_api/prompts.py` | 301 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/client_api/requests.py` | 265 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/client_api/sessions.py` | 115 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/client_api/version.py` | 3 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/client_api/views.py` | 331 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/client_api/works.py` | 607 | M | 全部 Work 视图/授权/submit/cancel/原 receipt/mailbox组合；准备在外，同writer复核 |
| `src/yuki/client_sdk/__init__.py` | 6 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/client_sdk/cli.py` | 196 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/client_sdk/client.py` | 435 | I | HTTP请求及原UUID/outbox委托；outbox独立库归root |
| `src/yuki/client_sdk/outbox.py` | 122 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/conversation/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/conversation/autonomy_binding.py` | 162 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/conversation/autonomy_db_models.py` | 110 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/autonomy_repository.py` | 781 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/canonical_db_models.py` | 722 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/canonical_schema.py` | 176 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/correlation.py` | 115 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/event_entry.py` | 29 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/features.py` | 131 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/frozen_fragments.py` | 359 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/conversation/hydrate.py` | 434 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/initiative_sources.py` | 58 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/media_index.py` | 135 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/media_service.py` | 1186 | M | 所有数据库消费者：_sources/cache publication/authorized_path/expiry/GC及锁序；native解析纯文件分支按结构排除 |
| `src/yuki/conversation/observation_models.py` | 75 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/observation_schema.py` | 131 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/observations.py` | 134 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/ordinary_admission.py` | 496 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/ordinary_admission_db_models.py` | 36 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/participation.py` | 318 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/conversation/projection_chunks_schema.py` | 80 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/projection_compact_schema.py` | 209 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/projection_current_schema.py` | 268 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/projection_history_schema.py` | 220 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/projection_material_schema.py` | 268 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/projection_models.py` | 82 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/projection_revision_schema.py` | 85 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/projection_schema.py` | 58 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/projections.py` | 692 | M | 所有DB方法与prepare_commit/publish closure；其余内存cache/编码投影按结构归类 |
| `src/yuki/conversation/prompt_history.py` | 317 | I | 内存历史/冻结投影；实际reader与publication在scope/projections审过 |
| `src/yuki/conversation/scope.py` | 118 | I | scope/turn只读DTO比较；无session owner |
| `src/yuki/conversation/scope_models.py` | 50 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/conversation/scope_repository.py` | 300 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/self_initiative.py` | 59 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/conversation/uploads.py` | 149 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/deployment_setup/__init__.py` | 5 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/deployment_setup/command.py` | 934 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/deployment_setup/environment_check.py` | 40 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/deployment_setup/service.py` | 970 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/deployment_setup/terminal.py` | 168 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/emoji/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/emoji/delivery.py` | 401 | M | source/admit/project/validate_claim/recovery委托；已识别C intents DTO writer问题并交C reviewer |
| `src/yuki/emoji/library.py` | 342 | I | 派生索引/媒体/描述委托；StickerIndex独立库及模型诊断由相应owner复核 |
| `src/yuki/emoji/media.py` | 151 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/emoji/models.py` | 27 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/emoji/sticker_index.py` | 543 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/gateway/__init__.py` | 23 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/activity.py` | 11 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/compatibility.py` | 66 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/local_clients.py` | 7 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/media.py` | 30 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/models.py` | 44 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/provider.py` | 145 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/__init__.py` | 36 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/app/__init__.py` | 45 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/app/ingress.py` | 262 | M | start/receive/ack与原on_finished生命周期；Social发送委托同原内部来源 |
| `src/yuki/gateway/providers/app/social.py` | 27 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/feishu.py` | 279 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/feishu_ingress.py` | 406 | M | Presence/Space创建、原receipt去重与membership/processor事务外边界；协议normalize按结构归类 |
| `src/yuki/gateway/providers/feishu_protocol.py` | 55 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/feishu_social.py` | 198 | I | 平台HTTP/media/social委托，没有自有SQL owner |
| `src/yuki/gateway/providers/feishu_ws.py` | 231 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/snowluma.py` | 63 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/telegram.py` | 183 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/telegram_activity.py` | 28 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/providers/telegram_ingress.py` | 505 | M | Presence/Space/migration/title/edit/receipt入口、membership/processor事务外；normalize按结构归类 |
| `src/yuki/gateway/providers/telegram_social.py` | 126 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/gateway/registry.py` | 449 | I | 内存连接注册/选择/能力，不是DB事务；route实际writer已另核 |
| `src/yuki/gateway/social.py` | 126 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/identity/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/identity/authority.py` | 26 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/bootstrap.py` | 28 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/canonical_repository.py` | 395 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/canonical_uow.py` | 841 | M | 全部原生入账、account fact、prepare与观察bind/reuse链；fresh codec复用与原receipt兼容 |
| `src/yuki/identity/db_models.py` | 347 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/identity/errors.py` | 11 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/identity/event_author.py` | 117 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/evidence.py` | 31 | I | 原事件可读性纯谓词；不写库 |
| `src/yuki/identity/ingress.py` | 790 | M | 全部pre_admit/_admit、冷/热身份/Conversation、reply/source/作者与mention投影；原连接与probe边界 |
| `src/yuki/identity/memory_guard.py` | 45 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/mentions.py` | 131 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/ownership.py` | 56 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/identity/ownership_interval.py` | 139 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/person_identity.py` | 526 | M | 全部root/family/name/merge prepare/apply与配置/alias集合核验；不是仅看owner revision |
| `src/yuki/identity/person_state.py` | 831 | M | 全部Account/Person观察prepare/bind/apply、历史阶段、profiles/aliases/membership与flags |
| `src/yuki/identity/readers.py` | 43 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/receipt_compat.py` | 204 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/routing.py` | 1605 | M | 全部读/send/probe/CAS/merge route/ingest方法与原writer闭包，所有网络在writer外 |
| `src/yuki/identity/space_metadata.py` | 63 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/identity/sql_constraints.py` | 23 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/identity/write_settings.py` | 51 | I | 进程内设置/ContextVar，不是DB写入 |
| `src/yuki/persistence/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/persistence/artifact_refs.py` | 29 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/persistence/command_receipts.py` | 827 | M | 全部record/recover/reserve/unknown/replay及audit关联验证；新小回执序列化保原子单位 |
| `src/yuki/persistence/commit_hints.py` | 27 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/persistence/database.py` | 407 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/persistence/diagnostic_writer.py` | 227 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/persistence/event_repository.py` | 986 | M | 全部主库方法，真实历史快照/driver interrupt/supplied no_autoflush与derived写委托 |
| `src/yuki/persistence/event_view.py` | 66 | I | 原EventRecord到HTTP投影；不写库 |
| `src/yuki/persistence/history_search.py` | 174 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/persistence/history_search_index.py` | 97 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/persistence/instance_lock.py` | 94 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/persistence/job_claims.py` | 62 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/persistence/media_repository.py` | 243 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/persistence/metadata.py` | 37 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/persistence/models.py` | 1844 | S | 模型/DDL/触发器/Schema结构候选；没有自有运行时session，迁移/生命周期由root核 |
| `src/yuki/persistence/people_repository.py` | 1916 | M | 全部CRUD/读、privacy prepare/SQL selection proof/apply、Memory/Plugin/Conversation清退尾部 |
| `src/yuki/persistence/repositories.py` | 33 | I | 稳定领域导入导出；接收真实实现逐个审过 |
| `src/yuki/persistence/repository_helpers.py` | 147 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/persistence/repository_records.py` | 183 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/persistence/schema_guard.py` | 337 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/persistence/scoped_event_uow.py` | 1184 | M | 全部append/重复receipt/causality/媒体edit/derived准备与writer；保事实源不以输入替代旧receipt |
| `src/yuki/persistence/sqlite_connection.py` | 35 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/persistence/sqlite_diagnostics.py` | 112 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/persistence/unit_of_work.py` | 56 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/persistence/web_repository.py` | 50 | F | 本轮完整实现已读；事务/来源/纯读或原领域委托见正文 |
| `src/yuki/persistence/worker_recovery.py` | 24 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/web/__init__.py` | 19 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/web/base.py` | 95 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/web/bridge_state.py` | 63 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/web/http_transport.py` | 304 | I | HTTP handle_async_request的execute/get不是SQL调用 |
| `src/yuki/web/models.py` | 64 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/web/native_bridge.py` | 337 | I | BridgeState派生独立缓存委托，不并主库FIFO |
| `src/yuki/web/native_sources.py` | 89 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/web/page_extractor.py` | 117 | I | 页面解析/文本结果，不是主库持久化 |
| `src/yuki/web/route_context.py` | 22 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/web/search_config.py` | 54 | I | 配置读取投影，无自有SQL事务 |
| `src/yuki/web/tavily.py` | 270 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/workspace/__init__.py` | 1 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/workspace/files.py` | 170 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/workspace/inspect.py` | 139 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |
| `src/yuki/workspace/service.py` | 244 | M | 原Workspace/Sandbox/ConversationMedia委托与实际source-version校验；独立manifest归root |
| `src/yuki/workspace/short_state.py` | 109 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/workspace/store.py` | 284 | X | 公共owner/Automation/独立数据库/离线生命周期，本轮root独立负责；不计本审查者验证 |
| `src/yuki/workspace/tools.py` | 65 | N | 当前完整AST/导入/调用结构排除自有SQL入口；纯数据/协议/配置/格式，领域接收处另核 |

共 240 个 A source：F 42、M 28、I 21、S 21、X 29、N 99。

跨域额外审查/修复：`src/yuki/services/participation_feedback.py`（C 域，同 writer 反馈准备与小列复核，已和当前 C reviewer 协调）。


## 本轮收尾

最终统一定向集：22 个测试文件，**247 passed，1 warning，73.02s**。其中未知提交、原子回执、来源变化、普通路由、媒体清理、App/ACP 请求、SELF 与 feedback 均实际执行，未调用真实模型或平台。

源码尾项已完成，本轮不先进入第 2 轮；等待 root 冻结。跨域 Emoji 提交状态由当前 C reviewer/root 汇总，公共 owner/独立数据库与全仓性能不是本文独立验收结论。
