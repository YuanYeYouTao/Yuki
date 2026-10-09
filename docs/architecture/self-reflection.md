# Self Reflection 执行与积压治理

Self Reflection 使用 canonical owner、内部事件范围与持久 run ID；不使用平台消息 ID
登记工作。schema 0061 增加 cycle、实际请求账本、源批次重试和恢复检查点。
SELF 自主证据扩展已有定向验证；T20 真实 QQ 社交效果仍须单独验收。
迁移顺序为 `0065`（关系历史索引）→ `0066`（autonomy 接纳）→
`0067`（SELF 工具来源与自省水位）→ `0068`（内部引用事件）→
`0069`（Social 回执内部事件关联）→ `0070`（无来源机会与讨论线程）。
实际线上版本以当前容器镜像与数据库迁移记录为准。

## 结构化生成

`memory_self_reflection` 使用独立模型任务；结构化输出格式由当前绑定的 Profile 决定，
不再强制 JSON Schema，也不在任务中开启文本 JSON 降级。Chat、Responses、Claude、Gemini
复用通用 Provider 格式转换和原有请求预算；见[模型供应商合同](model-providers.md)。

返回值通过 Pydantic、引用、范围、所有权与 mutation 校验。源证据必须来自输入中提供的
真实事件或工具回执；同一 proposal 选择的多个来源都会保存，不只使用第一条。原事件正文
仍是聊天引用的校验依据，人物名片和 ID 展示不会改写原凭据。截断或输出达到预算时报
`output_budget_exhausted`，不自动加预算。类别名称、importance、confidence 与解释文案
不再构成后台价值准入门槛，不要求 subject_basis、retention 或 source_style 分类。

稳定且不含具体人物隐私的 SELF fact、preference、reflection 和 principle 可使用 global；
私聊产生的 SELF fact 保持 current scope，Episode 永不进入 global。正文可记录真实姓名、
名片和 ID；名字和分类本身不赋予系统权限。输入复用现有可见记忆与 existing_episodes，
不再另取 previous_episode 专属视图。

## 调度与恢复

调度小时沿配置，默认 04/12/20；不要求恰好三个小时。默认每轮 32 批，每批配置
200 事件/16000 字符的输入预算。撤去每 owner 次数封口、低/高水位排空和自然间隔重复
调度。批次按实际渲染的输入计数，不能生成前丢弃尾部事件后再把整批标为完成。

每日 96 次限额在 Provider 准备 payload 并通过调度检查后、真正发送 HTTP 前原子登记；
包含格式修复和传输重试，成功或失败都收尾实际请求记录。schema 准备或调度前失败不
扣调用次数。进程重启保留原请求账本，不能重置原 run 的额度。旧版本未登记的 HTTP
重试无法追溯，不以 model invocation 次数冒充物理请求次数。

持久 cycle 防止重复时间槽，同一 cycle 内失败批次不再次领取；失败沿原 run ID、内部
事件范围、已保存输出和 mutation 回执恢复。不再以第三次失败永久隔离，也不添加水位
排空周期。真实源范围改变仍拒绝重放。连续检查点不会跨过失败空洞，后续成功范围独立
保存。完成提交后释放输入输出快照，失败快照继续保留。逐项 mutation 回执防止重复
写入；只有原完整执行检查点允许恢复 completed。旧版无检查点的已提交批次沿原回执
恢复，不能声称恢复了从未保存的模型输出。

mutation 的正常拒绝、重复与 no_change 是已处理终态；实际数据库、事务、检查点或
未预期代码错误保留失败记录。恢复不重复调用已完成模型，也不重复执行已提交操作。

无自身回复或可信工具证据的到期范围不调用模型，不写记忆；记录 `no_self_evidence`
并推进自省投影。原始事件账本不变。

## SELF 自主执行证据

