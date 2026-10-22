# SQLite writer 实现后交叉复审：第 2 轮，原 A 域

日期：2026-10-23。起点：`f8fbe14dc2b78a4160d004e714d7f272641a6b40`。
本轮从原 C 实施/第 1 轮 C 复审轮转到原 A；与设计阶段的三轮阅读分开记录。
源清单使用 `surface.json` 中 240 个 A source 路径，再解析当前每个文件的 AST、导入、调用和 SQL 常量。
第 1 轮报告只用于列候选及未决项；以下行为判断来自当前源码、真实调用者和本轮回归。
没有访问生产、调用真实模型或发送平台消息；未自行提交或推送。

## 本轮修复与删除

| 编号 | 事实、修复与证据 | 对应边界 |
| --- | --- | --- |
| A-R2-01 | `client_api/http_errors.py:19` 原来只把 committed=unknown 映为 unknown；根提交已确认、cleanup/ACK 抛 typed error 时仍变成 401/404/409。现在有真实 write phase 且 committed 不是 False，沿现有 `problem.code=unknown` 返回。没有加字段或泛异常 handler。3 种 typed error 的真实 pair 已提交回归保唯一 Device，普通 code 重用仍 401。 | W06/W24；V22/V36 |
| A-R2-02 | `identity/ingress.py:147` 的 `canonical_owner_disabled` catch 和 `client_api/messages.py` 的 receipt_conflict catch 会吞原提交事实。前者只把读阶段/确认回滚转换为普通 drop；后者先点读原 receipt 恢复，找不到则传播原未知/已提交异常。实际已入账原消息不会重新调 Main；不存在 receipt 也不会用同类异常编造拒绝或成功。 | W11/W13/W24；V22/V28/V36 |
| A-R2-03 | `ClientWorks.submit` 为读原 actor，在 reader 和 writer 各解整段 private Work source。新增 `_input_work` 只取当前 Work 小列并用 SQL 提取实际 actor、核同人族及 private Conversation；原请求、输入、canonical UUID、预算和 receipt 同笔。GET 的完整 Work 投影仍在 reader。大 source 回归证明 writer 不解该正文，models=3 等原预算保留。 | W13/W24；V22/V28/V36 |
| A-R2-04 | 删除 feedback outer 的重复 `feedback_run_current` 点查与仓库该方法；repository `record_feedback` 仍在首次 DML 前核完整原 source，原 sequence/receipt/state 原子提交，外层 effect/Work/model/no_reply phantom 证明保留。删除 `_feedback_session` 和 Config `_mutation_session` 两层无独立政策的 wrapper，直接复用现有 optional_session。没有“已验证”标记或新 revision。 | W18/W19/W70；V28/V49 |
| A-R2-05 | 全生产调用链核实 `People.observe` 仅供测试，已删除该入口、`_observe_role`、`observe_canonical_person` 及旧重复 nickname/profile 写。真实入站继续用两个 production preparer。测试 fixture 显式给已接纳的原 Assignment，调用原 account/person prepare→bind→apply，不复制角色分类或重试算法，不插额外 ledger event。全 `src/tests` 的 `.observe(` 逐接收者复查后，链式 People 调用也迁完；5 处 is_bot=True 本来无操作，直接删。 | W11/W13/W15；删除导向 |
| A-R2-06 | Person observation 准备只消费原内部 IDs；原 reader 的 Person/Space/SpaceBinding 读取没有产物消费者，删除它们。writer 把原 Binding、当前 root、原 Space/Binding 和原 Membership 缺席/存在证明合为一次 joined SELECT，仍核真实归属，原 membership 以原 Person 保存。名字/账号卡片仍归各自真实 owner。 | W11/W13；V01/V28/V29；W63 |
| A-R2-07 | Ordinary admission 原 current identity 7 次 SELECT 先合为一次；继续核全部 caller 后，把 existing/source/Conversation/segments/current identity/group 全部同笔纯证明合为一条 SELECT，删除 async `_identity_allowed`。原 admission 优先返回 False；不存在时原字段、generation/floor、keeper、segments、原 Person/root、Assignment、Presence、Space/Binding 任一失效仍抛原 source_changed。public controller 当前资格回调保留，没有缓存或新 mode。11 个真实撤权/来源变化 case、群私聊、handoff 与原 provenance 均过。 | W18/W19；V28/V49；W63 |
| A-R2-08 | PluginOperations external effect 的 3 个 typed catch 会把已带公共提交 phase 的原异常重新造为普通 ControlCommandError，导致外层错误缓存“版本冲突”。仅对有 phase 且 committed 非 False 的异常裸 raise；纯业务/已回滚映射照旧。真实 PluginConfigurationService.save 已 commit 后注入 typed ACK 丢失，修前失败，修后原配置保留、control receipt 为 unknown，重放原 request 不再改配置。 | W06/W16/W24；V22/V36 |

