# 持久工作者与共享预算

异常、分段、交付与检查点容量遵循 [主 Agent 执行与恢复合同](main-agent-runtime.md)。
不再维护子 Agent 独有的 SQLite BUSY 重试或旧沙箱进度预算。

主 Yuki 用 `subagent_start` 登记并派出子任务，继续处理聊天；通过
`subagent_control(action=message)` 补充要求，通过 `subagent_control` 查询、取消或恢复同一目标。
派生本身已经登记工作，工作者无需再次 `task_control.accept`。

工作者沿部署的 direct / Code Mode 执行，复用 `MainAgentContract` 的完整声明、核心 system、
原入口的 ScriptApi 和 `MainAgentBackend`。App 子任务保留 App 的工具合同，平台子任务保留平台入口的合同；不会把 App 请求换成 QQ 声明。`lookup_tools` 查询同一已提交目录，不维护独立工具
上限。子目标、父子关系和 cwd 作为 brief 资料；实际来源与权限由 Host 绑定，模型不能
通过资料包取得额外身份或授权。pending 沿原 run_id 等待，不以新脚本重做已提交子调用。

用户要求继续聊天不表示取消后台工作。主 Yuki 发言使用 `send_message`；
`task_control.answer` 不在公开 action 中。Agent 提交结束决定后，原 writer 只收拢真正活动的所属执行；没有活动执行的暂停/等待子目标保留自身状态，不否决父目标完成。fail/cancel 直接复用指定子树撤权，无需先逐一取消后代或等待远端全部确认。

可信 SQLite BUSY 在同 activation 持有原响应时仅重备原纯数据库发布，沿原租约与取消退出；已 durable response/paired 按原 ID 恢复，未持久且进程退出的响应不承诺找回或自动重购。LOCKED 和其他存储错误如实退出。
子任务状态和调用次数不是修改结果；仅列目录、读文件、
计算摘要不能报告已删除、清理比例或释放空间。回复效果记录沿内部事件 ID 关联，身份查询在首次写入前完成。

## 工具与授权

工作者使用 Main 的固定工具合同，完整 schema 和工具顺序保持一致。原生或外部联网能力
沿当前部署与 Profile 的实际声明，不在 worker 路径额外裁剪。工具执行按 child Work 保存
原 invocation、调用键、结果和来源；direct 与 Code 路径共用原执行后端。

`workspace_inspect` 读取获准工作文件或不可变 artifact，图片及视频帧交原工作者模型，
不另调视觉 Provider。媒体读取最多 20 MiB，主请求仍计入根预算；更大视频可先通过
终端提取帧或生成较小文件。来源和版本规则见[聊天媒体](chat-media-workspace.md)。

工作者可沿真实来源与授权使用统一 Social 发送、撤回、写长期记忆、修改 short_state、管理
自动化和使用已批准插件。Person、SELF、automation 与插件来源分别核验自己的 actor、
目标、能力批准和权限。固定 Main 合同不再声明 raw OneBot 管理入口；独立批准的插件 QQ 扩展只在真实 QQ 传输边界执行。
主、子 Agent 在同一会话交错发言时，正文与回执仍归各自原执行，已确认发送不重复交付。

后台执行沿原内部 Conversation、Person/Space、Presence、event/run 和 Work 租约核验。
断开连接、同 Presence 重连或活动发送路由变化不终止纯内部执行；真正发送时才解析、核验
当次平台连接。群入口保原 SpaceBinding 与 topic；平台群地址迁移不改变原业务来源。
迁移前私聊曾由两个 QQ Bot 共用 Conversation 的真实旧执行，只能用原内部事件、原
Presence 与旧持久 alias 证明其入口；新的私聊按 Binding + Presence 分开，不借旧 alias 合并。

子 Agent 用自己的 Work 作为嵌套子任务的父节点。`subagent_control(action=message)` 指定 child_id 时联系
自己的子任务；省略时联系自己的父任务。实际提问保存后，只有父任务仍非终态时才接纳 `waiting_parent=true` 并提议子任务等待；父已经终态时仍保存问题，返回 `waiting_parent=false`，不暂停子任务或复活父任务。同一原批次、程序中剩余获准的通信、派生、保存和交付继续收拢。子孙通信和管理核验
真实方向与树关系。只读 Work 目录使用 Main 的同一范围，目录信息不授予修改或取消权限。
工作者默认跟随 Main 模型；Provider/合同变化沿原合法边界恢复，不暗换协议。

工作者也可用 `task_control.wait(conditions)` 等待时间、当前会话、获准插件事件或自己的 run；信号仍投递到原 child Work，由现有调度器恢复，不另建任务。