schema 0067 允许工具回执由内部 `trigger_event_id` 或可信 `initiative_run_id` 单独归属，
两者严格互斥。自主执行从 run 的 canonical Conversation、Space、Presence 解析来源，
`target_person_id` 不授予任何私人 Memory 权限。执行回执按 run、execution、Provider、
tool 和真实 tool call ID 去重；没有聊天发言也保留实际成功或失败结果，不制造聊天事件。

每个已结束 initiative 有独立 receipt 水位；窗口最多 8 条回执，保持原有输入字符边界。
迟到回执进入该 run 的后续窗口，不受其他 run 进度影响。窗口复用原 SelfReflectionRun、
cycle、每日请求预算、检查点和原 run 恢复机制；管理输出以 receipt 范围显示，不将它们
统计成聊天事件。已领取未完成窗口保护其源回执，完成后仍被 Memory evidence 引用的
回执不会过期删除。未讲话的失败也可以参与判断，但仅能支持真实失败或尝试的经历。

回执清理每次先只读发现最多 500 个过期且未引用的候选，空页不取得写锁。
短 DELETE 再核验过期时间、全部 Memory evidence 引用及未完成 initiative reflection 窗口，
选择后新增引用、窗口或延长保留期都会阻止该条删除。回执引用查询使用 0083 的
tool_receipt_id 非空索引；原 receipt、run 和证据仍是唯一事实来源。

主 SELF 入口使用 actorless TurnMemorySession，自动读取当前群与该群可见的 SELF 资料，
不借最近发言者或目标人的身份准备上下文。纯工具自省经同一 MutationService 验证、
冲突处理和原子回执写入；只能影响有相应证据的 SELF global/当前群范围，不能写私人事实。
历史 run 证据可保留，不能因此恢复已失效 generation 的执行权限。

主生成仍复用同一 MainAgentTurnService、Runner 和固定完整工具声明。资料与 SELF run
位于动态输入中，恢复读取原 journal；不把恢复时新检索到的记忆登记成旧投影已经曝光。
子 Agent 保留原 SELF principal 与 execution ID，回执由原 run 归属，不冒充新的真人事件。
聊天事件水位、自主回执水位和参与控制器反馈序列各有用途，不能相互替代。

可选 `<yuki-state>` 是主 SELF 的内部状态自报，在可见发送及语音正文前剥离；不会生成
聊天记录或充当其他人意图的证据。自省仍依实际事件、工具尝试和回执做判断，不能把
自报、proposal 或模型说“做过了”当作操作成功证明。

## 管理与健康

超级管理员 `/ai memory self-reflection run` 以当前内部事件登记后台 manual cycle，
立即返回 ID、积压和边界；重复事件复用原 cycle。`status [run_id] [页码]` 只读查询。
开始和结束报告只有数量、时间、失败类别、事件范围与预算，不包含源正文、reasoning。
最终报告复用 Social 的 prepared/succeeded/uncertain 回执。同一 cycle 的固定调用键不重发；
传输结果未知时标记 unknown，不能声称端到端 exactly-once，也不能猜测失败后重新发送。

健康区分 actionable、waiting_retry、isolated、policy_ineligible、recent_not_due、processing。
后者计数可以在同一 owner 不同范围出现；不能把各组会话数相加当作去重会话数。
已跳过且没有新消息的 policy-ineligible owner 显示 0 pending events。
失败详情按 5 项分页，其他原始内容不进入报告。`retry <batch_id>` 可由超级管理员
重新接纳隔离批次，保留原 ID、尝试次数和总额度。健康快照按最近 24 小时内周期提供
实际流入/排出速率，观察不足 60 秒时为未知；历史失败不否决当前健康。
配置见 `.env.example`。

## 部署

本轮为源码与合同裁剪，不代表已部署或通过真实 QQ 验收。后续部署仍沿当前迁移链
与 Bot 发布流程，保留预算、mutation 和发送回执；回滚不得用旧数据库覆盖新记忆。
本地 SQLite 与来源回执测试验证恢复正确性，不能替代自然社交效果或人工语义准确率。
