# Yuki 记忆可靠性与强相关召回修复任务书

> 后续未完成工作由[记忆与查证可靠性收口任务书](Yuki-记忆与查证可靠性收口任务书.md)统一跟踪。
> 保留历史结论；新授权累计预算 96，旧请求不重置。

状态：实施中；未完成验收，不构成已上线行为说明。

## 一、基线、交付与边界

- 起点 `92483ab`；实施前核对 HEAD、线上镜像和数据库。分支
  `codex/memory-relevance-and-reflection`，版本保持 3.8.1，schema 保持 0051。
- 修复自省把 Presence 当 Person 读取配置；补齐自省/Dream 结构化合同与有界修复；
  自动召回改为当前主题优先、最多四条，背景最多一条补位；统一主动查询意图及全局排序。
- 完成隔离真实数据对照、本地提交、本地 linux/amd64 镜像构建和验收后的服务器部署。
- 不改人格提示词，不清空记忆，不执行 `/ai new`，不重建生产 embedding；不增加识别
  Agent，不为 Main Agent 构造独立短上下文。不自动推送、提 PR、合并或发布 Release。
- 生产数据只通过正常运行流程变化。验证写入只在隔离副本，禁止发送真实 QQ 消息。

## 二、实现合同

### A. 自省配置主体

已确认故障链：SELF mutation → claim processor → runtime config snapshot(sender_user_id)
→ 将 Yuki Presence 当 Person 解析。

- 引入后端内部 `MemoryConfigScope`，携带 nullable canonical Person/Space ID，模型不可填写。
- 普通用户即时操作保留真实用户和当前群的配置继承。
- 群自省只用任务所属 canonical Space；私聊自省用 Conversation 的 Person，不能取第一条
  证据作者。Claim processor 接受解析后的配置范围。
- Event、作者、Evidence、Presence provenance 保持真实；禁止伪换人类锚点、假 Person、
  吞掉身份错误后回退默认配置。
- 配置读取不等于任务准入。已有停用所有者仍可供历史任务读取配置；不存在或 canonical
  类型错误失败关闭，实际执行仍遵守原准入。
- 审计所有 MemoryProcessingContext 入口：即时修改、提取、自省、历史维护。
- 保留异常隔离、取消传播、部分提交恢复、安全日志。集成测试必须接入真实 RuntimeConfig。

### B. 结构化合同及修复

#### 自省

- schema 显式 `proposals.maxItems=8`、`episodes.maxItems=1`；episode evidence 为 1–8 个
  唯一且真实允许的引用。后端同等校验，不截断非法结果伪装成功。
- 去掉重复矛盾提示。修复针对 episode 数量、证据数量、缺字段、非法引用等实际错误。
- 最多一次模型修复；无价值内容允许 noop。

#### Dream

- 扩展现有 structured task，不另建执行器。保留首次失败输出、错误类别；第二次携带
  原任务、原输入、失败输出及定向要求。失败输出作为不可信数据，不拼进 system 指令；
  超长时明确标记截断。
- schema/来源错误修正格式与映射；长度错误要求精简，不能一律“重新划分压缩”。
- 每 cluster 最多两次生成，发送前预占预算；每轮最多 12 cluster、24 生成请求。
- repair 不绕过来源覆盖、跨 scope、显式事实保护、重复/未知引用和原子 mutation。
- 两次无效则保留原记忆并记录失败，不能转 keep 假装成功。
- 最多四个输出，单条最多 800 字、新正文合计最多 1600 字；默认 token 预算 4096。
- 0.45 压缩比只作软目标，移除 0.70 硬拒绝及旧配置/现行说明。soft miss 不触发额外
  调用，不以最短作为选择有效结果的唯一标准。生成和 mutation 共用同一绝对长度政策。
- 不静默截断源事实。超输入预算先删可选 evidence excerpt；完整源正文仍放不下则记录
  输入超限，保留原事实。
- 复用已有 fingerprint、状态、时间调度：未尝试/来源变化优先，失败按最久未尝试优先；
  同指纹一次运行只规划一次。失败/延期不推进成功 checkpoint。
