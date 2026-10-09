# Yuki Memory 删除优先重审：Provider、记忆策略与后台运行

## 0. 本任务的最高实施约束

**把写死的地方删掉。非必要不增添新代码，能删的就删。任何能通过删除或放宽现有条件解决的问题，不新增替代代码或机制。**

这一约束高于本报告的候选方案和旧任务书。实施者可以推翻下面的分组和做法；不能为了完成索引而添加本来不需要的检查。旧文档、已有测试和“以前为了安全”都不能单独证明某条限制必须保留。

注重可扩展性和可维护性，允许真实的部分成果、未处理内容和未决状态。先删除重复判断、强制闭合、经验上限和无人需要的分支；剩下的真实问题才做最小必要改动。不要给删除的条件换一个可配置开关，也不要另建预检、熔断、重试协调器或通用状态机。

保留规则须指向具体事实：会不会写错对象、越权读取、伪造来源、覆盖真实历史或重复已经提交的效果。`fact ID` 唯一和 `memory_key` 只能有一条 active 不是同一回事，不能把后者直接包装成前者。

**测试也按同一删除原则处理。** 全仓大幅裁剪测试是本次任务，不能只清Memory测试。固化旧设计、重复验证、只复制实现的测试及其专属夹具、快照、脚本应一起退役；保留少量能区分真实错误的验证，不为每次删码再补同规模测试。

**CI一并大幅裁剪。** 删除重复执行、冻结设计、按测试名字或固定样本数量设门槛的流程及专属配套。只留下能验证当前代码、构建与实际发布结果的必要步骤，不用新增CI管理框架替代旧复杂度。

## 1. 旧方案撤销与审计基线

上一版 M01–M10 不再是实施方案。尤其撤销“增加能力预检”“增加槽位一致性预演”“增加错误分类体系”“扩展档案合并器”“必须补历史链”“新增告警去重逻辑”等默认方向。它们只有诊断线索价值，不是待完成清单。

以下审计基线保留为历史证据。2026-10-09 用户已授权实施、PR 合并、本地部署与生产 Bot-only 上线；执行状态见索引和末尾交付记录，不操作 SnowLuma。

代码基线为 `2ce7a167fc6eb98e56996b6d8b4d7558ebfaa284`。生产计数引用此前 **2026-10-09 14:26（UTC+08）** 的只读快照：自省75/96条额度均无真实HTTP，isolated 10,340 events；Dream run68/cluster472异常未收尾；历史缺链162条。本轮没有重新查询生产，这些数字不是当前实时状态。

## 2. 重新判断根因

- **自省失败首先是覆盖配置。** 入口强制 JSON_SCHEMA，绕过 Profile 已有的模式；当前 Gemini response_schema 自制转换器又拒绝 anyOf。先删强制模式即可回到已有的 function_tool 路径，不需要先扩展转换器或加预检。
- **预算错误是责任顺序。** 登记发生在 payload 构造前，失败收尾又包住整个准备过程。移动既有登记与对应收尾即可，不需要退款账本或新的失败状态。
- **Dream 唯一冲突暴露的是记忆字典化策略。** 模型 resolve({1515,1516},anchor1515)，同时 keep(1514)；1514 已占同 key 的 active 槽。上一版先把单 active 当不可删除约束，再要求增加预检，这个前提撤回。
- **后台不能继续有多层经验封口。** 三次失败永久隔离、每簇终身两次、固定调度与额外排空、历史隔离持续报警是策略叠加，不是事实正确性的必然要求。
- **历史缺链不等于当前服务停摆。** 162条的历史迁移机制和副本恢复已查过，但恢复数据是另一个目标；本次不将其强制塞进简化工程。
- **issue #270 的人物信息丢失还会造成错配。** 正文和记忆主体分别编排“成员序号”，遇到发送者提及自己时顺序不一致；需要删除这套无名序号设计，不能只要求模型别写占位词。

## 3. 可直接删除或放宽的候选

| 对象 | 源码证据 | 减法方向 |
| --- | --- | --- |
| 自省强制模式及“零工具”合同 | `memory/self_reflection/service.py:283`，`self-reflection.md:14–25` | 删 mode=JSON_SCHEMA，沿 Profile；emit_result 只是结果载体，删绝对零工具的限制 |
| TEXT_JSON 第二次许可 | `model_runtime/structured.py:152–153` | Profile 已显式选择，删 allow_text_json 检查、参数和各处透传 |
| 专属 Schema fallback | `structured.py:224–257`，自省专属开关 | 删递归降级和其开关，直接使用已有显式模式；不扩错误码清单 |
| 纯文本任务的重复能力门槛 | `model_runtime/profiles.py:93–103`，`executor.py:473–474` | TEXT_JSON 没有原生 Schema 请求，不应再用同一个 STRUCTURED_OUTPUT 标签多次封锁；核对实际 wire 消费者后清掉重复门槛，不新增能力层 |
| Gemini 辅助摘要模式特判 | `structured.py:35–46`，三个摘要调用方 | 删按 Gemini 强行切 JSON_SCHEMA 的分支；空包装一并删，调用已有 Profile 方法；已有文本 JSON 提示与解析继续使用 |
| 自省产量限制 | `self_reflection/models.py` 的8条proposal、1条episode、8段、8/16个引用 | 删固定数量上限及相应提示、repair规定；已有逐条遍历与 index 回执可处理多条，不另加一套限额 |
| Dream 必须全覆盖与遗漏项假检查 | `dream/service.py:796–802,527–532` | 删 used==expected，也删遗漏事实自动checkpoint；显式keep仍有operation，未处理部分留给未来，不必逼模型补keep |
| Dream 篇幅与动作数量 | `dream/quality.py:5–22`、models/service/mutation重复限制 | 删4条、800/1600字、单个recompose等经验硬限制，不把它们改造成新配置体系 |
| 自动价值数字门槛 | `memory/quality_policy.py:148` | 删 importance<3 的硬拒绝；必填解释是否只用于展示也应审查，不把解释字段当成事实证据 |
| 用token数推断失败 | `structured.py:258–263` | 删达到 max_output_tokens 就认定截断的推断，沿实际返回状态与已有解析判断 |
| 配置值的第二层固定上限 | `settings_domains.py` 自省/Dream参数 | 删无消费者依据的 le=365/200/16000等固定天花板；已有可配置预算与真实模型容量继续生效 |
| 固定失败次数与退避表 | `self_reflection/repository.py:1039–1042`，`dream/repository.py:708` | 删“三次即永久隔离”和“每簇累计两次即封死”；复用周期与累计预算，同时核查FULL现有预算豁免，不笼统声称所有模式都有总额度 |
| 固定三个调度时刻 | `settings_domains.py:337–341` | 删恰好三个和重复检查，实际消费者本来读取集合；默认4/12/20不是不可改要求 |
| 多套自省调度政策 | `self_reflection/worker.py:101–120` | 收敛到已有到期判定与领取入口，删重复协调；聊天到期、纯工具回执和原ID重试仍可进入执行，不新增调度器 |
| 历史隔离反复报警 | `self_reflection/worker.py:250` | 删 isolated非零就warning；原health/status仍显示历史数量，不新增冷却、去重或提醒状态 |
| 向导覆盖独立路由 | `deployment_setup/command.py:578–592` | 删非首次安装重建全档案的分支；沿现有读取分支和WebUI/Profile文件配置，删除失去消费者的搜索合并器 |
| 缺链使运行健康永久失败 | `memory/models.py:301–313` | 历史缺链保留audit详情，删除它对运行健康的硬否决；这不等于历史已恢复 |

上表是删除候选和核查结果，不是要求按行创建新代码。删掉条件后同步清掉只服务该条件的参数、提示词、配置、测试和陈旧合同；保留真实请求与领域行为的验证。

## 4. 三处需要成组删除，而非只删一层

### 4.1 Gemini Schema 分支

本次自省可先靠删除模式覆盖解决。自制 response_schema projector 不必再扩 nullable/anyOf；可以整支退役，但先脱开真实消费者。

