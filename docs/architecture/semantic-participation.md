# 语义参与 V6：开发接入状态

2026-09-22；开发基线 `4f882e9`。独立仓库：
[Yuki-Semantic-Participation](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation)。

**当前为阶段一，未连接真实自主入口、未部署。** 新库只负责语义观测、参与状态与提议；
Yuki 继续拥有来源、权限、主 Agent、steer、工具、工作恢复和发送。

## 已实现的宿主边界

`AutonomyBinding` 按 Conversation/generation 保存 master/external 开关、唯一有效 owner、
controller_epoch 与 revision。0066 增加 selector、initiative run、已考虑来源和反馈记录。
未配置的记录默认 off；迁移不改变现有群设置，不启动外部服务，也不启用新模式。

接纳仓库以短事务核对持久 selector 与 proposal 身份，同 proposal 返回同 run；来源键按
event/memory 类型、内部 ID 和 revision 持久去重，legacy/semantic 共享已考虑来源。
context 中被动出现的事件不自动进入已考虑焦点集合。

新提议要匹配 owner/epoch。已接受执行按原 run 处理，不能用 selector epoch 的变化取消
Work；generation/reset/权限变化仍需在完整 SELF 接入时走宿主既有围栏。
实际迟到反馈保留，终态不因新回执重新变成可执行任务。

**这只是可信 Host 调用的仓库协议。** 尚未核验完整语义支持、所有来源权限、SELF 工具授权；
不能把仓库返回的 accepted 当作现成的对外发送许可。

## 为什么不能直接复用当前自主 respond

- `AutonomousGroupService._admit_latest()` 仍将最新真人 `last/profile` 交给 `ChatService.respond()`。
- 无 inbound 的 `ToolRuntime.require_actor()` 目前只支持定时任务；权限与个人上下文仍以真人为中心。
- `sandbox/source_recovery.py` 要求真人 inbound/person 来源；工作和子任务恢复也会还原这个来源。
- Social 当前群发送特许只认真人触发或插件事件；SELF 需要自己的可信 run 授权和执行前复核。
- MemoryToolReceipt 的 event 外键、自省的聊天事件水位不能记录无发言但执行了工具的自主回合。
- `ConversationTurnSnapshot` 强制非空真实事件；`work_session` 也靠事件 ID 判断动态输入是否已追加。
  SELF 需要独立、持久的 run 来源标记，否则每次唤醒会重新拼接 brief，破坏续跑前缀。
- `WorkScheduler` 的接纳类型、沙箱及子任务恢复尚未识别 SELF；仅新增 trigger 不能贯通恢复链。

因此当前没有把 proposal 包装成最新人的消息。`AcceptedInitiative` 不包含真人 actor 或假 QQ
消息；目标成员与发起主体是不同概念。recall/contact 可以有合法 memory 来源，不补造聊天锚点。

## 后续必须一起完成的垂直接入

1. 正式 self-origin、principal 和 authority；不继承最后发言者或目标成员的私人上下文、管理员权限。
2. 唯一 selector 分派 canonical 观察，并同时围住旧自主服务的新接纳；不建立另一个 Agent 循环。
3. SELF 群场景组装进入现有 MainAgentTurnService / Runner / Backend；固定前缀、工具合同不按模式动态变动。
4. 原 initiative run 下的 Work/子任务、恢复、预算和发送回执；切换 controller 不重做或重发。
5. SELF 工具证据进入自省；可选状态尾段在可见发送和记忆抽取前剥离。
6. 影子对照、真实 Jev 与小范围 QQ 验收；上线前验证两模式互斥、明确停止、未知发送和重启。

正式接通前还必须实现原子接纳后的持久派发：0066 的 run 要按唯一身份关联既有 Work 或
待派发记录，进程在 accepted 后退出也能接回，不能永久停在 busy。派发只进入同一主入口。
SELF Memory 需要 event/run 明确区分的证据来源与独立增量水位，不能只把外键改成 nullable。

这一阶段不修复或重写 steer，也没有发现足以重新认定其存在取消缺陷的证据。
