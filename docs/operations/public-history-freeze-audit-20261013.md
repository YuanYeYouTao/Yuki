# d75 公共历史冻结为何出现较早分组差异：原请求与实际Actor视图查证

本例有历史冻结。已核的差异来自两个实际Person拥有的历史view各自采用了不同分组表示；本证据没有发现同一actor的已保存83字片段被覆盖成155字。它只回答这两个目标事件与这组自然调用，不宣称所有历史路径都没有其它问题。

只读核对Host `d75a89463c491ad7d3eba4b074c2cbd3b8992050` / container78b / image87d。来源为此前实际付费自然请求原件；没有新增模型调用、QQ消息、重跑、源码或生产修改。8条已完成prepared→Gw→client完整互证的调用只涉及4个实际source主键98790、98794、98798、98803；另1条50067输入的调用缺prepared，来源/actor不按时间或token补猜。

04:27:01UTC用SQLite mode=ro取原chat_events主键、其Person/Conversation FK，及该conv/generation23和两actor实际现存projection/selection。读事务2.585秒，SQL全部完成后立即关闭，哈希、JSON和写外部证据均在事务外，没有持读等待下载或阻碍后续备份。捕获前后仍exact d75、healthy/restart0/OOM=false；私有快照1,292,134B，SHA256 `a0a1db5765269e622dcf6df6f99f03d3f5bccbdb2db102560999049f58c0e0eb`。

98790、98794、98803的原author Person为B，SHA256 `c4b06730c04d0143fe5fc6ccf68c3199ef13fb25ab403aea580d2e1d7e5542dc`；98798与历史98586/98587的原author为A，SHA256 `95b8a33c6db0dde6ffb9c293b5a8d26e801eecb32abe2b08d4e1ee7aba5eda52`。这些关联来自可信内部事件的现存主键/FK，没有从QQ平台ID、昵称或正文重建所有权。六个事件同canonical Conversation，8调用原generation均23。

两view保存的read_scope逐字相同，SHA256 `e3b74275509fa380e8ca6fdce5d10caefc21542696be897496b61bd84fb8c0ed`。pinned d75 `main_agent_turns.py:168–204` 将真实actor_id写入 `main-history-v2` view key，同时context key也含actor。用原事件Person/Conversation及实际保存scope复算原_hash，A/B各唯一匹配保存view，并与实际paid输入中的目标片段逐字核对。原HTTP/ChatRequest没有直接保存view_key或epoch/revision，这一查证是原身份、源码语法、保存view与实际采用内容互证，不能叫跨系统view/receipt-ID直接join。

| 目标已采用片段 | Actor/view | 现存selected.created_at UTC | 字数 | 实际内容SHA256 |
|---|---|---|---:|---|
| [98586] | A / d0185bb8… | 02:43:08.773961 | 83 | 18bbe2c8cbbe6019214cc5a88339d7996fa42d6c785190e6ae7b5cc07a96d172 |
| [98587] | A / d0185bb8… | 02:43:19.078440 | 102 | 78f8cd35ce8983b2f567f6d27835834b3c20c6e089014b4f6218975062052e5a |
| [98586,98587] | B / d7c64f85… | 02:50:49.935193 | 155 | 655ffb24e2a94e155a2dc12f556cdb8c7e5aa2eeaf33f98d6d44309c1c5b87a1 |

这些created_at是现存目标片段的采纳记录，均早于下列实际prepared请求。A/B整个view最早现存selection分别01:08:53.018983、01:36:26.830884，不能把目标事件的02:43/02:50写成整个view第一次冻结，也不能把现存首次记录推广成所有历史删除/重建路径的终身证明。

| Gw完成UTC | Client trace | 原source event | Actor view | 原请求实际采用组 | contents位置 |
|---|---|---|---|---|---|
| 03:54:24.584 | 197794 | 98790 | B | [98586,98587] | 225 |
| 03:54:33.688 | 197808 | 98790 | B | [98586,98587] | 225 |
| 03:54:46.719 | 197829 | 98794 | B | [98586,98587] | 225 |
| 03:54:59.779 | 197843 | 98794 | B | [98586,98587] | 225 |
| 03:56:23.262 | 197859 | 98798 | A | [98586] + [98587] | 225 / 227 |
| 03:58:32.786 | 未知 | 未知 | 未知 | 未知 | 未知 |
| 03:58:42.715 | 197906 | 98803 | B | [98586,98587] | 225 |
| 03:59:50.121 | 197920 | 98803 | B | [98586,98587] | 225 |
| 03:59:58.132 | 197934 | 98803 | B | [98586,98587] | 225 |