A-R2-04 触及 C 的 `services/participation_feedback.py`，已和本轮 C owner 协调，未并发改写该链。
A-R2-08 只处理会结束独立 writer 的 external plugin effect；同 supplied writer 的 Work/PluginOwnership 业务转换没有内部根提交，不泛加同样分支。

收尾冒烟补出一处真实 caller 遗漏：Work `repair_abandoned_inputs` 仍导入已删除的 async `_identity_allowed`。
现抽出共用的纯 SQL `identity_allowed_condition`，ordinary 的完整原 source proof 保单次 SELECT；
repair 在 reader 内将原 event/Conversation/generation/keeper/floor/当前身份合为一次查询，不恢复串行读取 shim。
这处 C 文件修改已与当前 C reviewer 协调；新 chat 冒烟实际走启动预热及调度，不只靠编译或普通 admission 单测。

## 全域核对结果

- 主库所有 owned reader 仍用 read_session；需要一致快照的历史、上下文、隐私准备、Config snapshot 和个人 views 显式 BEGIN。
  supplied session 继续复用，不为组合操作暗开第二个 owner。账户资料与 canonical event、receipt、FTS、media index、Conversation 水位仍同原 writer。
- Canonical/scoped append 的 tokenizer、正文及 external JSON、媒体准备在 writer 外；新行返回复用原编码前数据。
  并发 duplicate receipt 保原行兼容验证，不用准备输入冒充原件。ASR/derived edit 的实际原行/Conversation token 重核仍在同 writer，源变化只重备该 DB 结算。
- Merge 核原 source/target root、同人族、真实配置成员/版本/丢弃内容、alias absence；forget 核原事件/提及/ownership interval、私聊、复制来源、Memory/Rebuild、SELF、插件、audit 和资源 closure。
  两者保罕见完整原子单位。外部 ACP peer close 在 writer 外，不为纯 DB 重备重复关闭已确认的另一套资源。
- Admin database_command 的当前授权、savepoint、领域 mutation、audit、原 receipt 共 writer；external_command reserve 后退出 DB 再执行效果。
  Config legacy rollback 的历史 JSON/谱系在 reader，writer 比较原审计行及实际相关集合。Config 当前值的小投影、ID/时间参与的小型 receipt 编码仍保原子边界。
- Character 当前 profile/owner/receipt 使用当前 writer；SOUL reserve→文件操作/热装配→finish 分开，原 request/文件版本保留。
  App/ACP 普通消息和个人 Work 操作继续入原 canonical/Work 链，不自行创建第二份任务事实。
  Client SSE 等待 hint/keepalive 时不持 session；SDK/ACP 重连沿 durable 原 UUID 查询，不把 unknown 变新请求。
- Media 顺序为既有 media lock→文件/reader 准备→短 writer；解析、下载、hash、rename、unlink、模型和 native probes 不持主库 writer。
  upload 发布最终文件后才记 receipt；未知提交不会删可能已被原 receipt 引用的最终文件。GC 按原 expires_at 与当前引用清理，不重置 24 小时原件寿命。
- Projection 的 chunks、原 selection、source CAS、成员/absence 和容量淘汰仍同原发布原子单位；大 payload 构造、summary/source 渲染在 reader/准备阶段。
  主动 proposal/feedback 的旧页及 codec 在 reader；实际当前 scene、固定 source/member 和 sequence/receipt 在 writer。
