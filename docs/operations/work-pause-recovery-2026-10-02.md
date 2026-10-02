# 暂停通知与工作恢复核查

本记录针对数字生命研究所 2026-10-02 18:17–18:23（Asia/Taipei）的任务故障。
实施基线为 `af022cc`，当时生产 Bot 为 `ops-0ce3514`。当前内容是已查证的事故事实与修复范围；
测试、合并、部署和真实任务验收需要分别记录。

## 事故事实

- canonical Conversation 为 `5b234414-7537-4f1f-8f27-d03c2c0949c7`，generation 为 18。
- 原 Work `43ec0b6b-220c-44a4-8755-77755867d2e9` 因 `work_journal_source_changed` 暂停。
  持久恢复记录包含 `effect_receipt_recorded=true`，不可按没有发生效果重试。
- 原 terminal run `2e3bc225-ec73-4a42-8274-99b3d265964f` 已成功结束，exit code 为 0。
  此前两次显式消息发送也已有确认回执。
- 同一 Work 的暂停通知有 14 条 `accepted`，另有一条 `planned`；首末 accepted 通知均有
  transport 接受与平台回执。不能把这些交付当作 14 次独立任务失败。
- 同时观察到五次后台 `conversation_compaction` 空响应，后续也有成功记录。
  五次失败的新增来源上限为 79519，已有 emergency overlay 覆盖至 79954，且摘要更新时间早于
  这些失败。它们不要求用较旧覆盖替换已有 overlay，不能当作五次前台任务失败。
- 同会话 invocation 51494 的成功摘要（trace 56022）与下一独立压缩请求 56028 的
  Previous summary 精确相等，后续结果继续精确衔接，最后结果与当前 semantic 一致，
  证明后台 semantic checkpoint 已发布。
  第一笔提交发生在 10:19:07.910624–10:19:15.176746 UTC 之间；没有独立提交轨迹，
  不能仅按时间证明失败时所有来源版本变化都来自它，也不能排除同时的旧事件编辑。
  原 Work 冻结摘要包含仍有效的 overlay 全文，而 semantic 追赶会重绑 overlay 的
  `base_semantic_revision`，即使其全文、覆盖和实际 Prompt 选择不变。

## 修复设计

暂停通知由原 supervisor 登记，由原交付意图与回执恢复。通知维护使用原 Work 租约，
保留暂停状态、失败记录和待处理输入；不进入业务执行的结算、排队或模型循环，也不计入任务活动时间。
同一暂停期间再次遇到收尾故障复用原通知身份；真正恢复执行后再次暂停才形成新的通知。
已经 accepted 的通知不重发，dispatching/unknown 按原回执核对而不自动重放。

普通轮接纳 Work 后，响应或工具配对 journal 保存仍校验冻结的来源 revision。
若标量版本变化，仅在正常 journal 保存冲突路径、writer 之外复用原 selected-source guard 核验；
确认已用事件、摘要、身份及原执行权限仍有效后，有限重备同一数据库保存。
不再次请求模型、执行工具、发送消息或重置预算。没有原 guard 或真正来源变化仍拒绝；
压缩的冻结来源 CAS 保持独立严格校验。

来源 guard 复用 Prompt 的有效摘要选择：有效 overlay 优先，否则有效 semantic checkpoint。
核验原事件全文、身份与实际使用的摘要，而不是把未被选择的后台 semantic 及 overlay 的
基准 revision 维护当作模型来源变化。后台 semantic 仍落后于有效 overlay、且 rebase 后
有效摘要完整不变时，可以通过核验并保存同一链。有效摘要正文、覆盖、身份、来源指纹、
所选版本或选择发生变化时
仍拒绝，不自动改写历史前缀，也不把这条规则扩大为所有来源冲突的自动续跑。

本修复不改系统提示词、固定工具声明、模型请求前缀、Provider 路由或缓存身份。
原暂停 Work 不因代码升级自动恢复业务，命令与发送回执保留原 ID。
本次消除同 activation 中的无语义版本变化误停；崩溃后若原 journal 的来源版本已经变化，
仍使用既有显式链边界，不从新上下文猜测旧摘要选择，也不宣称所有重启都能保持原模型前缀。

## 验证与上线边界

最终本地组合验证 **167 passed**（146.81 秒），覆盖真实通知维护路径的 pending 输入、成功交付、未知交付和失败收尾，
证明不会进入模型或业务效果，不改变原暂停与失败记录。来源修复同时验证未用事件更新可以通过，
已用来源变化、隐私、generation 与租约失效仍拒绝；保留工具调用配对和请求前缀。
组合包含 14 项通知维护、11 项 journal 重备、8 项真实有效摘要发布/两协议序列化回归，
以及原有协议连续性、容量、Rollup 等待和汇报/steer 前缀检查，不混加各阶段重复运行计数。
现有 SQLAlchemy 循环外键排序警告保留，没有测试失败。

Ruff 检查和格式检查通过，Linux 目标的 mypy 检查 683 个源文件通过；release baseline 校验通过。
本地 amd64 预备镜像构建通过。此时 CI、合并和生产替换尚未完成，不能据本地结果宣称线上任务已恢复。

部署只替换 Bot 镜像。停写期间备份数据库、Work 协议对象、工具正文、工作区及配置，
校验引用哈希，并按原 Work ID 比较预算、journal、输入与已接受效果。回退保留上线后新事实，
不能恢复旧数据库来撤销回执。

错误收口依据现行 [共同架构约束 §7.4–7.5](../architecture/development-contract.md)、
[主 Agent 执行与恢复](../architecture/main-agent-runtime.md)、
[Provider 输出边界](../architecture/provider-output-boundary.md) 与
[完整证据备份合同](work-evidence-backup.md)。历史任务书中的已替换策略不作为当前恢复授权。