8条已互证paid请求中，目标选定片段的原文本各恰好出现一次，所有保存采纳时间均早于实际prepared。B的7条采用同155字组；A的1条采用同83字和102字两个单条，并在contents225与227保留中间既有模型项。因此03:54:59→03:56:23的较早contents225差异有真实actor/view切换证据，不能把它称同一冻结fragment被临时状态重写。之后回到B仍采用同155字组。

强内容核对：A83字第一条 + newline + A102字第二条去掉实际重复的31字display头，逐字等于B155字组，完整SHA为655ffb24…；31是这两份原件中量出的显示头差异，不是产品阈值。双方采用的是同一两份内部事件正文，差异在分组/显示头；没有输出任何用户正文、昵称、平台身份或签名。

现存A/B selection各377/384条，均model_input，0 observation source、0空event IDs；当前projection实际375/376 items与历史selection计数不同。当前snapshot的source_revision均2734，projection分别revision48/3、reasonprotocol_changed、updated_at03:56:25.928894/03:59:54.804760。这些是04:27现态，多数晚于原请求，不被写成当时revision/epoch。另只读取原container在03:54–04:01现有Docker日志86,159B，SHA256 `97ecde9884facba26f099d5793adb5122d9f01ec54a8b69159f99615bd65a2a6`；其中没有prompt_projection_committed记录，原每调用epoch/revision仍未知，没有启用新日志或从时间猜值。

本例展示的是同一Conversation/generation下按actor区分的公共历史表示：单个view保持自己已采用的表示，另一个view首次采用这两个事件时分成了不同组。当前两实际scope相同、样本没有私有observation，但这不自动扩大到其它scope或私有任务笔记的授权。源码的旧动态memory隔离注释、权限/notes消费者与是否删除actor分叉由独立源审查负责，本报告没有改代码。

单条带inlineData的50067样本及现有图片invalidate路径另记；它不能替代上面早于它的03:54:59→03:56:23 actor切换证据。此查证也不能证明31%/94%供应商缓存差异的唯一原因或真实计费。当前资料标题/大括号调查、临时资料排序新版本和后续上线均分开。

外部目录 `D:/codex-test-tmp/yuki-d75-history-freeze-actor-20261013`，完整snapshot与原日志仅在private以及自有remote `/tmp/yuki-d75-freeze-actor-snapshot-4q4bve56`；红删逐调用附录 `per-call-frozen-history-view-crosscheck-redacted.json`、CSV `frozen-history-per-call-redacted.csv`、视图概览 `target-view-summary-redacted.json`。没有重复原152窗口实验或完整生产历史扫描。

## 全量身份链路审查与删除范围

用户要求同一 Conversation 使用统一历史，并删除个人时区配置。审查沿实际入口、身份解析、历史读取、装配、选取保存、淘汰恢复和私有执行接续展开。下表记录已经定位的链路；最终测试、合并、0112迁移与自然流量验收另列，不把搜索覆盖当作验收通过。

