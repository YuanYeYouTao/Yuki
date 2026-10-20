# 后端五轮审查报告

审查基线：`1d831151d37718bb9f320020ada1a15b7277edec`（2026-10-20；终审重新 fetch 后 origin/main 仍相同）。**五轮已完成，报告本身已交叉终审。** 本次只调查和形成报告，未修复产品、修改生产配置、调用付费模型或发送 QQ/TG 消息。

本报告记录审计完成时的证据。后续按用户要求补充的测试、旧测试清理和CI调整见[测试实施记录](backend-audit-tests-20261020.md)；这些测试改动不表示下列产品缺口已经修复。

现已开始按本报告修复，逐项代码、验证与交付状态见[修复进度](backend-audit-remediation-20261020.md)。下文仍保留原审查基线和故障证据。

## 审查范围与方法

检查当前仓库后端、插件 SDK 和配套执行脚本，共 **647 个文件、163,568 行**。独立插件、语义参与和其他外部仓库只核本仓库实际调用合同，不声称完整审计其全部内部实现；前端与生产配置也不在此次全量源码范围内。历史任务书不作为现行实现；旧迁移只在当前 schema/升级 reader 有实际依赖时追查，不把历史迁移逐行检查算入这个数。测试作为消费者与离线证据使用，没有运行无关全量测试。

三个子 agent 与主审分四个互不重叠的分区。并行分区不是审查轮次；每一轮全局收口后才进入下一轮。结构扫描覆盖每个文件，人工顺入口、拒绝条件、首次写入、已知结果、持久回执及消费者检查；结构扫描不等于每行均已人工证明正确。

| 分区 | 文件 / 行数 | 模块 |
| --- | ---: | --- |
| A | 158 / 35,528 | Account/Assignment/Person、授权、管理控制面、平台适配与路由、个人 API/SDK、ACP-A、Social、隐私清退、命令和部署配置入口 |
| B | 157 / 47,156 | Memory 读写、提取、Embedding、Dream、Reflection、Rebuild、自动化、插件 Host/SDK、插件会话与事件桥 |
| C | 139 / 27,359 | 终端/环境/ACP-B、CodeMode、工具合同与结果、媒体/表情/ASR/Vision、工作区、Web、Provider/模型执行、执行脚本 |
| 主审 | 193 / 53,525 | Main/Processor/参与、上下文/公共历史、Work/子任务/预算/等待/恢复、持久化与 SQL、装配与生命周期、领域类型、提示编译、诊断 |

| 轮次 | 工作 | 状态 |
| --- | --- | --- |
| 1 | 全范围广审，记录具体候选及尚未逐分支验证的部分 | 已完成 |
| 2 | 沿第一轮候选核真实入口、消费者与离线复现 | 已完成 |
| 3 | 交换分区再次广搜，找前两轮没有关注的链路 | 已完成 |
| 4 | 深挖第三轮和跨模块缺口，反证过大的修复建议 | 已完成 |
| 5 | 终审代码证据、报告、分类、范围和删除后果 | 已完成 |

以下依据当前源码及离线证据。VM 步数用于同一实验的增长比较，不是现网耗时；写事务范围、SQL 执行时间、锁等待和事件循环阻塞分别判断。原 Person、真实 actor/source、原 ID、原执行回执、实际授权和协议配对不是“防御性编程”，不能为了提速删掉。

## 总体判断与处理顺序

存在值得拆掉的职责混用和旧链，不只是文件长：第一方已认证入口又退回裸账号推断人物；资料读取套用新发送资格；已知结果与辅助 audit 共用失败/重试范围；原件收藏受上下文视图寿命约束；定点读取反复构建完整 UI 或整棵任务树。这些问题适合沿已有事实收窄和删除，不适合再加统一门禁、身份缓存或补偿协调器。

| 顺序 | 项目 | 原因 |
| --- | --- | --- |
| P1 | F08 | 完整公开 App 解绑链可复现新消息作者错归；优先保住内部人物与实际入口 |
| P2：事实/正文 | F01–F07、F09–F12 | 正文被改、已知结果丢失或重复购买、旧资料误拒、缓存原件错配、清理失去恢复线索等；分入口修，不做全后端重写 |
| P2：算法/等待 | P01–P11，以及下文八组既有 SQL 问题 | 均有真实消费者，分别有规模增长、同步阻塞或无关条件误拒证据；先处理主循环阻塞、热路定点查询及 writer 内扫描，再清后台重复工作 |
| P3 | 后文删除候选、局部编码/序列化与容量观察 | 无消费者或成本已量化较小；删整条死链可以做，未证生产事故的策略不升级为漏洞 |