历史运维材料记录旧代理曾使用 responseSchema，不能谎称这条分支从来无人使用。辅助摘要的 Gemini 特判删掉后，原有无工具文本分支即可接续；明确选 JSON_SCHEMA 的任务仍可使用已有 responseJsonSchema 路径或改绑现有合适连接，不增加自动跨 Provider 兜底。

整支退役包括 `llm/gemini_schema.py`、adapter分支、wire_options字段、专属fingerprint兼容、已保存配置项和专属测试/文档。实际端点结果需要验收；不以已有分支为理由永远保留两套 Schema 系统。

### 4.2 同 key 单 active 策略

建议方向是**去掉“记忆是每个key只有一个值的字典”这一策略**，保留按 fact ID、真实来源与语义关系组织的事实。不能只删数据库索引。

证据：普通召回已经按 fact ID 汇总并返回多条。`memory/resolution.py` 却在模型判断 COEXISTS/UNRELATED 时，只因 exact key 命中就生成 `coexisting_key_collision` 争议。这是把标签重合当成内容矛盾，不是权限或身份规则。

联合删除范围：四个 active-key 唯一索引、`find_active()`按key取任意首行后的自动替换、supersede/restore/resolve中的key碰撞否决、同槽重复audit错误、以及根据key强造争议的规则。修改和删除沿真实fact ID，修正关系沿实际语义。

行为会改变：同key的独立事实可共存，不再自动覆盖一个旧值。已有Memory选择器可沿多候选路径处理，但偏好管理不是这样：`services/admin/preference_admin.py:69–150` 用 `{row.key:row}` 静默压成一条，QQ和WebUI的set/delete都调用它。该字典覆盖和无ID单值修改假设也需联合删除，复用已有fact-ID修改/删除入口，不新建槽位消歧器。

真正的语义矛盾仍应可见，不能将“解决选中子集”写死成“所有相关争议都已CLEAR”。旧clear赋值和状态审计也需一起审查，而不是再加一个全局槽位检查。

这能从策略层消除本例2067的前提。若实施核查发现某个业务仍确实需要唯一值，要指出该实际消费者并缩小范围，不能以旧合同继续要求整个Memory单值化。也不为躲异常先删掉RESOLVE业务能力。

生产SQLite已有索引，需要现行迁移流程真正删除；新增的必要迁移只承担拆除索引，不添新约束、新表或替代状态。

### 4.3 正文4000字与静默截断

4000同时存在于自省模型、MutationRequest、MemoryFactCreate以及 `normalize_memory_text(...,maximum=4000)`；最后一处会切掉正文。SQLite正文列是Text，没有发现对应4000存储约束。

只删入口限制会把失败推到下一层，或变成默默截断。应联合取消重复长度拒绝与正文截断，并删相关提示。保留已有模型请求的真实输入/输出预算；不要用新增质量校验器替换被删除的字数限制。

## 5. 少数问题只需移动现有责任

### 请求登记与收尾

将已有account以及登记后的失败收尾一起放到payload准备完成后的HTTP段，删complete外围覆盖整个_post的过宽失败收尾。仅移动account一行不够：finish使用的request_id可能仍是0或上一次请求，修复轮次会错误改写上一条状态。

保持预算在HTTP前登记，已派发的请求与真实重试照常累计。这是顺序修复，不建preflight、退款账本、错误分型或健康缓存。

### Dream工作循环

`dream/worker.py:162–170` 已有异常处理，却将drain放在其保护范围之外。先复用现有循环与恢复，不按每个异常新增分支，也不新建模型决议checkpoint。

现有恢复会核对committed operation；已提交事实效果不再重放。未提交的模型整理是派生判断，可以在原ID下重新生成，累计次数保留，不必因为没保存每次思考就让worker永久停止。取消和停止仍沿原生命周期。

现有run总额度只限制INCREMENTAL；`dream/service.py:1003–1006` 在FULL传maximum=None，worker也排除了FULL。直接删单簇两次上限并持续轮询，会让FULL在反复失败时持续花费。删除优先的收敛方向是同时去掉FULL模式豁免，复用已有run预算，不添第二个限额层；代价是全量目标也可能暂未处理完，不能把预算延期/本轮结束宣称为全部事实整理完成。

允许再生成意味着可能多一次模型费用，不等于可以重置额度。若一次事务的结果暂时不能核对，沿既有回执恢复入口处理；不能把“没看到诊断”当作已确认无效果。

## 6. Provider分工沿已有能力

实施后保留三条可独立绑定的任务：`memory_extraction`、`memory_self_reflection`、`memory_dream`。Rebuild 复用提取；consolidation 分类与 attribution 任务、路由和专属配置已随旧策略删除。Embedding 继续独立配置。

配置可以共享也可以分开，不强制多个 Provider，不新增路由系统。事实仍归Person/Space/SELF，不随Provider复制。自省当前超时和输出量来自任务配置，不冒称全部由Profile默认值控制。

先删覆盖配置和重复许可；旧模式展示不足不是本次必须新增前端功能的理由。向导停止非初始化覆盖后，模型调整沿已有WebUI和Profile文件入口，不再提供一套隐式重建所有连接的编辑方式。

## 7. 扩大检查：删除整条流程与空接线

本轮进一步核查了实际调用者、装配和下游用途。以下保留实施前的候选证据，完成结果见索引与交付记录。“无调用者”指当前仓库没有实际调用；“撤销策略”则明确有消费者，需要连同产品行为一起退役，不能伪称死代码。

| 编号 | 新增对象与证据 | 删除范围及行为 |
| --- | --- | --- |
| D09 | **模型记忆审计API无实际调用**：`memory/auditing.py:1–292`的audit_fact/audit_entity只在本文件相互调用；外部只有conversation模块构造/传递、Container保存 | 整文件、User/Self审计模型实例、装配字段和仅用于该API的DTO一起删除。不动`memory/audit.py`的真实SQL审计，不保留空壳审计接口 |
| D10 | **已停自动注入的整链**：`chat.py:1370`固定empty_retrieval；`context.py:197,283`两个自动查询入口无src调用；唯一diversify=True也在无人调用入口 | 删自动query拼接、SELF补充、自动额度/校准、`_limit_automatic_result`、MMR算法与三项配置；删context_assembler旧事实注入包装及零事实receipt。主动search_memory和真实聊天资料/历史保留 |
| D11 | **归因→强化→Activation→IntentRanker整项评分策略**：`attribution.py:225–333`、`context.py:591–654`、`activation.py:42–220`、retrieval两处rerank | 这条链有真实调用，但作用是派生排序、自我强化与诊断。可撤掉归因模型/队列、持久强化、固定意图权重、activation补行和专属回执，直接沿现有FTS/semantic/RRF排序；删专属CLI/Admin解释、指标、配置和质量门槛，不做替代评分器 |
| D12 | **空接线与只写不读的运行时结构**：MemoryStructuredCommand生产只传NONE；AttributionHandoff状态没有读取者；default_purpose、retrieval_degraded无外部消费者；turn_session._query仅构造 | 删READ/WRITE枚举分支、参数透传、不可达ORIGIN_WRITE_DENIED、归因状态机及freeze/queue/skip、死字段与无用QueryPlane实例。真实origin权限、曝光数据和实际enqueue/投递事实由原消费者承担 |
| D13 | **候选暂存与人工升格**：`validation.py:183`低于0.65暂存；`worker.py:378–404`以两条证据触发并max(0.75,confidence)抬分；named_subject_unresolved无后续解析消费者 | 删整条低分→暂存→证据数→后端抬分路线，不只改阈值；置信度作为原有数据保留。删无人消费的未决人名暂存，沿既有拒绝返回；ABOUT_YUKI→SELF仍有自省消费者，不把整个candidate仓库一起删 |
| D14 | **模型解释标签成为第二准入层**：`quality_policy.py:65–108`subject_basis硬配对；RetentionPolicy重复调用；SELF分类/受保护key名单仅用于拒绝 | 删subject_basis配对表、重复风格/价值准入、强制value_reason及提示；删SELF五分类、kind/category机械强配、identity/core/safety等key名称禁写。真实主体由现有SubjectResolver解析，名字本身不赋予系统权限 |
| D15 | **已准备对象又转DTO重解析**：mutation._prepare已解析target/owner，_validated_claim又造MemoryClaim经Processor/Validator重解析；bot_aliases参数接收后立即del | 以已有prepared target/evidence和direct-target构造为删除线索，减少重建DTO、重复解析与两套构造；bot_aliases参数/透传可直接清掉。DTO往返需核对真实语义后整组删，不以删一个调用冒充已等价 |
| D16 | **deterministic governance/reflection整条后台队列**：当前注册启动；任务为文本相同自动择主merge、当前membership缺失contest、已contested再contest | 退休discover→fingerprint→jobs→恢复/重试循环与governance别名。接受重复或未决事实，不靠现成员关系改写历史归因；Dream和即时写入已有整理能力。维护改用现成纯DB分支，删mutate_reflection“伪装成聊天修改”包装 |
| D17 | **跨worker长锁与旧格式补链器**：Dream和证据压缩共用锁，持锁跨模型等待；compaction每轮扫描旧三段self_episode格式 | 删跨worker锁注入、等待/持锁健康字段和callbacks；复用Dream已有自身串行职责和各自短事务复核。删`_backfill_reflection_results`及专用依赖/测试，当前结果身份已与mutation同事务保存，不再从旧bot+事件范围反推run |
| D18 | **未审查先记已检查**：Dream baseline收集所有signature后才检查向量；isolated筛选与模型遗漏也自动checkpoint | 删首次baseline状态/聚合/延迟复查、单条候选拒绝、isolated与未选事实自动checkpoint。显式keep或其他实际operation仍保存真实checkpoint，未处理部分可进入未来计划，不新增partial状态 |
| D19 | **Rebuild因换模型或版本封死恢复**：指纹含model/prompt/schema/resolver版本；resume拒绝且每轮强pause | 删“当前档案必须等于原fingerprint”的封口，指纹可留作历史说明；已存proposal用现有source hash、当前owner/validator和批准记录提交，不建分段模型状态机 |
| D20 | **检索重复筛选与多层排名**：query400/entity5/kind3/64字符等重复限制与截断；semantic0.35硬门槛；SQL时间过滤后再次过滤；局部融合后又全局融合 | 成组取消经验长度/数量/绝对分数门槛和静默截断，删重复应用层时间过滤与被覆盖的排名计算。保留真实授权范围、用户指定时间语义和已有候选/返回预算，不换成新规则引擎 |

