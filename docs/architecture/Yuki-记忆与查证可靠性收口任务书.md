# Yuki 记忆与查证可靠性收口任务书

基线：`codex/memory-relevance-and-reflection` / `3e67492`；版本 3.8.1，schema 0051。
统一跟踪原记忆可靠性、意图召回任务的剩余工作。状态：实施中，未通过全部真实验收，不构成上线说明。

## 目标与不变量

需要查证时正确查询，结果相关，最终回答不超出证据；不以调用次数增加作为成功。
独立执行，不调用 Grok。不修改人格，不新增识别/审核 Agent、短上下文、语义关键词正则。
不清空记忆、不 ai new、不重建生产 embedding、不扩权、不新增表/迁移/执行器。
保留固定工具 schema、模型请求/工具循环上限及已有提交。
已完成的 canonical 自省配置、结构化修复、Dream 预算调度、全局排序、四条召回、
session 回执、严格日期、有界完整事实返回继续回归，不重新施工。

## 顺序提交合同

### C1 `fix(agent): observe evidence tool availability and outcomes`

- 只读核对线上镜像/代码/配置及最近两个完整自然日。普通聊天、插件、自动化、管理分开；缺失字段标未知。
- 分开记录工具实际暴露、模型选择、执行结果、进入模型请求、最终使用。
- Memory 继续由 TurnMemorySession 管理；Web 复用来源记录/native events，Tavily/native/fallback 分开。
- 缓存复用不是新查询；取消前执行但没进入模型不能算暴露。观测失败不破坏正常查询。
- 日志只含关联 ID、枚举、数量、schema 指纹和脱敏错误，不含问题/账号/正文/原始参数。
- 人工标注需要补查机会；不用自动召回次数作分母，不静默改用户工具配置。

### C2 `fix(memory): preserve query targets and bounded result semantics`

- mention/reply 用事件 subject_ref，姓名用名称；保留合法兼容账号入口。人物查询无需群号证明权限。
- 只有明确群限定才填写 selector；冲突选择器报错，不改成发言者、不自动扩权。完整 Main Agent 理解指代。
- 区分同批并行和拒绝后重试。空结果仅本次未找到，N 条不是全部；保留截断数量/原因。
- 拒绝/故障不是没有记忆；严格日期不放宽；允许实质不同补查但不增循环上限。同步 grounding。

### C3 `fix(memory): distinguish relevant evidence from weak candidates`

- 自动主题优先最多四条，最多一条相关背景补位；SELF/Episode/偏好不绕过准入。
- 校准绑定 embedding profile，未校准/故障保持精确匹配降级。阈值 0.35–0.90 步长 0.025，主题不低于背景。
- 主动 overview/list/detail 不套自动门槛；主动搜索保留宽候选，不凭单例改全局阈值。
- 候选投影词法/语义及校准准入通过/未通过/未知；分数不是概率，候选不是认证事实。
- 冻结验收组不调参、不改标签，实际语义仍人工核对。

### C4 `feat(agent): establish a stable evidence-seeking contract`

- 固定合同加证据状态，不新增结构化最终答复协议。在 PromptComposer 固定规则中声明：
  历史/人物材料不足查记忆，时效事实/明确查证用联网，指定网页用受控读取，闲聊创作不强制。
- 不足则澄清/限定回答；自动预取空不是不存在，History/Rollup 的模型旧推测不是独立事实证据。
- 本轮投影来源类型、未查/成功/空/拒绝/失败、数量/截断、真实暴露引用、相关性准入状态。
- 引用由后端登记，用现有 fact/source ID/citation，不重复注入正文；随结果/尾部传递，未查询不新增消息。
- 保留 tool_choice=auto，不加预检/审核模型或重试；不按消息改前缀/schema，不用正则声称验证事实。
- 搜索不得外发完整聊天/私人记忆/system；外部资料不可信，回执只能证明执行和来源而非所有句子正确。

### C5 `test(memory): close reflection dream and attribution acceptance gaps`

- 自省使用真实 RuntimeConfig 覆盖群/私聊、outbound/旧 Presence，核对 committed facts，不仅看 completed。
- 复测叙事固化、Episode 混杂；使用原结构化任务/价值门槛修正，无价值 noop 推水位，不写生产事实。
- Dream 验证来源/意义/长度/修复、输入完整性、失败公平调度、预算延期、幂等/checkpoint。
- 两次失败不伪装 keep，输入超限不截事实再替换；保护规则由确定性测试保证。
- Attribution 人工对照暴露/回复，区分未使用/未评估/失败/跳过；修复关联/解析，不改变使用定义。

## 数据、预算与验收

仓库外受限冻结 corpus，原失败不覆盖。目标 80，至少 60 合格真实样本和 30 独立验收样本。
按话题窗口拆分，算法运行前冻结直接相关/背景/无关、必要事实及查证机会标签。
无法还原历史状态只能冻结 corpus 比较；生产只读，写入与 QQ 外发隔离。
必要主题召回 >=80%、主题准确率 >=90%、无关零注入 >=90%，背景不挤出主题，越权/来源越界零。
原八类 Main Agent 场景全部人工核对，加明确联网、时效主动联网、闲聊不查、搜索失败/空、
弱候选不编经历、截断不冒充全部。记录参数→有效查询→结果→暴露→回复，不只看 success。
统计正确查证机会率和多余调用率，不以调用量验收。

累计请求上限 **96**；历史 **47** 不重置，新增最多 49；旧 Main Agent 24 子上限取消。
软分配 Main 24、Dream 10、自省 4、attribution 2、embedding 4、联网 3、储备 2，可调配。
每次生成/embedding/联网/修复/传输重试在发送边界计数；禁用无法观测重试。
先离线标注/缓存回放/模拟再真实调用；额度耗尽未通过则停止调用和部署。

## 质量、文档、部署

每个提交包含对应当前文档，目标 pytest、Ruff、mypy、diff check 通过。
最终完整 pytest <=800、Memory quality/基线、插件合同、fresh schema/FK/FTS、release validation、Compose、本地 Docker smoke。
不删除安全保障或重设基线过门；跨人物/日期/问题固定前缀和工具结构不漂移。
最后 `docs(memory): publish evidence-seeking contracts and acceptance results` 同步检索、运维、工具说明；
旧任务书保留历史结论，标记替代与停止点，不留矛盾现行政策。
全部强制验收通过才核对线上状态，从已提交代码本地构建 linux/amd64，记录 commit/image/checksum。
备份验证 DB/WAL/SHM、配置及旧镜像，仅替换 Bot，不远端构建、不重启 SnowLuma、不清理未知容器/备份。
配置只定向调整并审计新旧值。观察 >=15 分钟，未自然触发标未观察。
回归恢复镜像/配置，无损坏证据不以旧 DB 覆盖新消息。不推送/PR/合并/发布。
报告分列修复、查询/参数/相关性/证据使用、自省/Dream/attribution、样本/预算、风险、镜像和回滚位置。

## 实施记录

初始工作树干净。线上 schema 0051，镜像标签 memory-recovery-92483ab1，镜像 ID：
`sha256:29b3bfbc7ca3460b337d8f373e573b5eeaa23ffce7fcb606ea4771b957c938a3`。
环境 WEB_MODE=native_with_tavily_fallback，不等于所有会话最终有效权限。
当前容器仅启动约三小时，9 月 7–8 日容器日志无记录，不能解释成零调用。尚未完成 C1 验收。

后续实现和真实复测详见 [验收进度](memory-evidence-acceptance-progress.md)。
C1–C4 代码提交保留，但不等于真实效果通过；自省语义与相关性校准等硬门槛仍未通过，禁止部署。