## 生命周期

`YukiRuntime` 与所有会话共用主 Runner，拥有 `SubagentScheduler` 的启停。
调度器负责有界候选与容量；`SubagentExecution` 显式取得工作者需要的能力，
负责来源恢复、Memory session、Main 合同绑定与原结果回传。它不访问整个应用定位器。
child 使用自己的 lease，在取得执行权后复用 root 的激活监督、续租、恢复和释放骨架；
关闭 runtime 收拢执行时仍保留原 child/root ID、预算、回执与未知效果。

0057 引入工作者资料、独立租约、根预算和媒体表。0105 将原关系迁入共同 Work.parent_work_id，工作者表不再混用 root_id。预算根按祖先关系读取，普通派生目标与工作者共用原树查询和管理。
父子通信复用持久输入日志：消息 ID、关联问题、消费状态一起保存，重复通知不重复消费。
工作者向仍可接续的父任务提问，原批次与程序收拢后进入 `waiting_parent`（存储状态为 `waiting_user`）；等待命令时进入 `waiting_external`，均释放执行名额。真实子问题属于父任务业务输入，可经原输入与 writer 接续尚未结算的结束决定或等待，既有 active wait 不遮住问题；普通进度和完成通知仍按辅助输入保存，不撤销完成或复活终态。父任务回答或命令回执到达后续接原历史。
已接纳的提问等待复用原 `accepted_control` 和调用键持久保存，后来的真实提问按原控制顺序接续子任务，不因分段或崩溃丢掉等待决定、重发问题。终态父返回 `waiting_parent=false` 不撤销子任务原完成决定；父的独立 `suspended` 原因也不自动解除，问题保留供显式 resume 消费。
暂停或等待父任务的子任务可直接 `resume` 原目标，无需新增 instruction 或伪造补充消息。
完成后同一 ID 可追加真实新要求，此时 `resume` 携带 instruction。
尚未归档的子节点沿原 ID、来源和累计预算继续，不重开已完成祖先；其他暂停树不阻挡，执行沿原租约。
普通前台没有当前 Work 或正在处理另一项 Work 时，查询、读取结果、取消、补充或恢复原有子节点均按原内部 ID 与本地来源授权解析目标，无需先接纳新 Work。列表按原来源列出实际可管理的子节点，工作者仍限于自己的后代；共享目录中的安全元数据不授予恢复或追加指令权限。新派生仍登记真实当前父节点，不借旧目标管理创建空父任务。

工作者租约不占聊天会话锁；取消和会话 generation 变化使旧租约失效，迟到结果不得交付。
恢复时读取原执行回执，未知执行结果先查询原 run_id，不能重跑。
根任务未结束时保留子任务；终态归档按真实引用与保留期清理；仍暂停或等待的后代保留恢复资料，不能因父任务结束顺带归档。
归档 ID 明确返回已归档，不自动创建替代 Agent。

子任务完成不强制附加正文；原目标资料与效果、产物交付查询保留，业务结束沿原决定提交。activation 结算后，父输入与完成回执只读取已提交 Work 行（completed 读 `sync_result`，failed 读原因），通知前崩溃由 maintain 按 notified_revision 收拢同一结果。完成回执保存结果摘要，不再次复制完整 Work checkpoint。仅单项 `result` 查询按原
child Work ID、父子归属和通知 revision 读取对应 checkpoint；恢复后的新版本不冒充
旧结果。状态与列表查询不加载大笔记，旧已归档结果中已有的快照仍保留。

默认全局两个工作者；子任务按现有调度器排队，不设固定入队或每根任务数量拒绝。
目录按内部 ID 分页，
执行收拢读取真实所属执行，不能以目录页面代替执行事实，也不要求全部子目标成功。
模型并发为一时沿现有模型调度串行接纳。
每段模型请求与业务工具限额沿当前 `agent.max_model_requests` 和 `agent.max_tool_calls` 热配置；到限保存并重新排队。
新根任务及其全部子任务共用累计计量，默认模型和业务工具累计限额为 null（无限），
定时到期发生若接纳 Work，其原工作树沿同一累计计量规则，不每段重置计数。
显式有限预算仍原子执行，不额外扣除固定 8/8 的收尾预留。
0087 保存的旧工作预算继续按原根 Work 计量；旧定时第二账本只保留存档事实，不再叠加扣费或执行限额。
不能把部署升级当作模型充值，也不能用旧镜像覆盖新写入的执行与预算事实。
失败请求、总结和含媒体的模型请求计费；等待、重启和恢复不重置预算。
预算耗尽仍可保存回执、维护租约和取消，不要求额外预算。

## 提示词与缓存