- 分开记录失败、输入超限、未执行的预算延期；不回写历史失败结果。不增队列或迁移。

### C. 强相关召回

#### 权限与重点分离

- `ResolvedReadScope` 只提供读取权限；`MemoryQueryIntent` 提供模式、目的、实体、时间、
  种类和检索重点，不能充当权限证明。
- 不用全部可读目标覆盖 subjects。主动工具仅为明确目标补缺省重点，不覆盖已有重点；
  自动预取没有明确重点时留空，不将发言者自动等同主题。
- 历史共同群政策保持；Evidence、private SELF、写入和插件身份不扩权。

#### 全局候选与排序

- 各目标先给有界候选，再按 fact ID 去重、全局评分、总量限制，不能先各裁两条。
- 保留 lexical/semantic；相同 query 与 embedding profile 的相似度跨目标可比。
  lexical 和 semantic 排名均在合并池中计算后融合，不给每个目标第一名相同基础分。
- 原始相似度用于准入；RRF/排名分不是概率。实体、时间、种类参与全局排序，重要性/
  活跃度只在同一相关性档位辅助，不能让弱背景超过强主题。
- hits 保持全局次序，分组展示与主动工具总量截断不能重排。

#### 四条与背景

- background/continuation 默认上限均四条，单目标可占四条。满足强相关的任意四类 scope
  都算主题，不按 Person/SELF 等类别降级。
- 主题不足四条且已有主题时，最多补一条与本轮有关的当前人物背景，须过独立背景门槛。
  无主题不靠弱背景凑数，允许零注入。SELF episode/显式偏好不能绕过相关性。
- 饮食偏好在选饮料时可为主题，在询问 Diana 昨日经历时不能抢位。
- 不静默覆盖明确数量配置；部署列出有效值并按本任务作审计后的定向调整。

#### Main Agent 补查

- 预取只用当前消息、真实引用和后端已解析目标，不拼历史，不加前置模型调用。
- Main Agent 保留完整 History/Rollup/工具/上下文，自行理解指代并使用现有意图工具。
- 帮助说明预取为空不代表无长期记忆，明确询问历史而材料不足时可主动查询。
- schema 固定，循环上限和同轮去重保留。自动门槛不用于主动 overview/list/detail，
  主动相关搜索保留更宽候选。
- 空结果、权限拒绝、歧义、基础设施错误分开；不自动重试不可重试错误。

### D. 统计与文档

- 不改实际使用定义，不强迫引用，不把未评估算未使用。
- 现有观测记录候选目标、主题/背景、准入、最终名次、零注入、主动补查，不新建业务表。
- 用真实回放人工核查 attribution 漏判，三组合成场景不是充分证明。
- 同步架构、检索、工具帮助、质量运维、修复记录。旧 P1“不新增阈值”阶段约束被本合同
  替代。CHANGELOG 和已发布说明保留历史原意。

## 三、验证与准入

### 真实样本

- 目标 80 问：跨人物、群整体、共同经历、偏好、无关闲聊、真实引用、指代补查、重复注入。
- 原文只在仓库外受限目录，公开仅匿名编号、统计、错误类别。
- 按连续话题窗口分组，校准/验收各半，相邻改写/同事件不能跨组。
- 新算法运行前冻结“直接相关/有用背景/无关”标签及必要事实集合，旧 attribution 仅参考。
- 严格历史回放须证明当时正文、状态、身份、权限，created_at 不足。不能还原的只做同一
  冻结 corpus 新旧对照，独立报告，不混入严格历史指标。
- 至少 60 合格真实样本、30 独立验收样本，不足则证据不足，不能用合成数据冒充通过。

### 校准

- 新旧算法共用冻结候选、query 向量、profile。query embedding 保留 query instruct，
  不用 document 编码替代；批量生成后缓存复用。
