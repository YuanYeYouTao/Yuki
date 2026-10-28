# 消息身份与投递链路续审（2026-10-28）

审查基线为main `afb509e7c96f1949651b505e601758f14d2f8fd8`。本轮在全仓ID审查和PR #526执行复审之后继续核设计，产出[实施任务书](../architecture/Yuki-消息身份与投递链路收敛任务书-2026-10-28.md)。运行时、迁移与SDK尚未改动，本记录不作为修复验收。

## 复核范围

第一轮全仓检索覆盖消息/运输键285个文件、985行，其中75个源码文件；另沿151个账号/Space字段源码文件的实际身份消费者扩展。字段命中数不是缺陷数。原生入口、Social/outbox、Client、Work、Memory/Trace、SDK、随仓插件及模型/迁移/投影分别追踪。

第二轮重点查方案：本地事务是否真的需要远端claim/空发送；旧source fingerprint能否保真；原request幂等与native引用如何分开；哪些历史冷列仍有唯一原件；账号解绑究竟影响新输入还是已接纳Work。

第三轮由三路负责人逆审完整任务书，包含互相的边界。反馈不是只调整文案：补了原内部引用与分段两处native门槛、旧PREPARED错误引用、通知最终lease/远端晚到事实、pre-ledger SDK事件和旧actor JSON reader等具体消费者。最终修订状态见下表。

| 审查 | 实际发现/排除 | 任务书落点 |
| --- | --- | --- |
| 全仓语义与反例 | App通知空native门槛、App本地写失败假成功、QQ file内部key进入native reply | T01/T03/T04/T05 |
| 原生namespace | 可碰撞账号内凭据的同群换接收账号引用条件缺口；TG同chat跨接收账号同原件引用正例通过 | T06，禁止全局samePresence门槛 |
| 完整接入key | 129字符同前128不同号冲突；SDK非空/128限制和str(None)也要同步 | T01/T02/T07 |
| 原来源/已付回执 | Memory/Rebuild hash实际包含旧平台列，简单清NULL会误报source_event_changed | 原列保值改origin_key，T02冻结codec/动态投影 |
| Work准入产品链 | 原绑定禁用后已接纳Work仍可续原请求；未ready新输入另按资格处理 | 保正确路径，不列身份漏洞；T08回归 |
| 自动化历史资料 | 139条记录中109条旧来源非空，4条没有可替代版本来源/原事件 | 活字段可删，原冷列保留；T07 |
| 整书逆审：分段与引用 | service.py:758删内部reply、:1397按native引用决定是否传内部ID，nullable后会丢内部关系 | T05点名同时改，验DB/SDK/历史内部引用 |
| 整书逆审：旧prepared | 原prepared_transport可能重新取出旧fake reference | T05在原未派发operation边界按原内部引用重新投影，不改旧JSON/hash |
| 整书逆审：通知 | local最终writer必须核真实attempt/lease；远端已确认的晚到不能因lease失效被否认 | T04分开新本地资格和远端已发事实 |
| 整书逆审：SDK/旧数据 | CurrentMessage之外还有pre-ledger事件payload；旧actor JSON删字段后可能TypeError | T07同步事件及原reader，不伪造内部event/不移动hook时序 |

## 已确认的旧缺口

App adapter返回None/remote_confirmed=False，但通知worker仍强制message_id非空；普通Social仍无条件confirmed=True，随后本地账本失败会补SUCCEEDED。QQ文件成功ack无message_id时，内部social-operation键或file资源号又被reply_reference送入OneBot reply.id。它们是旧消费者没有收净，不是所有外部ID都错误。

平台消息号用于真实收件去重、平台引用和撤回是必要的。Work/history/Memory/Trace当前主要使用内部锚点；旧work_delivery/repair_receipt_ledger已退役。App/ACP原request key是真实客户端协议键，不可误删，也不能当成可发给QQ/TG的原生引用。

通知媒体尚未形成稳定artifact/version、QQ本地写失败仍被记unknown、配图commit取消窗口、cache打开竞态、锁跨UID与TGS代价等，继续纳入任务书独立施工/验收，不能随身份字段改完自动销项。