固定提示词、完整工具顺序/schema 和 Provider 设置在资料包之前。
工作者的固定提示词包含与主 Agent 相同的 Memory 使用规则，新的记忆查询回执不重复附带该规则。
工作者按 Main 的方式准备当前运行资料；既有私有协议不被新 short_state 或目录改写。
每条链保存实际协议 continuation；普通唤醒只追加，合同或 Provider 改变时建立新链。
图片按内容哈希单独保存，恢复时还原原始协议内容，不能因内联图片丢掉整条链。

恢复检查点保存协议 manifest；原协议项、图片、模型观察和压缩前链分别
存放为不可变文件对象，按原 Work 持有引用，shared hash 在最后拥有者释放后才可清理。
对象单件默认 64 MiB，总登记容量默认 2 GiB，写新文件保留 64 MiB 空闲磁盘。
启动配置为 `WORK_PROTOCOL_OBJECT_MAX_BYTES`、`WORK_PROTOCOL_TOTAL_MAX_BYTES`、
`WORK_PROTOCOL_DISK_RESERVE_BYTES`；
对应 `storage.protocol_object_max_bytes`、`storage.protocol_total_max_bytes`、
`storage.protocol_disk_reserve_bytes` 只允许全局热覆盖，在下一次检查点准备批次读取同一快照。
降低限额保留已有对象、读取与引用复用，只拒绝超限的新对象发布；调整政策后可原位重试。
计量属于资源约束，容量不足暂停并保留
最后可用检查点，不能因磁盘配额触发语义压缩。活动 Work 的旧输入媒体引用保留到归档或隐私删除，
不能由较早的 save 准备快照删除并发新输入。终态归档及 canonical 隐私清理释放对应引用，
GC 有界标记未引用对象 deleting，再在 SQLite writer 外删除文件并收尾；中断可续清。

完整请求超过真实容量时走 Main 的 compact_current，保留初始材料、原回执与新旧链关联；最近一次执行检查点仍归该 child Work。
缓存由 Provider 决定；续跑持久化不代表 Provider 缓存永久有效。

插件后台 child 沿原 Job 及持久后代关系恢复，不把后台工作伪造成真人消息。
SDK child 使用原 plugin_invocation 来源，并核当前 live Host 的批准版本、允许工具、Memory
范围和上下文读取合同；插件退出、批准变化或权限失效不授予新执行。SELF 和定时来源
保留原主体、run 与执行来源，网关按真实 Presence 解析，不能伪造 automation_id。


## 配置、验证与回退

`SUBAGENTS_ENABLED` 默认关闭，只控制新任务接纳；关闭后已存在的任务仍收拢运行。
`AGENT_MAX_MODEL_REQUESTS=24`、`AGENT_MAX_TOOL_CALLS=32` 更新旧环境里的显式配置。
主、子任务窗口由所选模型 Profile 的容量合同统一提供，不保留子任务独有的默认窗口。
`SUBAGENT_CONCURRENCY` 由调度器控制后台激活并发，收尾 reconcile 租约不占执行名额。
同一 SQLite 数据库只允许一个活动 Application；仓储仍核验每个工作者的原执行租约和 generation。
模型池仍保留前台名额；关闭调度器取消并等待
所有活跃 worker 收拢，错误由各次激活返回，不存放在共享执行器的当前状态。

定向协议测试直接比较 Chat Completions、DeepSeek Responses 和 OpenAI Responses 的
真实序列化 `messages/input`、完整工具和原生工具声明，而非只比较哈希。
覆盖跨段、父子交流、32 次执行、媒体恢复、预算竞争、取消、回执修复和七天归档。
真实缓存审计应使用隔离数据库与选定 Provider 的实际 API，不启动第二个生产 Bot，不发送 QQ 消息。
生产网关、RSS、Manager 和持久环境必须在更新 Bot 后复核。

部署前停止 Bot 并做一致性数据库及配置备份，使用目标镜像执行 `yuki init-db`，
升级至随包 Alembic head 后更新 Bot；0057 是工作者表的历史引入版本，不是当前升级终点。
回退先关闭新派生并收拢任务，保留当前数据库、工作文件、执行回执和预算。
恢复镜像必须兼容当前存储合同，不能用旧数据库覆盖上线后的写入；迁移 downgrade
也不等于旧代码兼容承诺。部署流程见 [当前升级指南](../upgrade-3.9.0.md)。

取消對帳查詢使用 waiting 且 run_id 已知的部分索引，不掃已完成的 SandboxTaskRun。Manager 尚未返回終態時仍按原 run ID 取消；Client 收到原終態後先保存既有完成回執，下一巡回不再選中它。