### 7.1 这次需要连带清掉的装配与下游

- D09只有构造没有调用，不把它与实际运行的MemoryAuditService混在一起。
- D10的MMR约百行算法只服务无人调用入口，可连同向量重复读取、解码和配置一起删。ContextAssembler只删旧facts部分，不删除真实上下文组装。
- D11不是死代码，删除会改变“回答用了哪条记忆便提高后续排名”的产品行为。现有关键词/向量排序仍在；不承诺原个性化权重效果。recall receipt/item属于该策略的诊断，不是mutation/Dream/request账本；删除专用消费者后停止新增即可，不为了删运行时而强制清空历史表。
- D12即使保留D11，handoff状态机也没有业务读取者，可独立删除；即使暂不撤评分，也不用留“为了将来”的空接线。
- D14里category还参与私聊→GLOBAL的传播例外，这是真实消费者，不能与分类命名白名单一起机械抹掉。删除命名禁写不删除当前真实主体与可见范围规则。
- D16的维护已存在`maintenance.py:187–224`纯数据库实现，具备证据准备、短事务和现行数据库竞争处理；无需新维护器。撤掉治理后，不再按旧key和当前membership自动宣判历史事实不可信。
- D17撤的是Dream/压缩跨任务互斥，不顺手删Dream preview/rollback已有自身串行。旧三段补链器约160行，当前调用为`self_episode:{run_id}`并显式保存结果；旧孤儿结果可暂不压缩，证据仍保留。

### 7.2 部分结果不再被强制闭合

Dream显式keep会创建并提交operation，选中事实的checkpoint绑定该operation。遗漏或空actions却由`service.py:527–532`额外写operation_id=None的checkpoint；原样保留不等于已经审查。删全覆盖条件时要一起删这段自动闭合。

删除后，未选事实未来仍可被选入；当前run不会自旋，cluster结束后由下一计划继续选取。未来同簇可能再次包含已keep事实，可能增加一次模型调用，但不会重放旧operation。`run COMPLETED`只表示本轮结束，不能宣传为所有事实已整理。

同样删除baseline和isolated的假检查。已有历史checkpoint可能来自真正处理，也可能来自旧baseline，不能无差别清掉；本次只核查未来写入路线，不宣称已经恢复历史漏处理。

### 7.3 不用删除一个检查冒充已经拆掉依赖

Dream目前确实读取非optional向量并逐对计算相似度，未找到现成无向量planner。不能只删“需要Embedding”的两条raise就宣称解除依赖；要么另行退休整条向量规划策略，要么保留这个真实消费者。本任务不新增备用聚类器。

统一search当前没有空query的overview实现，也不把删除非空检查造成的空成功说成支持浏览全部记忆。各类实际来源、权限与效果身份继续按真实消费者核对；不以扩大删除数量为由造一个名义上的成功。

### 7.4 本次必修：issue #270 的人物占位与身份错配