## 本轮新增验证

数值和限定见[证据JSON](message-identity-delivery-evidence-20261028.json)。

- 已有边界：入站原回归46 passed、1个Windows不具备的Linux设备用例跳过；2个新引用/长key反例，另有1个JSON保存用例。反例通过表示确认错误，不表示修复完成。
- QQ文件：真实Social/SQLite/adapter的两种fake引用参数反例；平台替身返回成功，不能据此宣称真实QQ接受这些参数。
- Runtime：六项正向合同探针，确认内部event/execution、历史选择器及本机native资源namespace，不靠平台号补业务来源。
- 新续审：Processor→task_control accept→WorkScheduler与原mailbox路径3项通过；已接纳Work解绑仍推进原请求，原身份不变，未ready输入另判当前资格。
- 新续审：真实TelegramClient编码＋数据库的同chat跨接收账号引用正例1项通过，网络调用0次。
- 新续审：只让native引用helper返回None、其他产品实现不改，单段和分段发送都会丢内部reply_to_event_id，2项隔离探针确认了这两个下游门槛。因此任务书同时要求验平台参数和实际账本关系，不能只验“不发fake reply”。
- 新续审：三组native、旧App信封、旧local ledger对象，只机械替换fingerprint属性为origin_key，捕获实际SHA输入bytes；原codec bytes与hash完全相等。**这是方案等价实验，不是已完成迁移。**
- DTO探针确认现有reader会将NULL转成字符串`"None"`，当前SDK8拒绝None和129字符号；这些是T01/T07必改消费者。
- 自动化只读聚合未导出任何原账号、消息号、正文或authority原件。4条独有历史来源保留，不以版本JSON必然完整为假设删除。

隔离实验使用当前源码、fake网关与临时SQLite；仅在测试进程指向已有同项目Vec1 DLL，不更改应用实现。原脚本、JUnit、各分案和逐轮意见保存在本机 `D:/Code/Reports/Yuki/id-taskbook-review-20261028/`，前轮在相邻external-id-audit及emoji-execution-review目录。便携证据只保必要的合成数据、布尔结果及聚合，不收聊天原件。

## 本次选定的实现方向

旧ChatEvent平台列原样改名origin_key，保存真实旧信封字节与冻结hash输入；新增native_message_id，只存确证的原生引用。原收件receipt平台列改名ingress_key，去截断并保原请求namespace。内部业务继续用原event/operation/outbox/request，origin不成为新的业务主键。

本地Social不再先commit EXECUTING、调用成功noop、再补账本，改成同一短writer里的claim＋append＋finish；本地新路径不留单独EXECUTING恢复窗口，可删除平台列扫描。通知保现有processing准备租约，媒体冻结在外，final writer核当前attempt/lease并原子append＋sent；远端才调用native transport。

这是删除重复生命周期，必要的数据区分服务于真实原件/平台引用。没有新增ID框架、恢复表、影子账本、local队列或模型参数。准备、native字段回填、SDK9配套和旧行收尾的实际实施/验证仍须逐项完成。

三路对最终修订的重点反馈已逐项收敛，出站和入站负责人末轮只读核对未再发现实质矛盾。Runtime的冻结来源/旧actor读取及自动化冷列新写问题由主审复核：原冷列NOT NULL且无默认，任务书指定唯一writer写空值，不假定现有schema自带默认。该结论仍是设计复核，不代替之后的实现验收。

## 桌面消费追加范围

按用户后续要求新增T13，覆盖两仓的真实富媒体消费，详见[桌面富媒体审查](desktop-rich-media-audit-20261028.md)。任务书现共13项实施任务：后端T04仍负责通知稳定原件，T13补事件尺寸/MIME解释、桌面媒体读取/流、出站图片内嵌、音视频/PDF及公式/图表/内部Markdown图片；包括实际显示、播放、保存和性能矩阵。桌面基线为main`1d42784`，不是本机旧checkout。此追加仍仅为文档，没有实施或运行真实桌面验收。

本轮产物仅为任务书、续审记录、证据JSON及架构入口更新；未运行新代码CI、提交、推送、提PR、合并或部署。