- 各 schema 的运行时 trigger/hook 实际 SQL另核：原生 Account/Assignment 身份不变、结束阶段不复活、route owner/platform、pinned alias、projection/observation invalidation 和 FTS 删除仍由原事务触发。
  历史版本 schema 是冻结迁移依赖，不因看似重复就删除；普通运行入口不新建旁路连接。

## 独立库与排除项

- 本轮另外全文核 SDK Outbox、BridgeState、StickerIndex、Workspace manifest 和 short_state；它们有各自同步连接/文件锁/进程 owner，不进入主库 asyncio FIFO。
  Workspace publication 的小型 lstat/rename 保既有 manifest fence，流读取、hash、fsync 已在外；强拆这段会失去当前文件/manifest 发布关系，没有为主库改造另造 journal。
- `workspace/inspect.py:132` 原结构排除不够：实际 `validate_artifact` 通过 manifest 的只读 transaction 点核 immutable/digest。本轮改为 F 阅读记录；不是主库 writer，也不读像素。
  `admin/operations/__init__.py` 是真实依赖装配，改为 I；不是纯类型文件。
- 路由准备后出现另一个可达 candidate，不作为本轮新缺陷：选择发生于原 reader/native probe 时；writer 保原 route CAS、实际 winner Binding/Presence/能力/授权。
  不增“整个候选全集永远不变”门槛，外部探针也无法在 SQLite 内冻结。superuser first-create/group recovery 的原 absence、revision、authenticated connection 与唯一原账号合同仍保留。
- Public Database/driver/health/离线 owner、Automation 和 migration 生命周期由本轮 C reviewer 负责，下面记 X；并不计为本人已经独立通过。
  本人核其 A 消费者的 session 供给与提交事实；不把 X 误记纯结构排除。
- Emoji 两处泛 catch 关闭 intent 的跨域未知提交问题，已发本轮 C owner，确认由其修复和补真实 ACK 回归；本文不重复编辑，也不把对方验证计入本人通过数。

## 验证

以下组别有重叠，不能相加为唯一测试总数。

- HTTP/Ingress/App original receipt 和模型调用次数：4 文件 **31 passed**。
- 删除旧 People 便利入口后的 identity/Memory/social 定向集已有 **111 passed**；其中一处 fixture 变量迁移错误已修，相关 Memory partition 在后续 105 项联合通过。
- ordinary/参与/贴纸/ClientWorks/Memory partition：9 文件 **105 passed**。
- Dream + Memory person identity：**48 passed**；11 个新增群私聊原 Person/root/Assignment/Presence 与 Space/Binding 撤权 case 单独 **11 passed**。
- 最终热链与 external plugin 真实 ACK 回归：9 文件 **100 passed**；随后仅局部变量命名修正后，identity/origin/plugin **15 passed**。
- 删除 helper 的实际 Work 恢复 caller 补齐后，新的 chat 脚本冒烟：2 个输入全部被 Fake 模型观察并匹配确认 receipt，原生产者清空，workload_errors 与 SQLite errors 都为空。
  此为当前代码的冒烟结果，不与并发测试下的耗时作正式性能对比。
- 真实 abandoned batch/原 input 只推进一次、恢复提交未知、ordinary 原来源/撤权/handoff、ClientWorks 联合 **83 passed**；两个变更产品的 mypy/Ruff 再过。
- 11 个原修改生产文件定向 mypy 通过；新增 PluginOperations 及 Ordinary 最终两文件 mypy 再过。相关生产与测试 Ruff 通过。
  warning 仅为既有测试 create_all 的 canonical Conversation/alias FK cycle 提示。
- 日志均在 `D:/Temp/yuki-sqlite-validation/r2-b-*.log`；源码 AST/schema 提取只用于候选和结构检查，未冒充行为测试。
  公平 ABBA、全仓测试、全仓类型检查、Linux 独有路径与最终第三轮由 root 分别汇总，不在此宣称完成。

## 逐文件覆盖

F=本轮全文阅读；D=本轮全部数据库/来源/外部效果方法及其局部调用链阅读，纯声明/格式部分只作结构检查；
I=实际间接调用及 owner 边界核对；P=当前 AST/导入/调用/SQL声明分类排除，未声称全文；X=明确转交其他本轮 owner。
P 中的 schema 同时核实际 hook/trigger SQL，纯 ORM 列声明不记全文。所有路径均解析当前源，不只读 diff。