用户要求将[issue #270：记忆存的内容质量问题](https://github.com/YuanYeYouTao/Yuki/issues/270)纳入同一轮修复：记忆应保存真实人物名片、姓名或ID，不再保存“被提及者”等没有人物信息的称呼，并检查其他记忆存取路线。此项记为D21，与D14的标签准入删除共同处理，不以新增占位词黑名单解决。

源码实际生成的是`[提及成员N]`和“被提及成员N”，未找到字面`[被提及者]`生成代码；模型可能改写这些标签。本轮不据此推断生产污染数量。

已核对的链路：

- `adapters/onebot/normalizer.py:158–164`把数字at对象改成`[提及成员N]`，虽另存mentioned_user_ids，正文却失去可辨认人物信息。`event_prompt.py:450–463`又从segments重投影同样的序号正文；外围身份注释不能消除正文里的歧义。
- `memory/subjects.py:84–111`再编号提及主体，display_label只写“被提及成员N”和“回复消息作者”。`SubjectContextBuilder.build`直接返回它，未使用已经注入的PeopleRepository为这些标签提供姓名/名片。
- `memory/event_extractor.py:54,74`又禁止输出QQ号；单条/批次提取读取event.evidence_content，而不是带真实提及身份注释的聊天渲染。删掉阻止事实正文记录真实人物ID的禁令；数据库owner和权限仍由后端决定。
- `services/context_assembler.py:1279,1309`在提及/回复人物profile缺失时使用“被提及群成员”“被回复群成员”；`domain/profiles.py:24–26`缺名时又退回“当前用户”。这些用于人物显示的分支应使用已有reply_rep/user_id兜底，不只把一个无名词换成另一个。
- SELF自省复用ChatEventPromptRenderer，Rebuild复用MemoryEventExtractor，因此同类序号设计也进入这些模型输入。Dream的`repository.py:103,181`、Embedding的`memory/embedding/text.py:39`、召回的`memory/context.py:40–56,95`及最终工具的`agent_tools.py:2475,2503`原样传事实正文，没有末端替换占位词；问题已进入事实正文，不能靠召回后置改名修复。

已排除两处表面相似但用途不同的代码：classifier只输出candidate_ref的关系标签，不生成或改写事实正文，其QQ输出提示不造成此次信息丢失；Rebuild的DISABLED第三方模式删除提及元数据是该选择的写入范围，不当作匿名化策略一并撤掉。

**已用现行源码在本地复现身份错配，未调用模型：** 发送者10001依次at10001、10002、10003，normalizer正文为`[提及成员1][提及成员2][提及成员3]`。SubjectResolver先排除发送者，再把mentioned_1映射10002、mentioned_2映射10003。因此正文的“成员2”是10002，记忆别名mentioned_2却是10003；模型按序号理解可能写错事实owner。这不是单纯显示不好。

删除方向是取消正文和显示标签的无名序号，复用已有segments中的明确目标ID、EventRecord人物快照和PeopleRepository人物/群名片读取。名片或姓名可用时显示真实名称；未知名称时保留真实ID，不退回“被提及者”。subject_ref只承担后端映射，不要求模型把它当人物姓名写进正文；模型内容引用真实人物信息不等于获得写入权限。重复at、提及发送者、旧Yuki账号、reply作者、成员别名与Rebuild主体适配一起核对，不再建另一张序号补偿表。

原事件和evidence_quote仍是实际来源。新的可读姓名是显示资料，不能把改写后的姓名当作历史原话，也不通过删来源核验让引用假装成立。已存占位事实仅在原来源可确定时按已有纠正入口处理；不按占位词全库猜人、不重新生成整库、不悄悄改历史账本。实施时同步删旧提示和固化序号的测试断言，以实际身份、正文、证据和owner一致作为验收。

定向验收覆盖发送者at自己后再at其他人、重复at、名片缺失但ID存在、回复作者和同名成员，核对单条/批次自动提取与Rebuild的subject对应。SELF的渲染与mutation证据读取共用event_content，变更需一起验证；自动/Rebuild的quote仍匹配原event.evidence_content。验证已有身份路线，不增加一套占位词检测或修复框架。

`tests/unit/test_normalizer.py:144–159`目前明确断言提及不出现真实ID，是这次应更新的旧设计断言；不以保持旧测试通过为由保留匿名正文。

#### 已关闭issue与可复用的现行能力

已核对[issue #50](https://github.com/YuanYeYouTao/Yuki/issues/50)及关联[PR #52](https://github.com/YuanYeYouTao/Yuki/pull/52)：它们要求保留at对象和相对位置，不能把机器人提及连同人物信息过滤掉。`373327c5`加入的历史segments重投影保留了位置，但当前正文仍是成员序号；已关闭不证明当前Memory已取得可辨认姓名。

现成的姓名能力分布在`ChatEventPromptRenderer._identity_label`（name/QQ侧注，历史引入于`777118cc`）和`ContextAssembler._event_bound_memory_refs`→`PeopleRepository.get_many`（群名片/昵称，历史引入于`3fe61de0`）。提取器的SubjectContextBuilder也已经持有PeopleRepository。优先复用这些身份读取和显示职责，让原at对象信息进入Memory，删除平行的无名显示和重复编号；不另建记忆专属名字解析链。

`ProjectedMention`已保存target_user_id和segment_index，可复用原位置，不做序号反查。当前正文还有另一个入口：`ContextAssembler`传current_content，`event_prompt.py:427–428`直接返回它；只修历史重投影会漏掉本轮正文，两个入口应沿同一已有显示职责接入。

当前没有查到可直接调用、完整把正文at还原为群名片的统一入口。侧注、资料读取和正文投影是三个不同环节；不能把其中一个存在冒称全部已实现。必要接线应在已有职责中完成，不新增“序号→名字”补偿状态，也不把整份聊天上下文搬进记忆提取。旧事件优先用已保存segments和当时身份资料，不能把今日改名伪装为历史名片。

## 8. 全仓测试大幅裁剪（本次必做）

用户明确把“砍掉大部分测试文件”纳入本任务。按全仓审计和大幅净删除执行，不把范围缩回Memory；也不把某个删除百分比做成新的死规则。保留项需说明能发现哪类真实错误，测试数量、旧CI全绿和覆盖率不能独立作为验收。当前已证实一份严格重复、多组随退役业务删除的测试及一套固化旧策略的质量框架；尚未逐个审完432份测试，不能提前承诺所有未查文件都无价值。

静态基线：`tests/`内有432个`test_*.py`文件，包含2706个test函数定义、134012行；不含参数化展开，也不含frontend和插件独立测试。函数数不是实际执行用例数。本轮未运行全量测试或collect，不冒称已验证。

### 8.1 已核对的删除组

下表路径相对仓库；unit文件均在`tests/unit/`。混合文件不因文件名而全留，也不为保住文件而保留旧用例。

| 对象 | 核查证据 | 裁剪方向 |
| --- | --- | --- |
| `test_memory_writer_boundaries.py`整文件 | 七个函数分别验证Activation补行、私有游标和governance恢复 | 随D11/D16整文件删除，真实SQLite竞争由仍在运行的写入/回执用例承担 |
| `test_reflection_backfill_readonly.py`整文件；`test_evidence_compaction_preparation.py:102` | 调用即将删除的`_backfill_reflection_results`，断言旧三段补链和SQL条数 | 随D17整组删除；后者仍有真实证据保留/短事务用例，不机械删掉这些不同职责 |
| `test_memory_retrieval.py:61,112,847,1173`及自动注入相关用例 | 归因mock、零曝光回执、旧planner模式、自动阈值校准与limit包装 | 随D10/D11/D20删；保留或归并少量实际SQL授权、用户指定时间范围和索引故障验证 |
| `test_normalizer.py:144–159`、`test_event_prompt_mentions.py:43`等 | 明确断言“不暴露真实ID”或固定序号正文；同一用例又包含真实内部reply ID冲突验证 | 删匿名/序号设计断言，归并为D21人物信息与owner一致的验证；不能顺手丢内部引用身份检查 |
| `test_work_execution_dependencies.py`与`test_work_execution_receipt_regressions.py` | 两文件SHA256完全相同，七个测试函数及内容一致，未发现模块导入者 | 删除重复的一份，保留一份真实效果回执/unknown恢复验证；不增加替代用例 |
| `test_agent_core_differential.py`与`fixtures/agent_core/runner_golden.json` | 比较旧`b4fdef7d`循环完整序列；当前改动需更新golden才通过 | 退休旧实现的完整序列快照；`test_agent_core_differences.py`仍导入两个helper，先就地复用到现有消费处，再删源文件和数据，不增通用兼容层 |
| `test_memory_quality_gates.py`、`test_memory_quality_diagnostics.py`整文件 | 验证冻结基线比例/阈值和benchmark对SQLite/GC/方法的诊断hook，不直接验证Yuki记忆内容 | 随下文D23整文件删除；不保留只服务退役基准的探针 |
| `test_memory_quality_governance.py`混合文件 | release-check强制合成基线，health硬否决，与真实来源审计混在一起 | 删退役发布门槛和旧健康否决测试；只保留/归并实际来源错配、真实审计不泄正文等验证 |
| `test_codemode_default_policy.py`、`test_static_mode_prompts.py`；`test_complete_direct_tool_policy.py:22` | 锁定中文提示词、私有状态，硬写工具总数44 | 前两文件随少量现有真实派发验证归并退役；删44数字断言，不靠工具数证明可执行权限 |
| `test_long_task_benchmark.py`、`test_long_task_summary.py`、`test_deepseek_acceptance_harness.py`、`test_pi_codemode_inventory.py` | 四文件共897行，验证旧benchmark/export输出，部分只断not_run/not_collected | 随已无现行用途的基准/导出脚本整组退役；仍使用付费脚本时保留真实费用约束，不只删测试继续留昂贵旧入口 |
| `test_private_dispatch_source_reads.py`、`test_history_preparation_reuse.py` | 固定session 3/2/1、SELECT列顺序、分页[256,256,89]和getter调用数 | 删除内部布局断言；少量真实历史输入保全/竞争行为归并到现有路径验证 |
| speech/MCP退役表面测试、frontend speech-retirement、分散`test_migration_*` | 退役字段hasattr、权限/help穷举与重复fresh-head；29份迁移测试另有原回执保全 | 成组删旧表面矩阵和迁移布局穷举，归并成少数真实旧库升级/原pending或unknown效果保全验证；不为每个revision留文件 |
| `tests/support/identity_erase.py`与旧Pi验收export矩阵 | 前者无代码消费者；后者仍映射不存在的test_tool_kernel_mcp | 删死support与退役导出数据/脚本；pi_migration_seed.py仍由subprocess调用，不按无import误删 |

### 8.2 不只删文件：旧质量框架也一起退役

Memory quality套件有真实CI/CLI消费者，不能说是无人调用。但`quality/fake.py:35–79`按正文返回预写claim；`quality/runner.py:498–506`的context项只是把同一检索hit投影成字典，没有经过真实聊天ContextAssembler。它运行真实worker/SQLite，有宿主逻辑测试价值，却不能证明模型识别人名、提取语义或真实聊天使用正确。冻结baseline中的`duplicate_active_fact_rate=0`还绑定D03正在撤销的单active策略。

退役synthetic full→冻结baseline→compare及专属诊断、报告、fixture和快照维护链，不扩展另一套质量管理框架。`cli.py:280–290,628–747`的相关命令、`quality/release_check.py:59–122`的hash/固定10k事实100k事件样本/full再跑/合同快照要求、`config/memory_quality_gates*.toml`及专属数据一起核对退役。该CLI也有REAL_MODEL/REAL_EMBEDDING模式，不能声称所有功能都是fake；整组退役会撤掉这个评测入口，不把它偷换成本次必要新增验收系统。

不要按目录整删`memory/quality`：`quality/audit.py`和`quality/hygiene.py`仍有真实数据库CLI消费者；它们与无人调用的`memory/auditing.py`模型API、自动governance队列不是同一件事。删哪些诊断消费者，哪些来源查询仍需要，应按实际用途收敛。

### 8.3 CI、夹具和最小验收一起收敛

`.github/workflows/quality.yml:159–171`硬写八个pytest nodeid，其中包括固定提及序号的旧测试。删除这层按名字强制存在的门槛，不为旧nodeid留空函数。`:73–119`的memory-quality job随D23撤去；其余验证随最终保留的实际行为调整，不用skip、xfail或吞错冒充完成。

继续检查全仓fixture/support/benchmark/script的消费者，移除仅被删除测试引用的代码、数据、额外依赖和文档入口。当前测试跨文件导入helper、脚本直接导入integration fixture，删除时一起收敛调用者，不让测试配套变成新兼容层。

已静态找到344条跨测试模块导入，涉及96个被导入test模块。外部还有frontend18份测试文件（4541行）、plugins13份（4481行）及example1份；这些也在裁剪范围，不因不在默认pytest目录而遗漏。未执行的参数化数量不作为实际用例数报告。

保留的重点是少量实际链路：人物信息进入提取输入后对应同一owner、证据与真实来源一致、用户指定可见范围、已提交效果不会因重启/断连重复、取消后不继续发送、当前支持的数据库升级与SQLite写竞争。用实际DB/账本/Provider请求或原回执核对结果；fake可以隔离外部服务，但不能把预写回答当模型语义验收。删除旧限制的测试无需一对一补回；低影响接线也不要求新建回归文件。

最终记录测试文件/有效用例/专属夹具与依赖的净减少，并说明保留验证覆盖的真实问题。大幅删除是本次目标，未审计和未实施的部分仍如实留在索引。

## 9. CI大幅裁剪（本次必做）

本轮已读完整`quality.yml`和`release.yml`，共668行，核对了实际脚本依赖。不是只把pytest换成短名单：旧job、前置门槛、重复构建、一次性发布分支和配套脚本都一起收敛。以下仍是待实施方向，本轮没有修改workflow或GitHub设置、触发构建或发布。

| 对象 | 已核对的问题 | 删除方向 |
| --- | --- | --- |
| Quality的baseline job | 每个PR/main都运行release_validate；它要求四份文档含同一release/schema marker，并把Plugin API固定为3.3 | 删普通CI的发布基线job和对应needs；删文档marker/API常量封口。正式发布仍沿实际tag、版本、提交身份和可用迁移验证 |
| memory-quality job及专属制品 | run→compare重复比较，失败后无论原因再full一次；专属依赖安装与D23框架绑定 | 随D23整job退役，带走诊断重跑/上传及无消费者配置，不留假成功job |
| collect+required_tests step | 收集全套后按八个完整nodeid判断是否允许继续 | 整步删除；业务验证通过由实际结果决定，不要求旧函数名永远存在 |
| changes、force_docker和普通docker job | 自制Git diff分类job只供docker；正式tag先复用Quality构建开发compose镜像，Release又buildx direct镜像 | 去掉重复普通镜像构建及专属分类job/input，实际发布只构建所发布的direct镜像一次；需要的Compose解析与真实启动沿现有发布检查收敛 |
| 每次PR/main/tag整套重复验证 | Quality既接PR/main，也被Release强制复用；tag的release_validate又与Quality baseline重复 | 普通CI收敛成基础代码检查、裁剪后的少量有效测试及必要构建；正式发布不再重跑旧整套质量流程，不新增按文件归类的测试选择引擎 |
| Python job混入的前端/插件/安装器矩阵 | frontend format/lint/test/build、主pytest、example、两个plugin pytest/CLI合同、插件mypy、fresh install全部串行 | 删除风格/表面/重复合同检查；保留的编译、真实安装和行为代表各执行一次。插件pytest不在主testpaths，不能假称整套重复；其中已有contract test与CLI plugin test才是真实重跑对象 |
| release本地full smoke与匿名smoke | 两轮都执行Guided Setup、Bot启动、schema；full还重建Bot和拉NapCat检查其挂载 | 删除重复本地full及对外部QQ客户端的强耦合验收，收敛成对实际发布镜像/部署包的一次启动与真实持久化验证；只测Bot健康不能声称QQ登录或自然回复已验收 |
| bootstrap与finalize平行发布链 | bootstrap用于初始化，仓库已有正式发布历史；finalize重复打包、smoke、latest、digest和assets发布的大段代码 | 优先退休一次性入口和完整复制分支。现有正式发布已核对同版本image revision、已上传asset内容，优先沿原tag重跑复用这些事实；撤finalize入口须确认其恢复职责已由现行流程承担，不按近期没有记录认定死代码 |
| 固定产物数8、逐项布局与重复镜像标签检查 | 打包后按文件数封口，与真实bundle可用性并非同一件事 | 删数量/布局/重复标签矩阵；保留实际版本/提交对应、部署必需文件及真实发布内容，不把删除检查当作允许覆盖别人的版本 |

发布缩减不能把已push的原版本镜像改写成另一commit：现有版本镜像revision核验和同名asset比较是实际效果消费者，收敛时直接复用。latest及Release仍指向实际通过验证的版本，失败保留可恢复的原效果；不要为这点另建流程。

如果finalize仍承担独立恢复入口，先删除平行尾段及“当前分支版本必须等于旧版本”的封口：原tag/version/SHA决定恢复对象，今日main版本不是恢复旧发布的身份。可以保留必要入口而撤掉完整复制流程，不为删除数量另造新模式。

删除job后同步清理needs、if、workflow_call/input、报告上传、bundle allowlist和失去调用者的脚本/测试。若远端配置引用被删check，按真实配置一起调整，不能留永远pending的检查名。本轮只查过main旧式branch-protection接口返回“Branch not protected”，未完整读取rulesets，不据此宣称远端无检查要求。

实施验收重点是workflow依赖可达、精简保留检查确实执行、原构建/部署包可用，以及原tag重复运行不重写不同发布效果。不要求另开一套全量验收或生成新的CI约束索引。

## 10. 终局审计：逆向追查防御层

本轮沿“异常后补判断、补判断后改状态、改状态后再恢复”的路径继续核查，检查实际调用者和写入结果，不按`except`、`min/max`或validator的出现次数判罪。最值得删除的是把不确定信息改成确定结论、多个层级同时否决同一输入、用经验数字替代模型判断，以及没有真实消费者的完备性要求。以下列出本轮纳入的删除范围，执行状态以索引为准。

| 编号 | 当前链路与证据 | 删除方向与实际行为 |
| --- | --- | --- |
| D25 | **后端来源评分代替认知判断**：`memory/evidence.py:16–121`固定权重、authority等级和置信度cap；`service.py:480–556`准备聚合再重写fact元数据；`resolution.py:80–140`只选最高分关系、按来源等级自动取胜；`claim_processor.py:305–331`吞分类失败后继续落计划 | 撤派生置信度重算、固定authority胜负与“分类器不可用→同key旧事实自动contest”，连删配置、排序/确认级联及专属测试。保留原证据、来源类型和现有授权；来源是谁不等于后台已证明哪句话真。沿已有独立CREATE或真实失败处理，不另写兜底裁判；与D03同时撤同key强闭合 |
| D26 | **为保守而隐式遗忘**：`repository.py:1728–1773 make_room`在scope满时将低importance旧事实写INVALIDATED/STALE；`lifecycle.py:30–63`以天数、分数和来源级别认定stale；`claim_processor.py:343–362,421–426`又用容量拒Rebuild | 删除容量腾位失效、固定条数准入与按来源/分数/年龄自动过时路线，连scope-limit透传、stale SQL窗口、配置与测试。容量腾位不删除原数据，不能宣称它节省了实际存储或内存。用户/事实明确的valid_until与显式失效仍是现成职责，不因撤经验过时而删掉 |
| D27 | **Rebuild重试和整批闭合**：`rebuild/service.py:858–885`外层再包三次任意异常重投，下层`repository.py:480–518`已有真实517处理；`:240–241`要求全部review；`:692–699,898–905`单条耗尽使全run FAILED；`reset_failed`清attempts | 删重复泛化重试、未决项阻止已批准项提交、单条失败封死其余项与累计计数清零。复用现成APPROVED筛选/写时核验和原proposal回执；一起收敛review/commit入口、分页推进和完成判断，未处理项保留。不能只删break后反复扫同一失败页，也不能把未review事件写done；沿已有REVIEW/暂停语义，不添partial状态 |
| D28 | **SELF重复视图和表面多来源**：`self_reflection/service.py:522,532–558`另取previous_episode并造DTO，但已有existing_episodes；`:635`proposal声明多个evidence_refs却只取首条 | 删previous_episode查询/DTO/字段/专属提示，沿已提供的可见事实历史。撤首条证据捷径，复用episode现有additional_evidence能力保存真实选中来源，不能只让剩余refs通过校验却丢掉。最新episode可能不在当前20条视图中，删专属输入会改变偏好，不谎称两份输入始终等价；不新增来源表或补偿队列 |
| D29 | **相同工具结果被当成无进展**：`services/turn_execution.py:1537–1568,1615–1623`hash结果/参数并记repeats，达到2且无有限预算就WorkNoProgress；supervisor转不可自动重试的NO_PROGRESS；`work_control.py:449–480`预算查询只有此caller | 整条删fingerprint/repeats、专属预算查询、异常分类和通知。相同读取结果不证明目标没有进展；真实预算、取消、原效果回执和不重发仍按现有职责处理。删`test_work_no_progress_feedback.py`及pause-notice/旧golden专属断言，不以新相似度检测替代 |
| D30 | **压缩必须闭合且必须更小**：`work_compaction.py:26–38,126–142`逐输入三分类+reason、缺任一即拒；kind/reason无业务执行消费者；更正静默[-16:]；`work_session.py:1257–1265`与`ordinary_compaction.py:139–157`另要求候选小于原请求 | 删InputDisposition与全集分类校验、专属提示、16条slice和candidate_not_smaller独立拒绝。沿已有来源引用、原输入账本、分页和真实容量处理；不新建归档链或分类器。完整输入分类没有证明语义完整，候选足够容量时不因未变短再否决。当前原请求fits可继续前台，不把该策略夸大成每次都阻塞 |
| D31 | **显式关闭思考仍被后台开启/抬档**：Settings/Profile/Executor三层reasoning floor；`domain/messages.py:48–52`取max且至少low；`llm/vendor_policy.py`按整族供应商改档位和预算 | 撤强制thinking=True、low下限、请求/Profile取最大，以及medium→high等整族自动抬档和指数预算。沿原显式配置与必要参数方言；不是死代码，是撤掉真实产品策略。同步配置展示、Provider合同和抬档测试，不能只删validator而保留Executor覆盖 |
| D32 | **适配器“救活”输出的平行执行**：Claude `anthropic_messages.py:65–164`另做两次付费pause续跑，catch所有LLMError回旧INCOMPLETE，并固定搜索max_uses=5；DeepSeek Chat/Responses把正文DSML转ToolCall，通用OpenAI Responses也继承这条转换 | 删Claude适配器续跑/usage合并/吞后续失败，沿主执行已有pause checkpoint恢复；没有该恢复消费者的直接模型任务收到真实INCOMPLETE，不暗中买额外调用。删固定搜索帽。成组退休正文DSML parser/自动call ID/清正文并造function_call记录，沿真实协议tool_calls/function_call。只返回DSML的端点将失去这项兼容能力，要如实说明；现行执行仍有权限核验，不据此宣称已经发现越权。原opaque、配对、真实usage和效果回执继续复用 |
| D33 | **名词和版本作为可用性证据**：Settings扫描进程env因已退役3.6名字而拒启动；Profile重复检查schema版本/旧route名；ModelProfile和pool再按provider品牌白名单拒显式protocol，并按gemini-3.8前缀禁budget | 删无消费者旧env否决/regex、Profile手工重复版本与退役名词表、固定型号前缀封口。品牌准入退役需连带核对pool与`executor.py:511`续接身份：通用适配器返回固定provider标记，不能只放开名单就宣称任意名称完整可用。复用明确protocol和现行schema/ModelTask职责，不新增品牌插件或内容路由，也不删除真实checkpoint归属和native工具授权 |

### 10.1 已有编号补入的漏项

- **D02**：自动worker在配置外再次`min(batch_max_events,12)`；DTO/提示重复12条输入、36条输出、8条上下文，提取器另裁正文8000、上下文1000、发送者128。Rollup也有16项/128引用/1024正文及service/repository重复篇幅否决。成组撤盖帽、切片和提示，沿现成请求预算/实际容量，不只放宽一层留下另一层拒绝。
- **D02/D15**：Validator先核原quote在完整事件中，随后把source裁4000，再对这个副本否决。删源文裁短和第二次副本准入，引用仍按完整真实来源核对，不能用删除来源核验来掩盖问题。
- **D10/D20**：已停自动注入的校准规则仍通过`match_projection.py`给主动工具回执套topic_admission passed/not_passed/unknown；默认未校准并非事实真实性结论。连这个旧策略投影、校准profile和topic阈值一起撤，实际词法/语义候选与真实预算不足说明不需要它才能成立。
- **D01/D04**：主循环空/畸形输出另设两次强制纠正、system修复提示和持久计数；不完整输出又单独封一次续接。撤这些额外纠正链及终身次数封口，沿已有真实失败/请求预算/协议暂停处理。保持原未执行调用配对与原生效果未知围栏，不把所有INCOMPLETE处理都当死代码删除。
- **D06/D33**：setup的固定Profile名猜协议、吞错误回env随旧非首次模型编辑入口一起退役，不保留另一条猜测配置路线。
- **D22/D24**：新增范围的测试/CI/提示/合同随运行时同步收敛，删除专为上表经验规则存在的断言，不为每项再次补整套测试矩阵。

### 10.2 本轮局部复现与准确界限

未调用模型或生产的纯对象复现得到：分类器无关系＋同key→`contest/classifier_unavailable`并创建新争议事实；third_party证据confidence=0.99被固定0.55权重改成fact confidence=0.5445；quote在完整事件第4000字之后真实存在，却返回normalized_evidence_not_in_event。另复现Profile及实际请求的false/none→True/low、DeepSeek medium→high，以及OpenAI Responses正文DSML变生成ToolCall/协议记录。它们是现行路线的行为证据，不是生产污染量或实际费用测量。

本轮未把已修的Work完成入口问题重列：当前settle_final/complete_final共用完成入口，拒绝完成不会再买一轮纠正。也未找到新的主链证据可证明“所有catch都会转成假成功”；不能为了凑删除数量把真实来源核验、原回执恢复、Rollup租约或取消后的效果禁止当作多余。删除是否完整按上述实际调用链和消费者核对，不声称全仓已无防御代码。

## 11. 实施索引

分组可调整；实施时每完成一组更新该行，并填实际删除与验证证据。索引按实际实现与验证更新；已实施不等于已上线。

| 编号 | 删除方向 | 状态 | 证据 |
| --- | --- | --- | --- |
| D01 | 模式覆盖、重复许可、fallback及Schema分叉 | ✅ 已完成（定向验证通过） | StructuredRunner 沿 Profile；删 schema projector/fallback/重复许可；离线模式验证通过 |
| D02 | 数量/篇幅/价值门槛和重复配置上限 | ✅ 已完成（定向验证通过） | 提取/SELF/Dream/Rollup DTO 与切片盖帽、重复配置上限已删；Ruff/mypy 与相关定向回归通过 |
| D03 | 记忆单值key策略及依赖它的写入/审计 | ✅ 已完成（定向验证通过） | 独立 CREATE、按 fact ID CORRECT；ORM/0102 撤四唯一索引；SQL 真实身份/来源回归通过 |
| D04 | 强制全覆盖、永久次数封口和重复调度 | ✅ 原删除项已完成 | 全覆盖、永久次数帽、drain 和纠正链已删；后续发现旧代次领取缺口，尚未修复，见 §13 |
| D05 | 请求登记/收尾顺序与Dream循环责任 | ✅ 已完成（定向验证通过） | HTTP dispatch hook 与失败收尾同步；payload 失败零登记离线验证通过 |
| D06 | 向导覆盖档案与失去调用者的合并器 | ✅ 已完成（定向验证通过） | 非初始化向导保档案，删合并器/固定名猜协议/专属自省 Profile |
| D07 | 历史积压报警与运行健康硬否决 | ✅ 已完成（定向验证通过） | 历史 isolated 不再判健康失败；旧 classifier/slot 健康字段删 |
| D08 | 清理旧配置/提示/合同/测试并验收真实行为 | ✅ 已完成；文档复审已整理 | 原验证见交付；本轮逐份删除/修正过时合同、Release、README 和开发约束，见 §13 |
| D09 | 无调用者的模型审计API与装配 | ✅ 已完成（定向验证通过） | auditing.py/API/装配删除；真实 SQL audit 保留 |
| D10 | 停用自动注入链、MMR与空曝光 | ✅ 已完成（定向验证通过） | 自动注入/MMR/空曝光删除；主动授权 search 保留 |
| D11 | 归因/强化/Activation评分及专属诊断 | ✅ 已完成（定向验证通过） | 归因/Activation/强化/专属指标/CLI 消费者删；旧数据表不清空 |
| D12 | 空命令接线、ghost状态机与死字段 | ✅ 已完成（定向验证通过） | StructuredCommand/AttributionHandoff/曝光接线与死字段删 |
| D13 | 候选人工升格与无消费者暂存 | ✅ 已完成（定向验证通过） | 人工抬分/未决人名暂存删；ABOUT_YUKI→SELF 原消费者保留 |
| D14 | subject_basis、SELF命名、重复风格准入 | ✅ 已完成（定向验证通过） | subject_basis/retention/source_style/value_reason DTO 净删；旧 DB 列兼容保留 |
| D15 | DTO重解析往返与空兼容参数 | ✅ 已完成（定向验证通过） | prepared target/evidence 直构造；bot_aliases 和空参数删 |
| D16 | 重复治理队列与维护聊天包装 | ✅ 已完成（定向验证通过） | governance/reflection 队列、无消费者 ORM 与维护聊天包装删 |
| D17 | 跨worker长锁与旧格式补链器 | ✅ 已完成（定向验证通过） | 跨 worker 长锁/回调/持锁健康和旧 backfill 删；Dream 启动元数据恢复用现有即时写事务 |
| D18 | baseline/isolated/遗漏项假checkpoint | ✅ 已完成（定向验证通过） | baseline/isolated/未选假 checkpoint 删；真实 operation checkpoint 保留 |
| D19 | Rebuild模型/版本冻结封口 | ✅ 已完成（定向验证通过） | 换 Profile/版本冻结封口删，原 proposal/source/owner 继续核验 |
| D20 | 检索重复限制、过滤与排名 | ✅ 已完成（定向验证通过） | 字长/目标数/分数门槛/重复过滤排名与 topic_projection 删 |
| D21 | issue #270：人物占位、提及序号错配与身份文案禁令（本次必修） | ✅ 已完成（定向验证通过） | 正文保真实 ID、复用姓名解析，去双序号错配与占位；真实 SQLite 回归通过 |
| D22 | 全仓大幅裁剪测试、重复快照及专属夹具/脚本/CI接线（本次必做） | ✅ 已完成（定向验证通过） | 测试主文件 432→111；旧快照/夹具/基准删，保真实权限和效果检查；本轮再删无消费者 validation budget 和 AST retirement 导出器，验证见 §13 |
| D23 | 旧Memory quality基线/评测维护链及冻结发布门槛退役 | ✅ 已完成（定向验证通过） | synthetic quality/baseline/release-check 与配置退休，audit/hygiene 保留 |
| D24 | CI大幅裁剪：旧job/门槛、重复构建验收、一次性与平行发布链（本次必做） | ✅ 已完成（定向验证通过） | CI 单组源码检查；Release 单镜像/部署包 smoke，旧 bootstrap 与门槛删；workflow 解析、版本/部署包、本地 source-free 启动及重建验证通过 |
| D25 | 来源评分、自动置信度/胜负与分类失败推理退役 | ✅ 已完成（定向验证通过） | 固定来源权重/自动置信度/权威胜负/分类失败 contest 删 |
| D26 | 容量腾位失效、经验过时与容量准入删除 | ✅ 已完成（定向验证通过） | 容量腾位失效、按年龄/分数 stale 和准入删；valid_until 保留 |
| D27 | Rebuild重复重试与整批闭合、单条失败封口 | ✅ 已完成（定向验证通过） | Rebuild 批准子集可提交，未决 REVIEW，原回执恢复且不清累计次数；真实 SQLite 回归通过 |
| D28 | SELF重复episode视图与首条证据捷径 | ✅ 已完成（定向验证通过） | 重复 previous_episode DTO 删，多选证据复用 additional_evidence |
| D29 | Work相同工具结果→无进展挂起整链 | ✅ 已完成（定向验证通过） | WorkNoProgress/hash/repeats/唯一预算查询/暂停映射整链删 |
| D30 | 压缩分类全集、更正截断与必须更小策略 | ✅ 已完成（定向验证通过） | InputDisposition/全集分类/16更正 slice/must-smaller 删；容量来源约束保留 |
| D31 | thinking强制开启、reasoning floor与抬档策略 | ✅ 已完成（定向验证通过） | 三层 thinking floor/自动抬档/指数预算与 UI 封口删；false 不再强改 true |
| D32 | Claude平行付费续跑与正文DSML执行修复退役 | ✅ 已完成（定向验证通过） | Claude 内部付费续跑/usage 合并/固定搜索帽和 DSML 转 ToolCall 删；离线验证通过 |
| D33 | 旧env、版本重复、品牌/型号名词封口 | ✅ 已完成（定向验证通过） | 旧 env 否决/重复版本和 task 名单/品牌与型号封口删；实际协议身份保留 |

验证关注实际结果：配置能生效、有效输出不因经验数字失败、未选事实原样保留、真正来源和权限不变、已提交效果不重复、请求额度对应实际请求。删除专为旧限制存在的测试，调整现有相关用例，不新增全排列边界测试。

历史补链单独记录在[历史证据报告](memory-evidence-acceptance-progress.md)，不再是本次必做项。保留损失的审计可见性，不据此伪造旧数据或宣称已经恢复。

本报告明确区分删除方向与已完成实现；后续本地修改、验证、提交、推送、部署和生产结果分别记录。

## 12. 实施与交付记录

实现分支 `codex/memory-delete-delivery`，基于 main `2ce7a167`。实现与后续 Dream 修复已合并、部署；下面按实际阶段保留验证和交付回执。

0102 只删除四个同 key 的 active 唯一索引，不删事实或历史表。上线前已使用 0101 停写副本演练并核对事实、证据、Work、累计预算和效果回执；仅更新 Bot，SnowLuma 容器、登录与配置未动，最近一份已验证备份保留。

行为变化须准确理解：撤归因/强化会改变派生个性化排序；撤 DSML 兼容后只有真实协议 tool_calls 可执行；撤强制 thinking 只保证配置不被后台覆盖，false 省略启用参数后供应商默认语义仍由实际 Provider 决定。保留未决项不是自动判定已完成。


2026-10-09 实施项 D01–D33 已完成并在索引标注。保留 Python 主测试 111 文件、638 个静态用例、约 32,733 行（原 432 文件、2,706 静态用例、134,012 行）；支持 helper 43 文件/3,796 行。插件测试保留 4 文件，126 项通过；前端保留 3 文件，18 项通过且构建成功。不是以 skip 隐藏旧策略断言；既有 Monty/外部服务环境要求继续准确报告。

源码 Ruff、650 个源文件 Linux 平台 mypy、全迁移 fresh→0102、真实 0101→0102 数据保留、关键 SQL/回执/身份、Provider HTTP 与 Work 恢复验证通过。本地 source-free direct Bot 已启动、health/database 正常，重建后数据库与持久资料保留；未启动 QQ 客户端。下列交付回执补充 Linux 终验、合并与线上实证。

2026-10-09 交付回执：实现 PR [#272](https://github.com/YuanYeYouTao/Yuki/pull/272) 已合并到 main，应用 revision `4f582c066a7a6529f1a503a0fee292ab978a9062`。PR CI 与合并后 main CI 均通过：Ruff、650 源文件 mypy、前端 18 项与生产构建；保留 Python 套件 1081 passed / 49 skipped。49 项为既有可选 Monty worker/binding 集成环境缺失，本次上线 direct 构建、Code Mode 关闭；插件定向 126 项通过。

合并版本 direct 镜像已在本地 source-free 部署：启动健康、schema 0102，重建后数据库和持久资料保留。镜像上传 SHA256 与本地一致；停机前以新镜像解析实际模型配置，并确认 Bot 挂载和 SnowLuma 服务定义不变。模型配置只移除已退役 memory_consolidation/memory_attribution routes 和 gemini_schema_format 等旧 wire_options，不改现行连接地址、密钥或模型选择。

首次上线验证：4f582c06 的 health/database 正常、OneBot 已连接，SnowLuma 容器 ID、启动时间、镜像和 onebot.json 摘要前后完全一致。停写备份与引用文件校验通过，0101→0102 副本演练和生产迁移都核对了 14 个关键表内容摘要；2470 条事实、3050 条证据及原身份/Work/预算/效果回执不变。交付结束后已执行单份保留，旧备份及迁移演练副本已清理。

生产启动暴露并确认了一处真实竞争：Dream 恢复页只核对执行元数据，却借用事实写入的 deferred 读快照，先 SELECT 后 UPDATE。另一 writer 持锁时会直接报 SQLITE_BUSY(5)，不是仅处理 517 的证据快照重备。容器第二次启动恢复健康，仍须修复根因。D17 补充修复改用现有 Database.immediate_session：先取得 writer，再按索引核对最多 128 个 cluster 的原操作回执并更新状态；空页仍只读，不扫描事实/证据/聊天历史，不加入全局锁、超时或额外重试。真实两连接竞争、260 cluster 分页、原累计预算/attempt/调用数、重复恢复和快照耗尽等 15 项既有回归通过；该修复合并与最终部署回执如下。

最终交付：Dream 恢复修复 PR [#273](https://github.com/YuanYeYouTao/Yuki/pull/273) 已合并，应用 revision `31d12022f0ae24e29eab3a5b5bfc9455bfc4b22a`。该 PR 完整 CI 再次通过：1081 passed / 49 skipped、前端 18 项与生产构建、Ruff/mypy；真实 Dream SQL 竞争定向 15 项亦在 Linux 通过。合并版本 direct 镜像完成本地部署和重建持久性验收，上传校验和一致，仅替换生产 Bot。2026-10-09 10:43 UTC 确认首次启动完成、自动重启 0、health/database 正常、OneBot 已连接；schema 0102，SnowLuma 原容器 ID/启动时间/镜像/config 摘要不变。

D01–D33 交付闭环完成。最近的一份已验证升级备份保留于 `/opt/yuki-qqbot/ops/rollout-memory-delete-4f582c06/backup`，全目录核查只有这一份；旧备份、演练副本与本次上传 tar 已清理。备份是首次升级前的一致性 0101 快照，包含完整数据库和 Work 引用文件；最终补丁沿同一 0102 数据格式更新，无需再次迁移。生产数据在应用恢复后继续自然增长，不把启动前的摘要相同误写成线上数据永不变化。

补充现存问题（本次仅核查记录）：语义参与的 CanonicalIdentityError 来自一个已禁用空间仍有旧 semantic owner，调度继续新 hydration，live 配置读取按权限拒绝并报 canonical_owner_disabled。旧 2ce7 备份和本次升级前备份均已有该状态，相关调用链未被本次改动改变；5 个发现 scope 中 4 个启用空间的实际配置读取成功。普通消息、Provider 200 和真实发送回执已有自然流量，但不据此宣称所有语义参与场景验收通过。后续应停止禁用 scope 的新推进并保留原回执对账；权限拒绝、身份数据和旧 run 所有权不应被放宽或重绑。定位：autonomy_repository.list_current_autonomous_scopes → semantic_participation._scene/_advance_scene/_hydrate → runtime_config.snapshot → canonical_owners.require_live_space。

## 13. 残留复审与文档整理（2026-10-09）

本轮在 main `0ddd7eee` 重新对照 D01–D33、源码消费者、当前配置与 CI。确认主干退役策略未恢复；补删无消费者的 `scripts/memory_validation_budget.py` 和固定源码形状的 `scripts/export_pi_codemode_retirement.py`，清理 Memory runtime/maintenance/semantic/observability 的过时说明。冻结迁移、旧数据列及真实回执 reader 是历史数据合同，不按关键词误删。

README 中英、3.9.0 Release/升级、开发约束和现行模块文档已整理。旧正式发布保留对应 tag 基线，3.8.4 恢复为 `0072`；当前开发 head 为 `0102`、Plugin API 3.3，Code Mode 已合主线且默认 direct。过时施工方案删除，独立测量与交付证据按历史基线保留。逐份结果、检查和本轮提交状态见[残留核查报告](../operations/documentation-residual-audit-20261009.md)。

以下是已核实、尚未完成源码修复的运行问题，不能被 D01–D33 原交付结论覆盖：

- **SELF 旧代次阻塞。** 227 个已无当前有效输入的 failed/waiting 批次排在原重试队首，当前代次 20 的 812 条消息因空 retry 范围被整 scope 跳过。临时隔离这些旧批次后，两轮共完成 236 批、33 次写入、2,905 个聊天事件，有效聊天与工具积压清空；原错误、次数、检查点、已提交结果与回执保留。日限额及其他临时设置已恢复。`claim_due` 的失效来源选择和 `_advance_state` 的旧空洞水位仍需修复，临时排空不是永久改码。
- **禁用语义 scope。** 原交付末尾记录的禁用空间 hydration 仍待处理，不能放宽 live 身份校验或重绑旧 owner。
- **Provider 健康字段。** `Settings.llm_configured` 仍从旧环境字段推断，真实模型装配使用 v3 Profile；它不能单独证明显式 Profile 已配置或可用。现行文档已撤不存在的 legacy compatibility 入口，健康字段的实现需另行核对修复。

本轮只清残留与整理说明，没有修改上述恢复/调度/健康行为，也没有新的生产部署或正式 Release。