| 链路与代码索引 | 已确认问题与处理 |
| --- | --- |
| `services/main_agent_turns.py::compose`、`services/history_projection.py::prepare_history` | 公共 view/context key 包含当前 actor、read_scope 与入口范围；删除这些分叉，只使用实际 Conversation/generation |
| `conversation/projections.py::prepare_commit/selected`、`projection_models.py` | 公共选取又保存、比较个人范围及观察来源；连读写、恢复条件与列一起删除 |
| `conversation/observations.py`、`runtime/work_context_note.py`、`work_control.py` | 私人 note/scope-summary 自动进入公共历史，两个生产者继续重复发布；删除公共读取、付费摘要与发布，原 WorkSession 私有笔记入口已存在 |
| `plugin_host/main_turn.py`、`facades.py`、`ContextAssembler.assemble_plugin_turn` | SDK 用 plugin/actor/context_profile/toolset 分公共视图，且历史 renderer 使用个人当前时区；Person SDK 还从 raw_events 排除 actor.event_id，虽然当前消息其实是 SDK 指令；共同历史入口统一，删除这个过滤及只服务它的参数，未批准历史读取的 SDK 不读写公共缓存 |
| `RuntimeConfigService.snapshot`、`config_specs_hot.py`、Main 公共 `contract_revision` | 群里当前人的窗口/压缩参数与 LLM/web 参数能间接重建公共历史；群 context 参数使用 Space/global，公共请求合同的计算、比较与列退出。私人会话仍使用自己的实际 Person 配置 |
| `MainAgentTurnService.compose/commit_projection` | 带图跳过冻结并销毁整份 view，派发布局拒绝也清原 prefix；删除这些失效副作用，当前图片仍进入实际请求，公共保存原事件文本/附件引用 |
| `prepare_history`、`FrozenFragments.append_current`、公共选取替换 | 重复当前 event 曾清全历史并移除包含它的整组；删除重复事件公共 epoch/整组选取替换，已采用组原字节保留，本次真实触发按 activation 处理 |
| `time/service.py`、`people_repository.py::prompt_metadata`、automation 时间工具及权限目录 | 个人/SELF 时区覆盖进入当前资料、SDK 和排程默认；删除偏好 API、DTO、两表、设置/读取工具及调用者，统一默认 UTC+8，保真实 UTC 和任务明确时区 |
| `ContextAssembler._assemble_actorless_turn/_external_wakeup_message`、Main 的 `append_current` | 外部通知与沙箱完成曾把宿主当前文字挂到原 event ID 冻结，旧 before_event 截断还遗漏较新的公共事件；当前触发与公共原事件分开，公共历史读最新获准事件，真实执行仍保原 source ID |
| `TurnExecution._finalize_initial_layout`、`GeminiProvider._build_payload` | d75 把当前工作放在正文后；a3 已前置，但 Gemini 的相邻 user.parts 合并不补换行。仅新建当前 Host 工作块增加前后双换行，不改旧公共文本或已有付费原生检查点 |

同时核验实际身份模块：`identity/ingress.py` 按真实 Space 建群 Conversation、私人会话按其 Person；`conversation/hydrate.py` 使用原 canonical primary alias。`EventLedgerRepository.read_scope_context/read_scope_delta/read_scope_missing_history` 按实际 Conversation、generation、keeper 和内部事件范围查询，没有按当前人过滤旧聊天。`ChatEventPromptRenderer` 的姓名、AT、引用与角色来自原事件，不能把原作者身份删除为无名正文。`identity/memory_guard.py` 是显式记忆证据权限，退出公共自动笔记后不再影响公共选取。

Work、SELF、定时和插件最终都进入共同 Main。原 `WorkSourceGuard` 中 observation actor/read_scope、paid journal、opaque、签名、工具回执、artifact access 与实际执行 owner 属于已发生的私有事实，保持原来源；独立插件 agent session 仍属于其明确独立会话，不冒充该群公共历史。Provider session/header、request fingerprint 和 wire observer 传递原执行或记录诊断，没有另建公共 reader。公共历史模型/工具合同删除后，私有恢复合同仍沿真实协议边界处理。

审查索引：root 的 identity/Canonical/ledger/renderer/ContextAssembler/config 26文件、1003行匹配，外部 `identity-history-root-index-v2.txt` SHA256 `771cf922751eac9c9fa1986f0f2454d03b078a75ddf9674d2e71bfae873c5b72`；独立 ModelRuntime/LLM/SDK/ToolActor/插件 agent session 审查69文件、54文件匹配2712行，其余15文件也保存源码指纹，`identity-full-rg-index-v2.txt` SHA256 `7498aeea67bf4a324303f821cf15235a7eca435e2386aa7327226035fefb3167`。两份索引有重叠，不能相加成唯一文件数。Work/SourceGuard/SELF/automation 另沿6个实际 Main 入口核对。原索引保存审查当时的源码，最终 delta 复核及结果由任务书 Y08–Y12 记录。

晚期两项来源遗漏要按实际条件说明：已有无 Rollup 的普通 Main 常把 selected_summary_text 保存为空串，不能据 visible event 被过滤就宣称每次调用都重建公共 epoch。可达错误包括新建选取、物理缓存淘汰后恢复时漏源或重渲染同组事件，以及外部旧触发遗漏尚未采用的较新账本。新回归覆盖这些边界；此前 119 项测试没有证明这两条路径已修。