| 路径 | 当前行数 | 级别 | 本轮边界 |
| --- | ---: | --- | --- |
| `src/yuki/acp_adapter/__init__.py` | 6 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/acp_adapter/agent.py` | 394 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/acp_adapter/cli.py` | 51 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/activity.py` | 21 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/card_parser.py` | 122 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/media.py` | 77 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/message_id.py` | 9 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/normalizer.py` | 472 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/provider_adapter.py` | 152 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/sender.py` | 168 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/adapters/onebot/social.py` | 132 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/__init__.py` | 23 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/audit.py` | 242 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/authority/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/capabilities.py` | 110 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/command_types.py` | 341 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/commands.py` | 85 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/decision.py` | 114 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/json_types.py` | 50 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/operations.py` | 118 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/operators.py` | 114 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/authority/principal.py` | 119 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/problems.py` | 62 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/targets.py` | 61 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/authority/tokens.py` | 38 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_files.py` | 482 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_owners.py` | 16 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/config_registry.py` | 160 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_service.py` | 1966 | D | 所有repository读写、snapshot、legacy rollback prepare/current selection/原audit及供给session；纯配置投影按结构核 |
| `src/yuki/admin/config_spec_helpers.py` | 77 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_specs_asr.py` | 77 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_specs_future.py` | 45 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_specs_hot.py` | 525 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_specs_planner_plugins.py` | 172 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_specs_protected.py` | 122 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_specs_restart.py` | 512 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_specs_social.py` | 7 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/config_specs_tooling.py` | 64 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/control_resolution.py` | 270 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/models.py` | 296 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/operations/__init__.py` | 39 | I | 实际各operation依赖装配，共享原Database；本轮从纯结构项补入 |
| `src/yuki/admin/operations/base.py` | 346 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/operations/config.py` | 194 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/operations/groups.py` | 35 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/operations/identity.py` | 573 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/operations/models.py` | 56 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/operations/plugins.py` | 170 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/operations/work.py` | 40 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin/permission_catalog.py` | 781 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin/queries.py` | 442 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/admin_api/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/admin_api/cli.py` | 342 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/admin_api/client.py` | 145 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/admin_api/http.py` | 646 | D | typed handler/原操作恢复及所有领域路由委托；无隐式DB owner |
| `src/yuki/automation/__init__.py` | 6 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/authority.py` | 61 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/context.py` | 72 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/control_context.py` | 152 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/creation_key.py` | 14 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/gateway.py` | 12 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/models.py` | 152 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/repository.py` | 1289 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/service.py` | 790 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/task_spec.py` | 62 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/tools.py` | 484 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/automation/worker.py` | 334 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/capabilities/__init__.py` | 65 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/binding.py` | 43 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/catalog.py` | 124 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/coordinator.py` | 235 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/capabilities/exposure.py` | 5 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/invocation.py` | 173 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/media.py` | 30 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/models.py` | 143 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/policy.py` | 62 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/provider.py` | 327 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/capabilities/results.py` | 694 | I | 执行certainty/receipt消费由本轮C共同核；本文件无session/SQL owner |
| `src/yuki/capabilities/runtime.py` | 119 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/capabilities/validation.py` | 151 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/character/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/character/db_models.py` | 38 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/character/repository.py` | 265 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/character/soul.py` | 354 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/client_api/db_models.py` | 40 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/client_api/errors.py` | 10 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/client_api/files.py` | 143 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/http.py` | 324 | D | 鉴权、消息/Work/media/ACP/取消/SSE路由；唯一显式cancel reader边界 |
| `src/yuki/client_api/http_errors.py` | 73 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/identity.py` | 804 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/messages.py` | 151 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/models.py` | 111 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/client_api/openapi.py` | 30 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/client_api/prompts.py` | 301 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/requests.py` | 265 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/sessions.py` | 115 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/version.py` | 3 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/client_api/views.py` | 331 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/client_api/works.py` | 625 | D | 全部private Work视图/actor授权、prepared input/current proof/原receipt/取消；正文投影只在reader |
| `src/yuki/client_sdk/__init__.py` | 6 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/client_sdk/cli.py` | 196 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/client_sdk/client.py` | 435 | I | durable Outbox原UUID提交/查询/resolve/refresh；HTTP时已退出独立SQLite |
| `src/yuki/client_sdk/outbox.py` | 122 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/conversation/autonomy_binding.py` | 162 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/conversation/autonomy_db_models.py` | 110 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/autonomy_repository.py` | 754 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/canonical_db_models.py` | 722 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/canonical_schema.py` | 176 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/correlation.py` | 115 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/event_entry.py` | 29 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/features.py` | 131 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/frozen_fragments.py` | 359 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/conversation/hydrate.py` | 434 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/initiative_sources.py` | 58 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/media_index.py` | 135 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/media_service.py` | 1186 | D | 全部source/缓存发布/授权读取/预览/过期/GC/关闭；metadata工具与文件线程实际退出核对 |
| `src/yuki/conversation/observation_models.py` | 75 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/observation_schema.py` | 131 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/observations.py` | 134 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/ordinary_admission.py` | 509 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/ordinary_admission_db_models.py` | 36 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/participation.py` | 318 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/conversation/projection_chunks_schema.py` | 80 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/projection_compact_schema.py` | 209 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/projection_current_schema.py` | 268 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/projection_history_schema.py` | 220 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/projection_material_schema.py` | 268 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/projection_models.py` | 82 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/projection_revision_schema.py` | 85 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/projection_schema.py` | 58 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/projections.py` | 690 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/prompt_history.py` | 317 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/conversation/scope.py` | 118 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/conversation/scope_models.py` | 50 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/conversation/scope_repository.py` | 300 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/self_initiative.py` | 59 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/conversation/uploads.py` | 149 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/deployment_setup/__init__.py` | 5 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/deployment_setup/command.py` | 934 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/deployment_setup/environment_check.py` | 40 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/deployment_setup/service.py` | 970 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/deployment_setup/terminal.py` | 168 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/emoji/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/emoji/delivery.py` | 412 | D | source/admit/validate_claim/project/prepare及Social真实消费；泛catch未知提交修复交本轮C owner |
| `src/yuki/emoji/library.py` | 342 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/emoji/media.py` | 151 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/emoji/models.py` | 27 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/emoji/sticker_index.py` | 543 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/gateway/__init__.py` | 23 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/activity.py` | 11 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/compatibility.py` | 66 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/local_clients.py` | 7 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/media.py` | 30 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/models.py` | 44 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/provider.py` | 145 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/__init__.py` | 36 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/app/__init__.py` | 45 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/app/ingress.py` | 262 | D | 本轮数据库与外部效果相关方法/局部调用链逐项核；纯声明/格式未称全文 |
| `src/yuki/gateway/providers/app/social.py` | 27 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/feishu.py` | 279 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/feishu_ingress.py` | 406 | D | 本轮数据库与外部效果相关方法/局部调用链逐项核；纯声明/格式未称全文 |
| `src/yuki/gateway/providers/feishu_protocol.py` | 55 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/feishu_social.py` | 198 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/gateway/providers/feishu_ws.py` | 231 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/snowluma.py` | 63 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/telegram.py` | 183 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/telegram_activity.py` | 28 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/providers/telegram_ingress.py` | 505 | D | 本轮数据库与外部效果相关方法/局部调用链逐项核；纯声明/格式未称全文 |
| `src/yuki/gateway/providers/telegram_social.py` | 126 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/gateway/registry.py` | 449 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/gateway/social.py` | 126 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/identity/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/identity/authority.py` | 26 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/bootstrap.py` | 28 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/canonical_repository.py` | 395 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/canonical_uow.py` | 837 | D | fresh/duplicate receipt、account-only、prepare/bind/apply、FTS/media/watermark/原callback |
| `src/yuki/identity/db_models.py` | 347 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/identity/errors.py` | 11 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/identity/event_author.py` | 117 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/evidence.py` | 31 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/identity/ingress.py` | 792 | D | pre_admit/local/native/current与cold身份、reply/mentions/hydrate及recovery真实边界 |
| `src/yuki/identity/memory_guard.py` | 45 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/mentions.py` | 131 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/ownership.py` | 56 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/identity/ownership_interval.py` | 139 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/person_identity.py` | 526 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/person_state.py` | 726 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/readers.py` | 43 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/receipt_compat.py` | 204 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/routing.py` | 1605 | D | 全部resolve/候选/探针/三route CAS/current winner/ingest恢复；新全集门槛明确排除 |
| `src/yuki/identity/space_metadata.py` | 63 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/identity/sql_constraints.py` | 23 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/identity/write_settings.py` | 51 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/persistence/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/persistence/artifact_refs.py` | 29 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/persistence/command_receipts.py` | 827 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/persistence/commit_hints.py` | 27 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/persistence/database.py` | 417 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/persistence/diagnostic_writer.py` | 227 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/persistence/event_repository.py` | 986 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/persistence/event_view.py` | 66 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/persistence/history_search.py` | 174 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/persistence/history_search_index.py` | 97 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/persistence/instance_lock.py` | 94 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/persistence/job_claims.py` | 62 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/persistence/media_repository.py` | 243 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/persistence/metadata.py` | 37 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/persistence/models.py` | 1844 | P | ORM/DDL结构及实际hook/trigger SQL已核；无独立业务session；纯列声明未称全文 |
| `src/yuki/persistence/people_repository.py` | 1841 | D | 全部资料CRUD、privacy owned/mentions、prepare proof/apply/delete各领域来源；旧observe已删除 |
| `src/yuki/persistence/repositories.py` | 33 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/persistence/repository_helpers.py` | 147 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/persistence/repository_records.py` | 183 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/persistence/schema_guard.py` | 337 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/persistence/scoped_event_uow.py` | 1184 | D | 全部append/edit/derived/原causality/duplicate receipt/current source和prepare；history/tokenize在外 |
| `src/yuki/persistence/sqlite_connection.py` | 35 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/persistence/sqlite_diagnostics.py` | 112 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/persistence/unit_of_work.py` | 56 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/persistence/web_repository.py` | 50 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/persistence/worker_recovery.py` | 28 | X | 公共Database/driver/schema生命周期或Automation，明确交本轮C owner；不计本人独立验证 |
| `src/yuki/web/__init__.py` | 19 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/web/base.py` | 95 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/web/bridge_state.py` | 63 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/web/http_transport.py` | 304 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/web/models.py` | 64 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/web/native_bridge.py` | 337 | I | BridgeState访问与model/Work paid reserve/诊断委托；网络与独立cache事务分开 |
| `src/yuki/web/native_sources.py` | 89 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/web/page_extractor.py` | 117 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/web/route_context.py` | 22 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/web/search_config.py` | 54 | I | 本轮间接调用/导入/await owner边界核；领域实际方法另项核，无自有主库事务 |
| `src/yuki/web/tavily.py` | 270 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/workspace/__init__.py` | 1 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |
| `src/yuki/workspace/files.py` | 170 | I | WorkspaceStore委托与文件/hash操作，无SQL owner；独立manifest调用者另核 |
| `src/yuki/workspace/inspect.py` | 139 | F | 全文；validate_artifact真实独立manifest reader，像素/模型准备在外；补第1轮结构排除漏项 |
| `src/yuki/workspace/service.py` | 244 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/workspace/short_state.py` | 109 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/workspace/store.py` | 284 | F | 本轮全文；实际SQL、原来源/回执/取消与外部效果边界见正文 |
| `src/yuki/workspace/tools.py` | 65 | P | 当前AST/导入/调用排除自有SQL/事务；纯类型、数据、配置或Provider转换，未声称全文 |

共 240 个 source：F 63、D 14、I 22、P 117、X 24。

## 收束

原 A 本轮实现与全范围复审已收束；没有本人留待实现的 A 数据库方法。P 保留真实结构排除，X 的公共核心/Automation由对应owner完成，未把它们冒充本人全文阅读。
跨域额外修复为 participation_feedback 与 Work abandoned-input 恢复 caller，Emoji 当前catch由本轮C owner闭环。下一轮等待root冻结和轮转，不把本轮定向验证当最终全仓验收。