- 只在校准组搜索 0.35–0.90、步长 0.025 的语义门槛，主题不得低于背景。
- 先满足主题准确率 ≥90%、无关零注入率 ≥90%，再最大化必要事实召回；平分优先无关更少、
  再优先更高阈值。阈值绑定 profile 指纹并进入现有配置目录。
- 验收组不再调参。必要事实召回 ≥80%、主题准确率 ≥90%，背景不挤主题，权限/来源越界为零。
  未通过则停止部署，不改标签、不降门槛。
- embedding 故障/未校准 profile 时自动只接受确定精确匹配，否则空结果并记录降级；
  主动仍可 lexical。

### 行为与质量

- 自省首证据为 Yuki outbound、旧 Presence、人类 inbound、tool receipt，群/私聊配置正确。
  错误 canonical 类型仍拒绝，取消、部分提交、noop、水位、幂等保留。
- schema/后端数量一致，repair 带失败输出且防指令注入。
- Dream 来源保护、绝对长度、软目标、修复预算、输入完整性、失败公平性。
- 发言者弱匹配不压强主题，各目标第一名不可视为等相关；四主题、三主题一背景、零结果、
  SELF 无旁路、主动跨目标均覆盖。
- Main Agent 完整前文完成指代补查，无新增识别调用。
- 最多 800 collected tests，替换重复/过时行为测试，不删除有效安全保障。

### 外部请求预算

网络发送边界累计最多 48 次，生成、embedding、repair、传输重试均计入；无法观察的自动
重试必须关闭。预留：embedding 8，自省 10，Dream 12，Main Agent 10，attribution 4，
复测 4；可调配未使用额度但不能超总量。副本写入与外发消息隔离。

## 四、提交与上线

1. `fix(memory): resolve reflection config by canonical conversation`
2. `fix(memory): align structured contracts and bounded repairs`
3. `fix(dream): preserve meaning within explicit output budgets`
4. `refactor(memory): rank relevant facts across canonical targets`
5. `test(memory): validate relevance against private real replays`

每次目标 pytest、Ruff、mypy、diff 检查通过。最终完整 pytest、Memory quality、插件合同、
fresh schema/FK/FTS、release validation、Compose 校验、本地 Docker smoke 全部通过。

仅强制验收全通过后：

1. 已提交源码本地构建 linux/amd64 镜像，记录 commit、镜像 ID、文件校验和。
2. 上传既有服务器，绝不远端构建。备份 DB/WAL/SHM、配置和旧镜像并验证完整性。
3. 只替换 Bot，不重启 SnowLuma，不删未知容器或用户备份。
4. 检查健康、QQ 重连、自省批次、无超时 processing、数据库完整性及历史事实/证据/会话。
5. 连续观察至少 15 分钟，不发真实测试消息，不清上下文。
6. 启动失败或核心回归恢复旧镜像和配置；没有损坏证据不能恢复旧 DB 覆盖新消息。

完成报告分别列出修复、真实样本结果、模型调用数、未解风险、线上镜像、回滚位置。
调度器活着 ≠ 批次成功；批次完成 ≠ 记忆写入；更多召回 ≠ 更好召回。

## 五、实施记录

### C1：配置主体入口审计

- 即时 mutation：默认保留真实用户/群配置；内部 context 可由自省服务提供配置主体。
- 自动提取 worker：现有 eligibility 只允许 canonical human inbound，不接受 Yuki、
  external_bot 或 system 证据作为自动提取主体，维持用户/群继承。
- rebuild：提交前复用相同 eligibility，保留原权限检查，不将历史维护变成扩权入口。
- self-reflection proposal/episode：由 batch 的 canonical 所有者提供 MemoryConfigScope，
  第一条 event/tool evidence 只负责 provenance，不决定配置主体。
- MemoryMutationContext → MemoryProcessingContext 明确转交该内部范围。
- 测试夹具已接入真实 RuntimeConfig；覆盖换 Presence 的 outbound 首证据、episode 的工具
  evidence、停用 Space 读取覆盖值、错误类型拒绝及原即时 mutation 行为。
- 此处只记录本地实现，不表示真实模型、完整回放或部署验收已经通过。