P1/P2/P3 表示本报告建议的修复顺序，不表示已测到线上故障频率。没有发现需要改变统一发送出口、固定工具前缀或按内容强制选 Provider 的理由；也不据此承诺其余模块没有缺口。

## 新发现：事实或正文会被错误处理

### F01：旧的内部标记清理会删除普通正文

[renderer.py:22](../../src/yuki/services/renderer.py#L22) 的正则允许内部分隔符完全缺省；[Social 发送](../../src/yuki/social/service.py#L260) 和[历史渲染](../../src/yuki/event_prompt.py#L546) 都真实调用它。

当前函数的离线结果：`#473 修复说明` 变成 `修复说明`；`明天 [12:34] 见` 变成 `明天 见`；代码块中的 `#123 is a comment` 变成 `is a comment`。因此既会改发送内容，也会损失模型看到的历史正文。

删除方向：去掉对当前结构化消息正文的旧格式猜测；确有历史升级需求时，限定在能证明来源的旧 reader。保留真实 `yuki-state` 尾协议解析、控制字符归一化和真实空 payload 处理，不补另一组猜测正则或新增回复门禁。

### F02：插件已经返回结果，辅助审计失败却能推翻结果、重跑读取

[capability_adapter.py:191–239](../../src/yuki/plugin_host/capability_adapter.py#L191) 把 handler、后置 audit 和重试放在同一个 `try` 中。一次 audit `OSError` 可把已知成功 mutation 改报 `plugin_effect_unknown`；READ 配置 `TRANSIENT_ONCE` 时，handler 实际执行两次。持续 audit 失败还会直接抛错。普通 OneBot mutation 已收到 ACK 后，部分 Facade 路径也会被 audit 故障改成调用异常。

删除方向：已知 handler 返回与辅助诊断分开结算；audit 不进入 handler 的重试范围，不把明确结果降为 unknown。真实派发断连/超时仍按原不确定性处理，不把所有异常都改成成功。

### F03：Embedding 一批成功，另一批失败，成功部分也被重新购买

[Qwen Provider](../../src/yuki/memory/embedding/qwen.py#L97) 将输入拆成 HTTP 子批后整体 `gather`；[worker](../../src/yuki/memory/embedding/worker.py#L139) 在异常时把全部 claimed jobs 重排。真实 jobs/worker 的 21 条隔离案例中，20 条成功、最后 1 条 503，最终 21 条都回 pending、没有向量落库；第二轮的虚拟 HTTP 批次仍为 `20、1`，成功的 20 条也重复调用。

方向：优先让 worker 的 claim/结算单元使用现有 Provider 的 `capabilities.max_batch_size`，不跨多个可独立确认的 HTTP 批；该对照的重试批次由 `20、1、20、1` 变为 `20、1、1`。批大小不能对所有未来 Provider 写死 `20`；不为此新增通用付费执行框架。保留原 claim、profile 和 content hash。

### F04：暂停 Space 会阻止读取旧插件会话，也能丢掉已返回的模型结果

[require_session_readable](../../src/yuki/plugin_host/ownership.py#L171) 对 group session 调用 `require_live_space`。[插件会话执行](../../src/yuki/services/plugin_sessions.py#L127) 得到模型结果后，保存 assistant 和再次读取仍走这个条件。

真实公开 Facade + Fake 模型屏障已复现：正常接纳后暂停 Space，模型已返回，但 assistant 未保存，历史读取也被拒绝；新 run 同时被正确拒绝且没有第二次模型请求。需要分开的是旧资料/已知结果和新执行资格。

删除方向：资料读取、已接纳 assistant 保存和结果 DTO 只沿原 session/插件归属取事实，不重新走新 run 资格。这要同时收掉 `require_session_readable`、[get_for_actor](../../src/yuki/plugin_host/session_repository.py#L219) 和 [_session_record](../../src/yuki/plugin_host/session_repository.py#L454) 中混入的 live Space 检查；否则只删第一处，仍会在 DTO 或完成后的 `_get_authorized` 返回时误拒。新 run 开始及实际新效果前的 actor/source 授权继续在执行处核验。不续期、不借其他人物、不伪造来源。

### F05：公开插件 Vision 在原生编辑换图后，可能返回旧图识别

[vision_service.py:273](../../src/yuki/services/vision_service.py#L273) 先按 event/segment 等键返回缓存，尚未核图像内容；[媒体编辑](../../src/yuki/persistence/scoped_event_uow.py#L365) 更新原件与事件，但没有使这条 event-only 分析失效。

真实 MockHTTP TG 入站 → SDK `vision.analyze_current_media` → 原生编辑同一事件换图 → 再读，返回旧观察；Fake Vision 和下载仍各只发生一次。绕过 event-only 快路、保留内容 hash 缓存后，能取新图并返回新观察。**已证消费者是公开 Plugin Vision API；不能据此声称 Main 当前原生媒体输入全部受影响。**

删除方向：删不带原件身份的 event-only 快捷命中，复用实际原件/hash 缓存；保留相同原件的复用和原已付请求，不给附件一律强制重识别。

### F06：消息编辑仍另造一份旧提及人物列表，与原生快照不一致

[update_platform_message](../../src/yuki/persistence/scoped_event_uow.py#L281) 已准备精确 mention spans，却再按裸 `mentioned_user_ids` 和当前 active Assignment 构建 `mentioned_person_ids`，writer 还重复核这份列表。

真实 ingress/edit/read 隔离案例：提及目标账号暂停后，原生 span 保留已知 Person，旧列表却变空；另一个通用 UoW 案例中，没有位置的裸账号列表原本不形成 Person 关系，编辑分支反而造出关系。后一个是 UoW 合同证明，不代表现行 TG 适配器会生成这种输入。

[Social 读取引用](../../src/yuki/social/agent_adapter.py#L161)、Memory 的 `member_*` 引用和历史重建仍有旧字段消费者。Main/JEV 直接渲染走新 span 快照，本次没有证明它们重新发生了上轮真艾特解析故障。

删除方向：消费者统一投影自同一份已准备的原生快照，删除编辑分支的独立重解和 active CAS。保留原作者、内部事件、回执和真实编辑版本顺序。

### F07：artifact 文本搜索把 casefold 后的位置当作原文位置

[_read_text_artifact](../../src/yuki/tool_results/artifacts.py#L656) 在折叠后的字符串中查找，却直接用该下标切原文。真实 artifact write/read 隔离例：`ßxTARGETend` 搜 `TARGET`，返回 offset3 和 `ARGETe`，原文目标实际从2开始。`ß` 折叠为 `ss`，下标已经不属于同一坐标。

方向：统一搜索与分页的原文位置语义，匹配返回原文 span；不要禁止 Unicode、删除字符或把偏移错误伪装成找不到。需要同时检查返回正文、next_offset 和起始 offset，保留完整原件。

### F08：App 真解绑后，旧设备的新消息会被自动记到新造的 Person

[通用解绑](../../src/yuki/admin/operations/identity.py#L421) 能结束 App 阶段。旧设备凭据仍属于原 Person A，能读原资料；但[提交消息](../../src/yuki/client_api/messages.py#L108)交给 [AppGateway](../../src/yuki/gateway/providers/app/ingress.py#L150) 的是裸 App 账号，最终 [Ingress](../../src/yuki/identity/ingress.py#L582) 发现无当前阶段就自动建 Person。

真实管理接口完成合法 unbind → 同一 Client token submit 已复现：返回200，Person数量16→17，作者和 Conversation 从原 A 变为新 Person；设备仍认证为 A。原事件和原回执本身没有被搬家，错误发生在下一条新消息的作者接纳。

这是第一方已认证入口丢失内部作者证明，应优先修。新消息携带设备原 Person、实际入口阶段与 Conversation 进入共同入账链；真结束后的新入口资格明确处理，不能再用账号空位推测新人。旧资料和原成功回执继续可读，不用“全部撤销设备”掩盖断链，不增加身份缓存或另一套 Proof 框架。

用真实领域原语建立“原账号已有另一 Person/超管阶段”的对照，实际 ToolActor 也会变成该人物；但公开 Admin/Client 对 App attach/transfer 均拒绝，本轮没有执行真实超管修改效果。**不能把条件领域状态写成公开 API 已可直接换绑提权**。ACP 共用这段来源重建代码，尚未跑完整 ACP 对称矩阵，不能当作已验收覆盖。

### F09：Social 历史读取沿用了目标的新发送资格

[history.py](../../src/yuki/social/history.py#L53) 解析 target/原 operation 的 Conversation 时默认调用 `service.target(validate=True)`，进入[新发送目标检查](../../src/yuki/social/service.py#L422)：Space 要 enabled，Person 要 enabled 和 known-inbound。这些不等于读取原资料的权限。

真实原发送已经 `succeeded`、event_id=2，仅暂停 Space，随后按原 operation_id 或直接 Conversation 读历史就报 `space_not_allowed`。正确 delegated read_target 的旧 Person 资料也因目标 disabled 被拒；真实已配对、尚无入站的 App Conversation 纯账本空读取被 known-inbound 条件拒绝。原资料和发送成功回执仍在，没有产生新平台调用。

隔离实例只去掉 target 的发送资格校验后，正确 read_target 对 active/disabled 均读到原事件，错误 read_target 仍报 `history_scope_denied`；别的 source Conversation 仍不能借用原 operation。另有 [agent_adapter.py:132](../../src/yuki/social/agent_adapter.py#L132) 对显式 read_target 的同类多余检查，需一起收掉。

删除方向：纯历史读取只作目标/Conversation 的事实解析，保实际 actor、delegated read_target、source Conversation、原 receipt、原事件和 reset floor；移除混入的 enabled/known-inbound 新发送资格。新发送、撤回的真实执行资格不随之放宽。F04 是同一类职责混用在独立插件 Session 的消费者，两个实现入口均要收干净。

### F10：工具 schema 宣称不取远程引用，漏枚举位置却会触发同步外取

[schema validator](../../src/yuki/capabilities/validation.py#L156) 用人工关键字清单检查 `$ref`，漏掉 `patternProperties`、`dependentSchemas`、`if/then/else`、`unevaluatedProperties` 等位置。当前实际 jsonschema 默认解析器仍可检索远程资源。

真实 admit→validate 的六个位置均通过接纳，并各触发一次 `urllib.urlopen` 检索；已用本地 BytesIO hook 截获，测试同时禁止实际 socket/DNS，因此没有访问外网。`examples`/`const` 里的普通 `$ref` 数据不触发检索；当前检查过的 `properties` 分支会隔离 schema。环境 jsonschema4.26/referencing0.37。

第五轮还确认同一 validator 的反向误拒：[_with_lifted_defs](../../src/yuki/capabilities/validation.py#L186) 递归普通 JSON，连 `examples` 数据里的 `$defs` 也提升到根。合法的 `{type:object, examples:[{$defs:{N:{$ref:远程地址}}}]}` 验证空对象本应通过，真实 admit 却 quarantine；原本仅为示例的数据被当成活动 schema，0网络。不是另一个漏洞计数，但重写时必须一起去掉这种普通数据改写。

方向：在真正的资源解析入口使用不做远程 retrieve 的本地 Registry，删除不完整的手写枚举；定义的生成/合并由真实 schema producer 处理，不在 validator 通用遍历并提升任意数据。独立对照保留本地 defs 的真实校验，远程引用不可解析且0检索。不能递归任意 JSON “见 `$ref` 就拒绝”，那会误拦例子和普通常量；不增加插件权限框架，也不把这项扩大为已发生的生产入侵。

### F11：插件临时媒体清理先丢路径，再删文件；一次 unlink 失败会留下永久孤儿

[PluginMediaArtifactStore.cleanup](../../src/yuki/plugin_host/media_artifacts.py#L150) 先删 metadata 并提交，再逐个 unlink。周期清理实际由 [container](../../src/yuki/container.py#L944) 调用；下一轮只按库中行找文件。

真实临时插件安装、有效 PNG 和实际 Store：到期后仅注入一次文件删除异常，行已经删了、文件还在；恢复正常 unlink 再执行 cleanup，返回0，文件仍在。不是“没有清理 worker”，而是唯一恢复线索被过早删除。

方向：原路径记录留到外部删除确认，再作短条件结算；保待发通知/outbox 的真实引用。不要新建删除任务表或另一套状态机。范围仅临时插件生成媒体，不能扩成删除收到的 QQ/TG 原件、持久表情库或工作区文件；本次没有测线上孤儿数量。

### F12：会话重置会吞掉已接纳原生表情的后台收藏

[collect_message](../../src/yuki/emoji/library.py#L84) 在共享原件下载/格式核验后，调用 [source_current](../../src/yuki/conversation/media_service.py#L408)，其中仍要求资料的 generation/floor 与当前聊天视图相符。

真实 TG Gateway→CanonicalIngress→共享 prefetch/收藏下载屏障，期间执行真实 `/ai new`：原 event、segments、可读原件 SHA 都不变，却0 asset/0永久文件；无变化对照正常1 asset/1文件，真实 edit 和 forget 均正确0/0。

删除方向：只删除“上下文视图已重置”对原件资料收藏的限制，继续核原事件、原 segments、原媒体位置、真实编辑和隐私删除。保留24小时原件寿命与实际来源，不加收藏 worker、补偿状态或替代门槛。

## 新发现：查询、等待与写锁范围过大

### P01：一个附件下载，授权遍历本人全部历史，未命中再逐个展开 Work

[ClientFiles._authorized](../../src/yuki/client_api/files.py#L62) 找一个精确 artifact/version，却查询本人全部私聊出站 `segments_json`；未命中再逐 Work 调完整 `_public`，磁盘读前后各做一次。

隔离真实下载中，非目标历史 10→1000，两次授权各返回 11→1001 行，SQL 条数不变也仍是全量数据成本。目标不存在时，1/10/100 个可访问 Work 对应 18/90/810 条 SQL；有效原 Work 的不可变群附件下载对照同样出现36/100/1508条 SQL。不能把一个定点原件查询变成完整个人 UI 的重建。

方向：沿精确原件引用和原发布事实限定候选，批量读所需事实。保留现有两条授权来源：本人私聊的 Yuki keeper 出站原 segments；可访问 Work 的原 domain/成功 Social 资料。不能给前一条统一新增“必须有 SocialOperation”的门槛。保留真实设备资格、同人族、原 version、不可变原件和实际撤权核验，不新增长期授权缓存。

### P02：SSE 每个全局 hint 都重查全部入口；首页沿用 P01 的逐 Work 放大

[ClientViews](../../src/yuki/client_api/views.py) 的首页逐 Work `_public` 与 P01 共用根因，不重复计一组算法问题；独立新增的是同步对全部 entry 逐项 `_activity`。隔离 1/10/100 Work 的首页为 18/90/810 SQL；1/10/100 entry 同步为 11/20/110 SQL。无关 Person 改名的 commit hint 也能触发同一轮同步查询。

方向：完整列表合同保留，读投影改成批量所需事实，并合并已积压的 disposable 唤醒后重读。现有 commit hint 只有 bool，不带主体，不能据它判断哪个人的资料变化；不能靠猜主体、隐含截断、缩短历史或另建必须一致的 UI 状态库解决。

### P03：ACP 没有待发增量，仍每秒重读原触发后的消息尾

[_finish](../../src/yuki/acp_adapter/agent.py#L298) 每轮在 `wanted-sent` 为空时仍分页拉取尾部。真实 Agent + MockTransport 的三轮空增量轮询，为相同 121 个尾部事件重复请求 9 个 HTTP 页面，正文 POST 只有一次，也没有新输出。

删除方向：先判既有增量集合是否为空，无待发原 receipt 时跳过无用尾部查询。原 request 查询、结果流结束语义、原事件去重与取消仍保留。

### P04：恢复某个 QQ 群，却以全部平台连接集合不变为条件

[GroupRecoveryService](../../src/yuki/services/admin/group_recovery.py) 在 membership 网络等待前后比较全平台连接集合，writer 还查询所有 Presence。异步屏障对照已复现：目标群、原 actor、Presence、路由都不变，只断开一个无关 TG 连接，本次恢复返回 conflict、没有 receipt；不发生该断开则成功恢复。

删除方向：只核本次目标、原 source、路由版本、实际尝试的连接及参与唯一性判定的同平台候选；删除不相关平台集合相等的条件。不另加重试层或连接稳定期。它与已知 Presence 枚举 SQL 扫描有关，但**假冲突是本次新增的行为证据**。

### P05：为派发八个会话，调度器先读全部 ready Work

[WorkScheduler](../../src/yuki/runtime/work_scheduler.py#L285) 的 metadata 查询没有 LIMIT，读完再在 Python 选最多八个 scope。实际 method + 当前 schema，10/1000/10000 个 ready scope 都只派发八个；第一条查询却返回 10/1000/10000 行。Linux SQLite3.46.1 的 VM 为 732/62112/620112；Windows SQLite3.53.1 为 743/63113/630113。

方向：SQL 按真实可执行会话选有界的最早 head，再保留同会话顺序。不能简单对原 Work 加 LIMIT8，使单个会话占满候选；也不改无限并发、不加资格缓存。这里是 ready backlog，不冒充全部终态历史扫描。

### P06：查一个 run 的完成状态，在写事务内展开整个任务子树

[deliver_due](../../src/yuki/runtime/work_wait.py#L995) 的 writer 里再次执行 `_observe_page`；递归 CTE 先遍历 owner 全部子孙，再挑一个已知 run。固定一个完成结果，兄弟任务 10/1000/10000，Linux VM610/46150/460150；同一数据库中复用已有 `ancestors(known_run)` 点查比较为122/122/122。

方向：先定位真实 run，再沿其父链核所属关系；尽量在 writer 外准备，只在提交时核事实版本和真实归属。这个对照不是现成完整修复，sandbox run、多 owner 和 lease/generation 都必须保留。其他 control/subagent 的 descendant 谓词也应沿真实调用逐个收窄，不能一刀切删除所属校验。

### P07：每次发布工作区快照，都在 manifest writer 内重复跑旧 TTL 转永久

[_prune_metadata](../../src/yuki/workspace/store.py#L130) 的旧转换和到期清理在每次 `snapshot` 的 `BEGIN IMMEDIATE` 内执行；当前新记录本来全部永久。它扫描的是独立工作区 manifest，不能写成占住 Yuki 主业务库写锁。

真实 `snapshot` 的 10/1000/10000 条永久 manifest 规模中，旧 UPDATE 为50/4010/40010VM，到期 DELETE 为49/4009/40009VM，snapshot 孤儿 DELETE 为84/7014/70014VM。方向：旧资料转换留在真实一次性升级/激活边界，当前发布不反复运行退休 TTL 扫描；按实际生命周期保留必要孤儿文件 GC，不能把有用途的原件枚举全部删掉。

### P08：部分 ASR/视频处理仍在 Bot 事件循环做同步文件工作

[ASR prepare_audio](../../src/yuki/asr/audio.py#L103) 和[video_frames](../../src/yuki/services/video_frames.py#L130) 的 async 路径有同步读写。前者可在语音入站接纳调用，后者是按需选视频，不能混成“所有附件自动读取”。

受控把文件操作延迟设为120ms，heartbeat 最大停顿约123ms，证明它确实堵共享 loop；这是注入值，不是现网媒体耗时。方向：沿已有媒体 I/O 所有者完成阻塞操作及临时文件清理，避免共享 loop 停顿；保留 source/hash 与真实 Provider/资源预算，不新建权限队列。

### P09：Dream 增量规划没有变化，仍计算全体两两相似度并堵 Host loop

[_clusters](../../src/yuki/memory/dream/service.py#L534) 已知道 changed 为空，却继续两两比较、保存每对分数、组簇，最后才丢掉所有未变化簇；每一对还重新归一化两份向量。`max_clusters_per_run` 在规划后才截取，不限制这段工作。每日 Dream 与手动规划都可进入，发生在 writer 外。

真实当前函数，完全不变的10/100/300条、32维规范向量，输出全部为空，仍调用45/4950/44850次 dot；同 loop heartbeat 最大间隔约1.75/116/1110ms。这里没有注入磁盘或模型延迟，但仍是合成输入，不能称线上事实规模或现网一秒停顿。

方向：增量规划跳过没有变化的整个分区，删空转；含变化的分区仍保留全部相关邻居，不能只聚类 changed 子集。每向量只归一化一次，再用已有 normalized dot，保持原排序、阈值和聚类结果。完整规划也共用这段 CPU 路径，剩余长 CPU 不占 Host loop。完整任意精确聚类仍有真实两两成本，不承诺神奇线性算法，不加固定资料上限、ANN 服务或持久相似度缓存。

### P10：等价自动化查询深 OFFSET 反复遍历前面的候选

[find_equivalent_directory_entries](../../src/yuki/automation/service.py#L99) 需要显式完整比较目录，这个全查本身有用途；问题是按不断增大的 OFFSET 多次重走/排序前缀。

真实 service、实际任务/owner与同一规范比较，50/500/2000条当前任务、固定0命中：2/11/41次目录 SQL，总 VM13060/346465/4704565。同一 projection/status/order 一次读取相同候选的对照为5292/52542/210042VM。

方向：一次一致的流式遍历或真正可 seek 的内部遍历，保原 owner-independent 目录及完整规范比较；不能仅加 cursor 条件仍每页全排序就声称线性，也不能用隐含截断或另一份等价 hash 状态掩盖成本。界面分页与这个内部完整搜索分别处理。

### P11：子任务每轮维护，在没有待归档项时仍递归旧归档树

[SubagentScheduler](../../src/yuki/runtime/subagent_scheduler.py#L89) 每轮先维护、轮尾 sleep1s，实际周期还包含本轮耗时；[maintain](../../src/yuki/runtime/subagent_repository.py#L187) 的递归起点是全部到期终态根，已归档过滤与 LIMIT16 都在展开之后。

真实 maintain，10/1000/10000棵已归档树、各带一棵仍有 waiting 后代的对照树，没有新归档、全行 hash 不变：递归查询 VM2001/166340/1660341；另一个 missed-notification 查询为223/14083/140083。每次只有 reader SQL，**0 writer/0 DML**，不能写成此空轮询占写锁。

方向：从真实未归档候选倒查相关根/祖先，保活动后代、原回执和真实保留语义；删除重复历史候选工作，不加归档缓存、暂停期限或另一份进度表。

## 删除候选和需要区分的策略

- 插件一个默认 `NEVER` 后台 runner 出错，目前会停止整个插件、取消其他 runner、移除普通工具；`ON_FAILURE` 对照只重启出错 runner。行为已离线确认，但现行合同本就允许插件失败传播。应作为**失败范围过大的策略整理建议**讨论，不能冒充所有 Host Work 都被取消的已证 bug。
- [canonical_repository.py](../../src/yuki/identity/canonical_repository.py#L216) 的九个旧裸号 helper：`require_person_binding`、`require_space_binding`、`representative_external_account_id`、`external_accounts_for_person`、`person_id_for`、`space_id_for`、`active_person_id_for`、`active_space_id_for`、`presence_id_for`；另有 `services/admin/common.py` 两个 require 包装。它们没有现行生产消费者，只有少数旧测试/fixture 调用，可连对应测试一起删除；`bindings_for_person` 等仍有真实隐私消费者，不能整片误删。
- `gateway doctor` CLI choices 接受 Telegram/App/ACP，实际兼容诊断只认 QQ SnowLuma，其他值抛 `ValueError`。修正声明与实际诊断能力即可，不影响 TG 真实收发，不需要新 Provider 控制框架。
- `_MAX_IN_FLIGHT=8` 是资源并发配置；operator 声明最多64人、权限目录搜索最多64字符是可评估删除的经验限制，三者不混为一类。目前没有这些上限造成实际业务事故的证据，不仅因为“有数字”就列严重缺口。
- `ConversationTurnCoordinator._states` 随一生见过的会话数保留小状态，是容量观察项，没有现网内存事故证据。不能直接 TTL 清零，旧 token 可能与重新创建的版本碰撞。
- 大型 artifact 全局容量 SUM 和发布锁有真实总量核算用途，先量化再决定；不建议加另一份易漂移的计数器来掩盖成本。
- 独立 PluginSession 的 `metadata` 被 SDK 接受却未传入 Host；`max_tool_calls` 被忽略，当前运行层 `tools=None`。适合清理无效公开字段与“为未来后端保留”的交集链，不能误删有真实预算消费的 `max_model_requests`，也不能声称当前已承诺独立工具后端却没实现。
- EventPromptIndex 还在热路维护 sender 前驱双列表和历史 Bot 账号推断；前驱 API 无生产消费者，Bot 推断最终透传到未读取该参数的 `event_content`。隔离 AST 删减副本经真实 history extend/capture/render，10/1000/10000 条输出逐字相同；10000 条 owner 的 tracemalloc 保留分配少918360B。这是 Python 分配量，不是 RSS、模型 token 或缓存命中率。可删整条派生索引与透传；保原 event-ID 引用、capture水位/ordinal、补洞/overlay，以及另一条仍有用的入站 `replies_to_yuki`。
- Gemini 序列化每条 result 反向找 call 的访问量为R²，且 base history 转换两次。真实构造R4回执约0.20ms，R1024约160.63ms；后者是合成极端规模。一次 payload 内局部 lookup/一次构造历史有优化空间，保 opaque 和原配对，不加工具次数 cap，不作为当前线上瓶颈结论。
- Work 检查点对旧模型观察反复 JSON/hash：48条、总约1.58MB观察的一次无变化 save 仍编码1.58MB，但新增物理文件为0，SQLite manifest仅472B，现有物理去重有效。编码/hash约7.45ms为本地仪器测量，保留为 P3；最新观察在工具结束时还会追加 results，不能按 mutable dict 的 object id 缓存而丢掉已付结果。

## 前次已知、仍未修复的八组 SQLite 问题

详见[此前的原 SQL/VM 审计](sqlite-query-scan-audit-20261020.md)。本次对照当前实现保留，不重复算作新发现，也不冒称本次已修。

| 已知项 | 当前触发 | 收窄方向 |
| --- | --- | --- |
| 入站名称观察读取该账号全部旧名 | 每条入站准备 | 只批查本次规范键 |
| 私聊缺 Assignment ID 时回查扫描其他阶段 | 特定私聊 fallback | 唯一 Account→原 Assignment |
| Presence/群候选遍历平台账号或全部阶段 | 路由发现、接管、合并 | 少量真实 Yuki/Presence 候选驱动 |
| 暂停插件 Work 的 due Job 池空轮询 | 默认每秒 | 先选可调度原 Work，不加暂停期限 |
| Memory 到期发现扫永不过期事实 | 默认每300秒 | 原 status/valid_until 范围索引 |
| Reflection 空 claim/retry/report 扫终态历史 | 默认每60秒及相应分支 | 现行真实谓词对应索引 |
| CodeMode 子效果绑定 JSON 路径而错失表达式索引 | composition 恢复 | 程序固定路径作 SQL 常量，数据继续绑定 |
| 投影容量聚合扫描失效旧 views | 新投影发布 writer | 活跃谓词/淘汰索引，不另存计数状态 |

## 已反证或必须保留的边界

- 固定工具声明与实际执行授权分开；工具可见不等于有权执行。没有发现需要恢复按人裁工具声明的理由。
- 普通 complete/wait/need_input 不截断原响应后续获准工具；真实 fail/cancel 具有不同语义。没有要求模型额外汇报或满足经验产物门槛才完成。
- FrozenFragments 已用共享 chunk 和持久 AVL ID 集合；不是每轮复制全部旧前缀。普通追加不推进 `prompt_source_revision`，不能声称每条新消息都使 source guard 全量重算。
- 本轮 runtime `role=user` 是已知协议/缓存选择，不因字段名就报成新 bug；没有改 role 或冻结前缀。
- 普通图片/视频/文件按需读取；入站 ASR 依显式配置。表情选图/描述在后台，普通文字无需等它。媒体相关缺口必须按真实消费者区分。
- 终端观察超时不是终止原命令；主终端默认无限期，可自由操作持久环境。CodeMode 的 Host 隔离、引擎格式和 snapshot 来源是另一份真实协议。
- 并发锁、effect 锁和 Processor 的普通输入锁使用弱引用，未证空闲锁泄漏。诊断队列满只丢诊断并标 coverage，不丢交付。
- 未知派发不能自动重发，缺 cache usage 不能算零命中；原 event/Person/Work/receipt 不能用今天的平台绑定重建。

## 验证与证据边界

离线环境：Windows CPython3.12.13/SQLite3.53.1；已有 Linux 验证容器 CPython3.12.15/SQLite3.46.1。只用 Fake 模型、MockTransport、临时 SQLite/文件和受控故障。诊断测试“通过”是在证明当前错误可复现，**不代表产品已修复**。

| 阶段 | 证据 | 能说明什么 |
| --- | --- | --- |
| 两次广审 | 647 文件的独立分区、两次源 hash/结构扫描；第三轮轮换审查人，另沿真实入口和消费者人工追查 | 覆盖模块和调查路径，不是每行五次形式化证明 |
| 第二轮 | A 12种独立参数情形、B 10个诊断、C 3个 witness、主审2个编辑 witness；另有脚本 SQL/正文对照 | 当前行为的隔离复现；A 含重复执行不按15个独立问题计数 |
| 现有回归 | B 82个 Memory/Dream/Reflection/Rebuild 目标回归、C 34个协议/CodeMode目标回归通过 | 支持对应已保留合同，不替代全仓、CI或生产验收 |
| 第三轮 | App 错作者与 Social 误拒的两条预期合同探针为红，保留原日志 | 当前确有反例，不把红探针说成修复已绿 |
| 第四轮 | A 14个 schema/贴纸诊断和9组 wire 对照；B 11个 App/Social/Session诊断；C 4种 pytest情形及完整索引副本对照；主审5种 Dream/自动化/文件清理情形 | 深查第三轮候选、真实消费者与删除后果；诊断通过表示错误可复现，scratch 对照未写入产品 |
| 第五轮 | 三名子 agent 交换前四轮分区核源码/证据，主审核范围、计数、链接、基线和报告；另有合法 schema 注释资料误隔离的纯离线反例 | 已完成：补齐 Session 三处消费者，纠正 bool hint 作用、附件既有授权来源、schema 普通数据边界及测量分类；未增加产品修复 |

本地证据目录：`.cache/backend-audit-20261020/`，含分区、源 hash、各轮记录、探针及日志，未提交。探针准备中出现过漏 fixture 外键、未展开 SQLAlchemy POSTCOMPILE、Windows临时 SQLite 连接关闭等诊断错误；修正探针后只重跑对应项目，原失败日志保留，不计产品缺陷、不冒充初次全部通过。

调度/任务树的增长探针隔离了 SQL 和选择过程，不用它们证明入站认证成功；App、Social、媒体等身份/来源结论另有真实公开入口、当前 schema 与实际消费者的隔离案例。

终审确认：12项 F 与11项 P 是本次列项，不等于23个互不相关根因或23次线上事故；八组既有 SQL 问题另列。所有647个源文件终审 hash 与开审相同，审计阶段正式源码、测试和迁移未改；相对链接及行号已检查。各轮保留 A/B/C/主审四份记录，不把四个并行分区当四轮。

本次未访问生产账本，不能给出线上触发频率、真实锁等待、附件 orphan 数、现网缓存命中率或端到端提速承诺；模拟故障和合成规模均在对应项目标明。没有产品修复、CI结果、提交、PR、合并、上线或真实平台验收。五轮完成的是当前源码调查与报告交付，不是所有并发时序、外部依赖与历史协议都已证明无缺口。
