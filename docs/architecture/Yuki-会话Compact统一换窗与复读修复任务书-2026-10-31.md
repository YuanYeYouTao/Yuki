# Yuki 会话 Compact 统一换窗与复读修复任务书

日期：2026-10-31。状态：用户已授权实施、循环复审、PR／合并及按原规矩 Bot-only 上线；实际进展见[实施记录](../operations/compact-delivery-implementation-20261031.md)。

本轮只解决三件事：**旧历史太大，就复用现有 compact 真正替换旧历史；刚发生的工具结果，先让正常 Main 看见；这一轮的目标不被压缩接续材料带偏。** 直接复用现有公共压缩、执行压缩、投影与恢复链，只修已经确认的自动分流、切点保留和目标说明，不重写压缩算法，不新增选区策略层。

压缩不能把已经回答的问题重新变成待办。它也不能规定发一次消息就强制结束；需要多次发送、继续调用工具或处理新输入的正常工作仍可继续。

## 1 基线与授权

- 源码基线：最新取得的 main `59ad21816d079236a6ecf037db984b95cfc1c978`。本地任务书分支为 `codex/compact-taskbook-20261031`；相关 compact 源码与本次故障取证版本一致。
- 生产取证版本：`8ab2d12106a8099c1ae628dd35bf8c74411078bf`。这是本次只读取证时的镜像来源，不代替实施或部署前的实时核对。
- 设计来源：[原 Issue #418](https://github.com/YuanYeYouTao/Yuki/issues/418)、[原重叠问题 #417](https://github.com/YuanYeYouTao/Yuki/issues/417)，以及本轮已同意的三个目标。
- 本轮用户已明确授权执行本任务书、完成后循环复审至无已知缺口、提 PR 并合并，按原规矩 Bot-only 上线。沿本次授权执行，生产备份后仅更新 Bot，使用自然流量核验，不主动发送 QQ 测试或付费探测；不沿用上一项工具结果交付的上线授权。

本任务书独立于 10-14 的历史实施任务书，不在旧任务书上叠加施工说明。历史任务书和日期验收保留；实施时直接修正现行模块文档与代码的对应关系。

## 2 已确认的故障与边界

数字生命群的 fanout 讨论中，一条普通提问只发生一次 Main 激活，却在同一激活内产生六次新的 send。该轮没有业务 Work；期间共 24 次物理模型请求，其中 7 次是执行压缩，压缩调用合计约 81.69 秒，最后遇到 `WorkBudgetExceeded`。这不是六次自主接纳，也不是原发送操作的重试。

| 证据 | 本次实际情况 |
| --- | --- |
| 初始输入 | 433 条消息，本地估值约 94.2k，低于 96k 软触发线；此估值不等于 Provider usage |
| 首次发送 | 真实 send 返回完整成功回执，7/7 分条及原内部事件 ID 齐全；结果展示没有截断该回执 |
| 自动选区 | 新执行尾使总请求越线，代码因初始输入未越线而选择只整理执行尾 |
| 压缩后请求 | 原 433 条初始材料全部保留，只追加最新私有检查点；原 send 调用和成功回执未进入下一正常 Main 请求 |
| 容量分支 | 本轮 Profile 的 `max_input_tokens`、`context_window_tokens` 均未声明，命中 `input_budget=None` 路径；所选末边界后的尾部为空 |
| 目标污染 | 检查点虽然写了 fanout 已答，却将群史里的另一个后台任务写成当前“原任务”，随后模型查询后台工作并再次生成回答 |

本机原始请求、响应和审计摘要位于工作树 `.cache/fanout-20261031/`，不随任务书入库。共享测试使用同形的脱敏合成数据，不提交真实群聊正文、账号或生产数据库。24、7、6 是这次事件的观测值，不是新的配置或验收次数门槛。

已经确认的是错误选区形成近触发线循环、未观察回执被覆盖，以及当前目标在摘要中被误认。不能据此保证只修改一句摘要提示就消除所有模型重复，也不能把配置一个硬容量当作修复。

## 3 最高设计约束

**禁止防御性编程。能删除或放宽现有条件解决的问题，不新增替代机制。** 遵守 [共同开发约束](development-contract.md)，本轮尤其包括：

1. 不新增 compact 协调器、摘要 Agent、独立后台摘要服务、影子回执表或第二套上下文缓存。
2. 不新增发送次数帽、相似正文去重、参与冷却、压缩冷却、最小压缩率、固定整理轮数或“摘要必须短于某比例”的完成门槛。
3. 不用提高触发值、伪造 Profile 硬容量、关闭自动压缩或 `send` 后强制结束代替修复；现有软触发值与真实硬容量继续分开。
4. 不要求模型另开一轮确认“我看过回执”，不新增汇报、自报或全部子任务成功的收尾条件。
5. 原身份、来源、授权、内部事件 ID、原调用键、预算、未知效果和投递回执按事实保留。文本摘要不能改判效果、接纳任务或授予重发资格。
6. **先写 E2E 场景和失败分支，再改实现；绝不写完代码后补单元测试。** 现有仍有效的隔离回归保留，失效断言随产品合同修改。

## 4 统一的换窗合同

### 4.1 复用现有压缩，只修自动分流

每次 compact 从当前实际 Main 输入出发，复用原 Provider/Profile、模型、固定 system／工具合同、真实消息顺序、来源核验和派发预算。保留现有 `TurnExecution.compact_current`、`_PrimaryDispatch`、会话串行槽位与原持久化 owner，不另起模型路线。

公共历史继续使用 `public_compact_plan` 及原有检查点发布、采用逻辑；私有执行历史继续使用 `execution_compact_plan` 及原 journal／普通轮回执。手动命令保留公共会话范围。无需合并两个函数、重写通用 planner，或计算“哪部分更值得压缩”来证明统一。

自动维护已被完整请求触发时，在现有公共路径合法的前提下优先复用它，删除“完整 initial 自身还必须达到阈值”这层额外分流。本案不能因为公共底座略低于触发线，就绕开已经能替换旧群史的现有逻辑，反复只整理几条执行尾。没有合法公共范围时，仍沿原执行压缩、软触发继续或真实容量失败路径处理，不新增成本比较、优先级或经验占比。

这里只复用既有 fragments 条件：公共检查点本身不计为待覆盖片段，采用后仅保留未覆盖 fragments；仅剩检查点时不会因为总请求越线再次购买公共摘要，不增加另一套判定或状态。

保留 `public_eligible` 现有的公共片段、实际前缀匹配与 `continuation is None` 条件，以及公共 planner 对非 `ChatMessage` 私有尾的原协议边界。公共路径会清空请求的 continuation，不能为了优先公共压缩删掉这些条件、强清原生状态，或添加另一套原生转换路线。

公共规划不可行时仍尝试原执行 planner；两者均不可行才沿原软触发继续／真实硬容量失败处理。优先公共路径不应遮掉本来可行的执行路径，不新增另一套选区策略。

软触发只安排维护，不要求新请求一定低于触发值。没有合法可整理内容或未得到有效检查点时，原请求可容纳就继续；只有超出明确硬容量且不能合法容纳时才报告真实容量失败。未知硬容量继续保持未知。有效性不以候选是否更短或压缩率为条件；已收到的合法检查点与付费事实照实保存，不因摘要仍超过软触发值就反复购买同一份整理。

容量计量与合法切点沿用既有 `RequestTokenCounter` 和尾部索引，保留现有避免重复克隆、编码完整历史的实现；不为了收集切点、身份或保留事实在 writer 中扫描历史。不开新的常驻候选缓存，也不增加按经验耗时判完成的门槛。

### 4.2 复用原有换窗结果，保留未覆盖内容

公共压缩已有“检查点替换覆盖原文”的行为，直接复用；新 Main 输入保留固定 system／工具、有效检查点、未覆盖原文、当前合法运行资料／输入，以及需精确保留的协议与工具结果。公共前缀已经被该路径覆盖后，采用与恢复不能再把旧全文拼回去。

执行压缩只覆盖执行历史时，未被覆盖的 initial 继续保留，这是合理行为，不为表面统一而删除。`initial_messages`、已冻结 layout、公共采用与真实投影提交已有接线，先沿真实场景回归；只有出现与本次分流／切点修正相关的丢失或重复时才作最小修补，不预定重写 `compacted_transcript` 或前缀格式。

退出模型窗口不等于删除原聊天账本、原付费请求、原协议对象、Work journal 或执行回执。公共检查点只覆盖可证明属于公共范围的来源；含任务私有资料、工具结果或运行资料的检查点只归原执行／Work，不因统一换窗写进群公共 projection。可以复用现有两类 owner，不为它们再造两套调度或摘要生命周期。

### 4.3 正常 Main 尚未观察的工具轮保留原事实

辅助 compact 请求看过结果，不算正常 Main 已观察。选切点时，先保护下一正常请求仍需看到的完整工具轮，不能先把切点选到它之后，再只在剩余 suffix 里寻找回执。原回执经过现有 rebase／原生协议转换成为 `work_unobserved_tool_round` 等 portable 证据后，这个要求仍然成立，不能只查最后一组 assistant／tool 消息。

该规则同时覆盖硬容量未知、已声明单项上限及两项上限共同作用的路线，不只修 `counter is None` 一个表面分支。最近未观察结果的成功、失败、未执行、partial／unknown、原参数、原调用键、operation／event／run ID 及其关联必须真实接续。沿既有完整协议配对或合法的 `tool_result_evidence` 表达，不重跑工具、重建 ID 或伪造结果。

观察事实沿现有正常请求／response observation、原 paired journal 和受信的 portable 工具证据判断，不依赖可丢的诊断 trace、模型自述或任意 user 正文中的 `kind` 标签。compact 自己的 response／paired 不把原结果变成“正常 Main 已观察”，不建立另一张“已看过”状态表。

原生签名、opaque continuation、未结清调用及尚未关闭的组合执行按原协议边界处理；不能为了整理转成普通文字。若当前没有合法切点，软触发继续可容纳的原请求，硬容量不足保真实失败。正常 Main 实际收到原结果以后，后续合法整理可以覆盖该轮。

### 4.4 摘要目标由当前可信来源确定

- 普通问答认本次激活的原输入、内部 source event 及已经明确接纳的补充。没有业务 Work，就明确没有当前业务 Work，不从群史推导一个。
- 已接纳 Work 认其原 ID、目标、来源与追加输入；SELF／安排等允许没有真人事件，不伪造用户消息。
- 可见工作目录、引用消息、近期工作和 `participation.thread` 只是各自的事实，不转移当前任务所有权。查询另一个任务不表示接管或恢复它。

复用现有 runtime、source、control 和类型化初始布局表达这些边界。只调整执行摘要及换窗后正常 Main 的接续说明，去掉让无 Work 问答从群史猜原目标的无条件“接续原任务”。公共压缩仍只使用原公共指令和获准公共历史，不能为了锚定本轮目标，把 Work goal、运行资料或私有工具结果加进公共请求再发布。

摘要可以记录已确认进展，不能覆盖程序持有的当前输入和任务归属，也不解析模型文字来新建完成状态或切换 Work。不新增目标解析器、任务接管或摘要检查模型。

问答完成且没有继续事项时，沿现有 final／空正文／`NO_REPLY` 结束。需要继续工作或有新输入时继续原循环；不规定每次 send 都结束，也不增加一轮确认或 engage 填写门槛。

### 4.5 热替换、发布与冷恢复使用同一个覆盖事实

普通轮 `CompactReceipt` 与 Work journal 继续沿现有 response → paired 保存和发布，不重做持久生命周期。公共采用、覆盖边界、必要尾和原 owner 已有表示，优先复用。

用真实热运行与冷恢复场景核验普通公共恢复的独立采用分支、非公共恢复的 `old_count=len(saved.prefix)`、Work 的 `prepare_context_compaction/finish_context_compaction`、业务 rebase 及新输入发布。`old_count` 不是本次已经证明的缺陷，不能预定删除或改格式；发现与新接线有关的丢失／重复再修。重启后不得重新附回已覆盖全文、切掉检查点／未观察回执，或带回旧运行资料。

已有 `response`、发布未完成时复用原响应重备发布，不再次购买；只有 `dispatched` 而无响应时保未知已付，不盲发。旧已付请求、响应和已确认回执仍按原身份恢复，不改写其原 prompt 或充值预算。是否需要持久形状或合同版本调整，以实际改动为依据，不为本轮预建新表、空迁移或兼容执行别名。

若更晚的公共检查点已经发布，旧付费响应恢复不得回退覆盖点或覆盖后来发布的 owner；复用 `current_public_compact` 等既有采用逻辑和实际发布事实，不新增“最新摘要”影子状态。

压缩期间新消息、已选择补充和 Work 输入沿原入口、原顺序接续，不重复接纳。发布继续核原 generation、来源 revision、privacy、实际 owner 和原租约；所有外部模型／文件工作及历史准备在 writer 外完成。来源失效不能用今天的资料重新认证旧响应，隐私清退不能被冷恢复带回。

## 5 实施任务

以下全部待实施。每项修改同时处理真实消费者、旧断言和现行说明，不把配套留到最后。

| 编号 | 工作 | 主要位置与完成条件 |
| --- | --- | --- |
| T01 | 先建立故障同形 E2E | 真实普通 Main／发送／SQLite／投影链；公共输入略低触发线、成功回执使总输入越线，记录下一正常请求，先复现问题 |
| T02 | 复用现有压缩并修自动分流 | `turn_execution.py::prepare_request/compact_current`；删 initial 额外阈值，在原 `public_eligible`／原生边界内接回公共 planner；不新建成本选择器或重写 planner |
| T03 | 保护尚未观察的工具轮 | 候选切点、`_TailIndex/_native_tail_items` 与 `turn_transcript.py::tool_result_evidence` 一起核；未知／已知容量及普通／Work 路径都保原事实 |
| T04 | 核验现有换窗与前缀采用 | 回归 `compacted_transcript`、`initial_messages`、layout、公共采用／提交；已覆盖正文退出，未覆盖内容保留；只修实际接线缺陷 |
| T05 | 锚定真实本轮目标 | compact 指令、后续材料、原输入与 Work 上下文；普通 fanout 问答不被目录里的其他任务接管，既有真实 Work 目标及补充继续有效 |
| T06 | 核验普通与 Work 冷恢复 | 复用 `CompactReceipt` 与 `WorkSession`；回归原采用、截取和 rebase；只有丢失／重复与本次修复相关时才补接线，原 paid／unknown／预算保持 |
| T07 | 保留新输入、授权与协议边界 | 原 ordinary publisher、Work 输入、source guard、公共投影 CAS；覆盖并发追加、删除、privacy、generation、未决原生协议及发布冲突，不另建唤醒或恢复系统 |
| T08 | 改掉冻结旧行为的测试与夹具 | 按第 8 节逐项处理；不删除原效果、真实失败、原 ID、幂等、已付恢复与权限覆盖，不以手造 runtime 代替新增 E2E |
| T09 | 输出可复现产物并接现有 CI | 第 7 节 E2E 在实际收集入口执行；请求、候选、摘要、正常后续输入和回执实际落盘，失败也保留已有事实，加入原 always 上传 |
| T10 | 更新直接受影响的文档和提示 | 修正自动分流、未观察保留及执行接续说明；公共指令、合法 E 未覆盖 initial、原恢复合同仍有效，不机械删除或重写 |
| T11 | 交叉复审与交付核对 | 对照三个用户目标逐项复核，补齐新发现的真实缺口；源码、场景、静态检查、CI、提交、合并、镜像、生产及自然流量分开记录 |

## 6 删除与改写清单

| 现有逻辑或说明 | 处理 |
| --- | --- |
| 仅 initial 达到阈值才优先公共整理 | 删除该分流依据，不换成另一个经验比例 |
| 公共前缀已覆盖后仍可能沿旧 initial／layout 拼接 | 通过热／冷回归核验原采用逻辑，若有问题仅修接线；E 未覆盖的 initial 继续保留 |
| 先选最后配对边界、只保边界后 suffix | 改写选区／保留顺序，未观察工具轮不能被切点覆盖掉 |
| 容量未知或已知时漏保未观察工具轮 | 在原切点与保留逻辑上修，覆盖两类容量，不要求合并成新 planner |
| 普通无 Work 的泛化“原任务／接续原任务” | 只修执行摘要与正常接续的目标说明；公共指令不注入私有目标 |
| 恢复截取可能受接线修正影响 | 回归现有 P／非 P 恢复，证明有丢失／重复才改，不预建新持久形状 |
| 断言将已覆盖的旧全文固定保留 | 只改与实际覆盖冲突的断言；未覆盖 initial、原模型／ID／预算等断言保留 |
| 关于自动分流与未观察回执的旧说明 | 按实际修复直接改；不为文案上的统一删掉合法两类保存范围 |

保留现有公共与执行 planner、`RequestTokenCounter`、投影发布／采用、私有 journal 和会话派发。删掉草案中按各部分成本择优、统一重写 planner／窗口构造的要求；不以函数数量或所有路径改成同一种数据形状作为验收。

不要借此删除账本、Memory、Work tree、原 journal／协议对象、原调用键、真实失败或未知发送事实；不扩大为 JEV／参与算法、工具改名、提示词增量缓存或 Provider 路由重构。

## 7 先写清失败分支，再实施 E2E

优先沿实际 Main 装配、Provider 序列化边界、发送服务、SQLite、公共投影和 journal 验证。LLM 传输可脚本化；脚本根据实际输入中是否保留本轮目标和原回执决定下一步，不按固定“第六次才结束”序号掩盖输入错误。成功结束不以摘要措辞或模型口头自证为依据。

| 场景 | 输入与失败分支 | 必须核对的结束产物 |
| --- | --- | --- |
| E01 大公共历史与短问答 | 公共略低触发线，真实 send 后越线；硬容量未知与已知分别覆盖 | 被覆盖旧正文退出下一正常请求；原问题与 send 成功回执仍在；不因不可变大底座反复购买小尾摘要；正常 final 可结束 |
| E02 本轮目标与背景任务 | 普通问题旁有另一个真实后台 Work；注入错误声称应接续旧 Work 的摘要 | 原普通激活、source／actor／Work 归属不变；下一正常请求仍明确本轮目标和已发生事实，不隐式 accept／恢复背景工作 |
| E03 未观察工具结果 | 单工具、同批多工具、分条成功、部分成功／确认丢失、失败、未执行；回执已转 portable 证据且正常 Main 尚未派发；随后 compact | 正常后续 Main 中可查原 call／operation／event ID、参数与各项真实结果；不只认 assistant／tool 角色，工具只执行原调用，unknown 不自动重发 |
| E04 正常多步工作 | 一次 send 后还需读取资料、处理补充、第二次合法发送或等待外部任务 | 继续原工作，原预算累计；证明修复没有把 send 变成强制结束，也没有新增确认轮 |
| E05 热换窗与冷恢复 | 普通、已接纳 Work、真实 child worker；response 已存后发布失败、paired 后重开；恢复后原结果为 portable 证据但正常 Main 尚未派发；更晚公共检查点先发布 | 原热／冷采用与截取按实际覆盖工作；不重购、不回带已覆盖全文、不丢 portable 原回执、不回退更晚公共覆盖点，原 owner 与父子预算一致 |
| E06 新输入与状态变化 | compact 期间追加消息／已选补充／Work 输入；来源修订、privacy 删除、generation 或权限变化 | 输入原顺序与原接纳身份；旧产物不得凭新来源发布，被删除内容不经恢复复活 |
| E07 原生协议与真实未知 | Chat Completions、既有 Gemini／Responses／Anthropic 适配边界；复用文本 call ID、签名／opaque、pending／pause、仅 dispatched 无 response | 原协议配对和真实调用身份完整，合法切点与 unknown 路径可复现，不把 opaque 改成自由文字或补发已付请求 |
| E08 无合法切点与无效摘要 | 只有大当前输入／固定声明；空、不完整或含工具调用的 compact 响应；未得到有效检查点；合法摘要仍长于软触发值或旧文本 | 可容纳请求继续；明确硬容量不足保真实失败；不执行摘要里的工具，不伪造成功，不按更短或压缩率否定合法检查点，不重购同一已知无效候选 |
| E09 复用原公共与执行路线 | 同一会话走手动／自动维护，带真实私有目标／工具尾，以及存在 continuation 的对照 | 合法公共压缩直接复用；不为优先公共路径强清 continuation 或绕前缀核验；公共请求不含私有目标，原执行／容量路径仍工作 |

这些是任务场景，不要求每个维度穷举笛卡尔积，也不新增固定总用例数。实现若影响 Code composition 消费，应沿其真实原回执补必要场景；本轮不能把 Provider 原生协议等同于 `native_worker` VM，也不为无关 VM 链增加强制新测试。

每个选定场景在 `test-results/compact/<场景标识>/attempt-*/` 保存：复现输入与设置、源码版本、首次／compact／下一正常请求、摘要原响应、实际覆盖与保留区、原调用及发送回执、owner／预算，以及恢复前后投影／journal 所需事实。必要原件保存文件和摘要，足以逐字或逐 ID 比对。

请求和回执到达时即记录，清理阶段沿 `finally` 落盘；失败也保留已取得事实，重跑使用独立 attempt。不得只在所有断言成功后写 evidence，也不把 stdout、JUnit 数字或测试自己调用底层 reader 得到的内容冒充模型实际观察。

## 8 旧测试与 CI 连带处理

| 当前入口／断言 | 必须处理的缺口 | 保留的有效覆盖 |
| --- | --- | --- |
| `test_compact_trigger.py` 的 ordinary/work/worker 参数场景 | 当前主要覆盖 initial 本身超阈值；补真实 E01 的“略低阈值＋send 回执越线＋后续 Main”，不把手造 runtime 参数化称完整入口 E2E | 原共享触发配置、软硬分离、无合法来源仍可继续和预算事实 |
| `test_current_context_compaction.py` 最新工具轮场景 | 目前只核工具对进入摘要请求；必须核下一正常 Main 实际收到原结果 | 配对完整性、真实原 ID 和原模型合同 |
| `test_work_current_compact_recovery.py` 的整个 initial 原样保留 | 区分 P 已覆盖与 E 未覆盖范围；补大公共历史的真实 P 恢复场景，只改与覆盖事实冲突的断言 | 原 E 未覆盖 initial、当前目标、原付费响应、Work 与不重发 |
| `test_worker_shared_compact.py`、`test_work_compaction_public_delta.py` 的摘要输入断言 | 现有 E 的实际协议前缀断言仍可成立；仅新增公共接线／portable 尾场景，真实选区受修正影响时再改，不机械放宽 | 同模型／工具、原 worker／根预算、真实新公共事件与合法 E 行为 |
| `test_compact_checkpoint_regressions.py` | 已知预算的协议对照不能证明未知预算路径；补 E07 的 None 与下一正常请求 | 已有原生签名、portable 配对与真正失败分支 |
| `tests/support/current_compact.py` 固定 128000；`work_compaction_capacity_helpers.py` 直接造 runtime | 保留用于原隔离回归；新增 E2E 不沿这些假定证明真实发送／未知容量 | 原 owner、请求计费与恢复夹具的有效用途 |
| `test_compact_cold_public_ingress.py`、`test_manual_compact_authority.py`、`test_work_invalid_compact_receipt.py`、`test_runtime_recovery.py` | 对照新边界补必要断言，核 response 重备、unknown、隐私与手动权限未受损 | 真正幂等、已付不重购、来源拒绝；相关局部执行次数断言不得一并删除 |
| `.github/workflows/quality.yml` 的 direct tests matrix | 当前上传 XML、tool-result-delivery、connector-address；须把本轮 `test-results/compact/` 加入原 always 上传，普通 E2E 放进实际采集的 integration 入口 | 原 unit/integration 与 `not native_worker` 分工，不增新 workflow 或全局有 skip 即失败的门槛 |
| 同 workflow 的 native job | 已上传整个 `test-results/`；只在本轮实际选用 VM 场景时接入其真实执行与证据 | 原 pinned worker／隔离验证保持，假引擎与 collect-only 不算真实 native 通过 |

新功能优先用 E2E；不在实现后补一套镜像 helper 的新单元测试。已有计费、原调用不重放等局部次数断言是事实，应保留；不把它们扩成全局固定模型调用数。验证匹配最终改动风险，静态检查和受影响回归通过后，不反复跑无关全量套件。

## 9 实施顺序与交付记录

1. 先将 E01／E02／E03 的真实链路、失败分支和 artifact 输出落好，取得旧行为的可复现证据；需要隔离的部分先写清全部可能失败情况。
2. 在原 compact 上修 T02／T03／T05 的分流、未观察尾和执行目标；T04／T06／T07 沿真实回归核现有换窗、两类恢复及边界，只补受影响接线，不重写已正确的 planner／持久化。不能热路径先上线、冷恢复以后再补。
3. 同时落实旧断言、CI 上传及现行文档，按场景核请求、原回执、覆盖与恢复事实，进行跨模块复审。
4. 获准执行交付后，分别记录本地改动、实际验证、提交、PR／确切 head CI、合并、镜像及生产状态。生产只按后续明确授权的范围操作；不主动发 QQ 测试或付费探测。

验收按三个结果收束：**旧材料真的退出新请求；刚发生的结果不因 compact 丢掉；这一轮的目标不被背景任务替换。** 摘要文本不保证模型永不重复，但程序不能再制造本次已经证明的输入缺陷，也不能用限发规则掩盖它。

## 10 源码与现行合同入口

- 选区、规划与重建：[context_compaction.py](../../src/yuki/services/context_compaction.py)、[turn_execution.py](../../src/yuki/services/turn_execution.py)、[turn_transcript.py](../../src/yuki/services/turn_transcript.py)。
- 当前目标、前缀采用与持久恢复：[main_agent_turns.py](../../src/yuki/services/main_agent_turns.py)、[work_control.py](../../src/yuki/runtime/work_control.py)、[work_session.py](../../src/yuki/runtime/work_session.py)、[work_journal.py](../../src/yuki/runtime/work_journal.py)、[projections.py](../../src/yuki/conversation/projections.py)。
- 容量与派发：[capacity.py](../../src/yuki/model_runtime/capacity.py)、[dispatch_guard.py](../../src/yuki/model_runtime/dispatch_guard.py)、[executor.py](../../src/yuki/model_runtime/executor.py)。
- 现行合同：[开发约束](development-contract.md)、[会话 Compact](conversation-compact.md)、[主 Agent](main-agent-runtime.md)、[持久工作者](persistent-subagents.md)、[Provider 输出](provider-output-boundary.md)。
- 历史设计：[417/418 实施任务书](Yuki-417-418统一Compact实施任务书.md)、[Work 摘要退役与任务树审查](Yuki-Work摘要退役与会话Compact及任务树设计审查报告-2026-10-14.md)。历史方案不冻结本次已明确放宽或删除的条件。
