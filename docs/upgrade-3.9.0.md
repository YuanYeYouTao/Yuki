# Yuki 3.9.0 配置与升级草案（未发布）

<!-- release-baseline: version=3.9.0 schema=0118 -->

本指南按 main `abf25c1e924b6ba7c2087d3dc9039bf87801066f`（2026-10-14）核对当前开发源码与随包迁移。**3.9.0 尚未正式发布**，正式下载仍为 [3.8.4 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4)。源码版本号不代表已有 `:3.9.0` 正式镜像；完整合并范围、后续修改及各自交付状态统一见[发布说明](releases/v3.9.0.md)，本页不另维护 PR 数量或旧截止。

默认构建与发行使用 direct：同一 Agent loop 提供固定完整工具声明，不包含 Monty binding、原生 worker 或 launcher，`CODE_MODE_ENABLED=false`。Code Mode 已合入主线，是显式 `--target codemode` 构建与启用的可选能力；持久子 Agent 与这个可选原生组件是不同模块。切换模式保留原 Work、composition、预算和回执，不能取得重派发资格。

3.9.0 的现有 WebUI 定位为**临时管理界面**，仍使用当前控制面、认证与业务入口。下一个大版本将进行彻底的多客户端重构；这是未来计划，当前 WebUI 尚未退役，也没有要求现有部署现在迁移到新客户端。当前实现见[WebUI 合同](architecture/webui-console.md)。

后续 NapCat 退役已通过 [PR #276](https://github.com/YuanYeYouTao/Yuki/pull/276) 合并，并按开发提交部署；另见[任务书与交付状态](architecture/Yuki-NapCat删除与既有网关抽象保留任务书-2026-10-09.md)及[上线回执](operations/napcat-retirement-20261009.md)。下述旧部署处理适用于包含该修改的源码；3.9.0仍未正式发布。

## 版本与迁移范围

| 项目 | 3.8.4 正式包 | 当前 3.9.0 源码 |
| --- | --- | --- |
| 数据库 head | `0072` | 当前开发单一 head `0118` |
| Plugin API | 3.0 | 3.5，需适配并重新批准 |
| 管理 WebUI | 不包含 | 临时管理界面，默认关闭，共用控制面；多客户端重构属于下一大版本 |
| MCP / Genie 输出 | 旧实现 / 可选组件 | 专属入口、配置和服务退役；共享回执、媒体与 ASR 保留 |
| 长期记忆 | 旧策略 | fact ID 为身份，同 key 独立事实可共存 |

开发部署从**实际数据库 revision 与生产者 schema**继续迁移，不按应用版本猜测。3.8.3 正式 head 为 `0061`，先满足 [3.8.4 升级指南](upgrade-3.8.4.md) 的停写与媒体会话切换前提。已经完成 `0072` 的库不再重复离线 generation 重置；不用 `stamp` 跳过迁移。

| 当前随包迁移 | 内容与数据边界 |
| --- | --- |
| `0073`—`0078` | 控制状态/回执、执行与发送关联、Work 和模型用量查询索引 |
| `0079`—`0085` | 物理请求计数、发送计划份数、Claude 缓存写入、来源 revision、媒体与读取索引；未知历史不回填 |
| `0086`—`0089` | 工具效果、原正文、可配置累计预算、私有协议对象和有序观察来源 |
| `0090`—`0095` | Memory/协议维护、普通消息接纳、证据压缩及来源查询索引 |
| `0096` | 保存原 invocation 索引 |
| `0097` | 核原 Speech 表形状和依赖，清退配置键及四张专属表；真实语音事实和引用文件先冷备，未终态生成须先对账 |
| `0098` | 汇合历史主线与实验谱系的 MCP 派生表退役，保留 invocation 索引 |
| `0099` | 冻结摘要 kind/renderer |
| `0100` | 关联插件后台 Job 与原 Work |
| `0101` | 给旧效果回执补规范 outcome，不改原 result |
| `0102` | 删除四个 active/key 唯一索引，保留全部事实和回执 |
| `0103` | 退役旧好感/信任评分三表及索引、配置override，清理自动化专属字段并重算脚本hash |
| `0104` | 删除检查点配额表/触发器、投递死字段和主动轮唯一占用，更新canonical Work查询及取消状态；保留原媒体、Work及Memory |
| `0105` | 父子关系迁入 Work.parent_work_id，删除旧root_id、产出分类字段、汇报水位及未派发暂停通报；保留原ID、来源、预算、未知效果与journal |
| `0106` | 新自动化Run引用原version唯一ID，version保存原委托与canonical目标；删除旧迟到字段，保留原Run、cursor、累计预算和效果回执 |
| `0107` | 清理退役的记忆引用人数配置，索引本次名字、等待与到期扫描；保留原历史、来源、执行和回执 |
| `0108` | 原投影增加可丢的最后可见资料缓存；清退公共选取中的旧自动资料包装及无实际引用的自动来源，沿新 epoch 重建输入；有用笔记、聊天与真实 Work/协议/产物引用保留 |
| `0109` | 退出普通运行资料首份/差量公共冻结，删除对应缓存、公共选取与不再使用的自动来源；保留有用笔记及原 Work、效果和已付协议依赖 |
| `0110` | 定时定义转为共同 Main 目标；原版本/hash、到期 run、批准、Work、预算和回执不重建。无法直接表达为目标的旧 DSL 保留原件并停用未来安排，需显式编辑目标 |
| `0111` | 删除 Memory 回执只接受 event/initiative 的封闭来源检查；无真人事件的 SELF 定时执行沿原 occurrence、版本与 execution 记账，事实、证据、回执及原 Work 不重建 |
| `0112` | 一个 Conversation 共用公共原事件历史；清退旧 projections/selections，删除公共个人范围、自动观察来源与请求合同列，并退役个人/SELF时区表；原 Work、版本/hash、ContextObservation/DAG、协议和 artifact 引用保全 |
| `0113` | 删除 generation/reset 时清除原 ContextObservation/DAG 的旧触发器；清公共历史和取消旧 Work 继续沿既有语义，原来源、私有回执与文件不删 |
| `0114` | 退役管理员聊天工具；旧群自主参与停用标记转入 `conversation.autonomous_enabled=false` 群配置并重置旧列，原覆写历史、审计和工具回执保留 |
| `0115` | 删除 SELF state 未使用的回复/工具/价值信号与旧策略诊断列；原 owner、水位、run、请求和 mutation 回执保留 |
| `0116` | 旧 detached sandbox continuation 的 claimed 状态一次性改为 blocked；保留原 request/run、次数和执行回执，启动不再重复清理 |
| `0117` | 删除无人读取的通用 OneBot 诊断表 agent_actions；原工具授权、Work effect 与发送执行回执保留 |
| `0118` | 为 waiting 且 run_id 已知的 sandbox run 建立部分索引；取消仍按原 run ID 对账，完成回执不改写 |

历史主线的 `0096` 曾删除 MCP 派生表；当前同号保存调用索引，`0098` 按实际形状汇合。编号相同不能证明 schema 相同。其他旧实验数据库需先核生产者与完整形状，不能强行 stamp 或重建数据绕过冲突。

迁移不重建原任务，不重置累计预算、不回填未知用量或重放效果。DDL 和索引成本在停写副本演练，回退使用能读取现行 schema 的兼容代码或向前修补。

源码运行要求 Python 3.12，安装时使用随包锁文件及其固定的 Yuki-Semantic-Participation 依赖。项目当前声明的许可证为 [AGPL-3.0-only](../LICENSE)；第三方组件与素材保留各自许可，见[第三方声明](../THIRD_PARTY_NOTICES.md)。Pi 是设计参考，不是安装依赖；Monty 随可选 Code Mode 构建提供，来源与许可见[对应说明](architecture/pi-port-provenance.md)。

## 升级前整理配置

保留实际 Compose 项目名、全部覆盖文件、挂载、模型连接和插件目录。模板只作对照，不覆盖现有部署。

- **模型文件**：使用 `webui-config/model_profiles.toml` 及同目录密钥 sidecar；旧路径按[迁移说明](operations/model-profile-path-migration.md)处理。保留有效连接、模型、密钥引用与 routes，仅移除退役的 `automation_text_generation`、`memory_consolidation`、`memory_attribution`、`relationship_evaluation` 路由和 `gemini_schema_format` wire option。使用目标版本 parser 校验，不用模板重建整份配置。现行 `memory_extraction`、`memory_self_reflection`、`memory_dream` 可独立绑定；Rebuild 复用提取，Embedding 独立配置。
- **结构化输出与思考**：任务沿 Profile 的 `function_tool`、`text_json` 或 `json_schema`；Gemini JSON Schema 使用 `responseJsonSchema`。旧 `responseSchema` projector 与降级开关退役。思考沿显式设置，不再强制开启或抬档；正文 DSML 不再转工具，Claude 适配器不隐式追加付费续跑。核对端点实际协议响应，不能靠配置名证明可用。
- **Memory 策略**：删除仅服务自动注入、归因/强化/意图评分、治理、容量腾位与经验过时的旧设置，对照当前 `.env.example`。`valid_until`、用户显式变更和真实权限仍有效。同 key 不是唯一槽位，管理修改和删除沿 fact ID；不要把多条旧事实压成一个值。
- **本轮资料配置**：撤去 `CONTEXT_METADATA_BUDGET_RATIO`、`PLUGIN_MAX_PROMPT_FRAGMENT_CHARACTERS`、`PLUGIN_MAX_PROMPT_CHARACTERS_PER_PLUGIN` 和 `PLUGIN_MAX_TOTAL_PROMPT_CHARACTERS`；0108 清退三项插件字符配置对应的 override，不以其他字符阈值替代。完整模型请求仍沿原容量检查。
- **参与配置**：当前新安装的 `conversation.auto_reply_enabled`、`conversation.proactive_wakeup_enabled`、`conversation.autonomous_enabled` 默认均为 true。旧 3.8.4 的默认关闭说明不适用于这份源码；已有显式环境设置和群级覆写继续生效，升级不把原停用群改成启用。`0114` 将旧停用状态迁入同一配置入口。
- **联网**：新安装未设联网开关时默认 native，显式 disabled 与既有搜索连接保持有效。原生搜索、Gemini 搜索桥及外部后端按[Provider 合同](architecture/model-providers.md)配置；默认开启不能补出模型缺少的能力。
- **持久环境 / WebUI**：保留原 Manager、共享 volume 和执行回执；普通包不会安装新 Manager 或 gVisor。WebUI 默认关闭，认证与管理授权见[WebUI 合同](architecture/webui-console.md)。

### 旧 NapCat 部署配置

Yuki 不再支持 NapCat，当前附带网关为 SnowLuma。原 Gateway 抽象、Catalog、Registry 与 OneBot
能力保留；这项退役不需要品牌数据迁移，不把历史 `ingress_provider=napcat` 改成 `snowluma`，
也不删除 Person、Presence、Conversation、Memory、路由、Work 或原回执。

安装器不替换已有的 managed Compose 文件，`.env` 合并也保留未知字段。因此仅更新模板或
重跑安装器不会清除旧部署的执行配置；向导不停止、启动服务，也不执行自动切换。

在实际部署目录中，保留项目名与完整 Compose 覆盖链，备份配置后用既有编辑方式逐项处理：

1. 从实际 Compose 链撤去 `napcat` service、专属镜像/构建及其端口、Bot 的 NapCat 专属环境项和挂载；共享 social transfer、SnowLuma 挂载和其他服务继续保留。
2. 从实际 `.env` 撤去 `NAPCAT_*`，只从 `COMPOSE_PROFILES` 删除 `napcat`。例如 `napcat,external` 保留为 `external`，不能清空其他扩展 profile；未选择附带网关时不隐式启用 NapCat。
3. 核对目录后清理旧 `data/setup/gateway-action.json` 无消费者标记，保留其他 setup 状态与凭据。该文件不是可执行的切换任务，重新运行向导不会按它停服或恢复。
4. 沿全部 Compose 参数运行 `docker compose config --quiet` 并检查实际解析结果；确认没有旧 service、profile、专属环境项或挂载，再按获准部署步骤应用。配置清退不等于旧容器已停止；仍运行的旧实例须单独核对账号、连接与停用范围。

新包不创建或包含旧 `napcat-data`、`napcat-config`、`napcat-plugins`。这些存量目录可能含 QQ HOME、
token、用户配置与插件，不能默认删除；Git/镜像 ignore 暂时保留仅为保护遗留私密资料，不表示运行支持。
不清空 QQ 登录目录、备份或用户插件，不重启 SnowLuma 来清理旧配置。若需要删除实际存量资料，另行核对目标、引用与授权。

### MCP 与 Genie 输出清退

从实际 Compose 链撤去 MCP 专属配置/服务和 `.mcp.json` 挂载，以及 Genie Worker、speech profile、socket volume、Bot speech 挂载和退役环境设置。保留 ASR/Qwen 凭据、共享媒体、工具正文、用户自有音频及原恢复文件。

核对实际 `CONTROL_OPERATORS_FILE`，撤去 `control.mcp.read`、`control.mcp.mutate`、`mcp.web_search`、`control.speech.read`、`control.speech.mutate` 等退役 capabilities。保留原 principal/person、角色、token 引用和有效授权；禁用 operator 也由 parser 校验，不能靠停用绕过退役字段。

在执行 `0097` 前，按原 Work/operation/plugin-execution ID 对账旧语音生成和发送：已确认不重发，未知保留围栏，确定未派发的退役 AUDIO 明确拒绝，不自动转文字。混合计划按原身份对账，旧冻结计划不在线续派；离线导入只补账或记录未派发事实。兼容读取不能改计划、hash、effect key 或预算。

停写冷备需包含 `speech_generations` 真实执行事实、通用回执和被引用 WAV，保留原路径、内部 ID 与校验和。专属表终态不等于 QQ 已送达。服务器文件按实际归属和引用清理，不直接删除整个 data、共享 artifact 或有效备份。范围见[Work 证据备份](operations/work-evidence-backup.md)。

### Plugin API 3.5

当前 Host **只接受精确的 API 3.5**。旧插件先完成 [API 3.1 的 MCP 退役](plugin-development/api-3.1-migration.md)、[API 3.2 的语音输出退役](plugin-development/api-3.2-migration.md)、[API 3.3 的共同 Main 入口](plugin-development/api-3.3-migration.md)和 [API 3.4 的关系接口退役](plugin-development/api-3.4-migration.md)，再按 [API 3.5](plugin-development/api-3.5-migration.md)移除从未发布的事件订阅及未实现的 `runtime.config.read/write` 权限，最终声明 `plugin_api = "3.5"`。插件自身配置仍用 `plugin.config.read/write`，当前事件以 [Event Catalog](plugin-development/api-reference/events.md)为准。

实际代码与 manifest 变更后，沿现有发现、审阅和批准流程核对新的精确合同，保持原启用意图；旧批准不自动授权新 API。历史批准读取只忽略 3.5 明确退役的两个权限，不扩大其他授权。宿主挂载的插件代码不会随镜像自动更新，不能只改版本号而继续使用已删除的 Facade。

[PR #412](https://github.com/YuanYeYouTao/Yuki/pull/412)要求 `ctx.automation.create_task(task, request_id="...")` 提供明确、稳定的操作 ID，不再用整轮调用 ID 合并创建。同一调用创建不同任务使用不同 ID；同一插件与来源下，相同 ID 和参数的重试返回原任务与创建回执，重用 ID 改参数则报冲突。

本轮资料删减同时移除 `limits.prompt_characters`、`PromptFragment.max_characters/cache_key`、
无人消费的 `PromptTarget.PLUGIN_SESSION`，以及恒空的 `get_current_observation` 与
`vision.current.read`。插件仍可显式调用 `analyze_current_media`，私有 agent_sessions 仍可
使用原会话合同。资料片段保持结构化身份与正文，不再叠加 fragment/插件/总字符门槛；
私有会话历史和结果也不再按固定字符数截取。完整请求沿原容量检查，初始超容量不承诺
自动私有 Rollup。manifest 真正变更时沿原批准流程处理，不增加新的批准门槛。

## 迁移与启动

1. 固定目标提交和镜像，核随包 head、配置与插件版本；正式发行时再核 `v3.9.0` 资产。
2. 在独立数据库和文件副本演练迁移及启动，核原身份、Work、预算、协议和发送回执；不启动第二个主动 Bot 写生产库或向 QQ 发消息。
3. 停止旧 Bot 及需停写的相关 Manager，保存一致数据库、配置、插件、媒体、工具正文、协议对象和持久环境回执。QQ 网关可保持运行。
4. 完成配置、授权和语音保全后，沿原 Compose 参数用目标镜像执行 `qq-ai-bot-cli init-db`，核 head `0118`。来自正式 3.8.3/3.8.4 的旧投递计划先在停写副本运行 `work import-legacy-deliveries --dry-run` 再导入；原计划、未知效果和预算保留。
5. 启动一个 Bot，核真实 revision、数据库、QQ 连接、工具合同、模型路由与 worker。启动健康、真实 API、自然聊天和长任务交付分别验收。

## 回退与已知问题

应用核对随包 schema head；完成新迁移后不能仅换旧镜像。先保全升级后产生的消息、文件与回执，在副本验证兼容 reader 或向前修补。`0097` 无事实重建型 downgrade，重建空表也不会恢复语音事实；不回灌旧备份覆盖新数据。

10-09 的 SELF 旧代次领取问题与当时处理记录保留在[残留核查](operations/documentation-residual-audit-20261009.md)；后续源码处理见[Work 与 Memory 审查 C21](architecture/Yuki-Work与Memory多余约束删除审查-2026-10-10.md)。部署状态按对应交付证据读取，不能把历史报告中的待修项当作当前源码结论。保留历史失效批次、检查点和效果，不以清空历史失败数冒充升级成功。

## 旧人物评分关系系统退役

后台评分模型、关系工具/管理员指令、WebUI和SDK Facade全部退出，不再支持旧评分合同。停旧写者后，从实际模型TOML撤 `relationship_evaluation`，从实际环境及Compose frozen override撤关系配置，从operator声明撤 `control.relationship.read/mutate`；其余连接、密钥和授权保留，用目标parser验证。

`0103`清退三张评分表、专属索引、七项评分override，并移除automations/automation_versions的 `context.include_relationship`、按已有算法重算hash。默认false也改变hash；尚未结束的原run须按原ID明确收尾，不能改cursor为新hash或清预算重跑。0102→0103的实际升级记录见关系系统任务书；新安装不再创建旧评分表。

10月9日关系系统退役时，另获授权清退当时的旧Work及子任务，并处理关联plugin Job、自动化run、外部执行和待投递记录；原聊天、Memory及共享产物保留。该次范围及执行状态见 [R23与交付记录](architecture/Yuki-旧关系好感度系统彻底删除任务书-2026-10-09.md#123-全部旧work清退r23用户新增授权)。这项历史授权不适用于后续升级；0109/0110保留原Work和私有协议。

迁移成功后旧0102镜像无法识别0103。故障恢复保留新数据库与上线后的消息/效果，采用支持新schema的代码版本或前向修复，不降stamp、不恢复旧数据库冒充代码回退。SnowLuma与QQ登录资料不在本次更新范围。

## Work 与 Memory 多余约束删除

本轮源码删除固定重试转挂起、完成汇报标签、任务及输入数量、子任务文案、JSON保存字节帽和Dream的唯一/固定数量/全集覆盖政策；状态见[审查与实施记录](architecture/Yuki-Work与Memory多余约束删除审查-2026-10-10.md)。0104删除检查点配额表及六个记账触发器、无人读取的投递target/not_before和窗口索引、主动轮单活动索引；保留原媒体、日志和真实回执，Work来源查询按canonical人关联。自动化run可如实取消，不把合法取消记作blocked错误。升级沿实际head执行，不清数据或恢复旧评分系统。

## Work 树升级到 0105

0105 从旧 worker.root_id 回填真实直接父，再删除旧字段、output_kind/deliver_artifacts、两项汇报标记和未派发暂停通报。原 ID、来源、预算、journal 与已经派发的未知/已确认回执保留。终态 Work 的恢复资料按实际引用保留；父完成不连带归档暂停子任务。升级与部署状态见[本轮任务书](architecture/Yuki-Work生命周期删减与Pi-Durable重构任务书-2026-10-10.md)。

## 0106 自动化原版本与删减

创建及运行完整保存指令与已返回结果，未明示的长度、次数、迟到和失败连坐门已删除；已声明和旧已存的有限预算继续按原Run消费。暂停只停止未来触发，原执行仍可收尾；取消沿真实所属Work树与执行回执处理。

升级不从当前计划、同hash或当前QQ权限回填旧version的委托和旧Run的version_id；历史缺失保留为缺失。新的接纳在原短事务固定真实版本及委托。0106保留这些原执行事实，不提供降级清退；旧0105镜像不能识别新的revision，恢复须使用支持0106的代码，不能stamp或旧库覆盖上线后事实。本轮实施、测试及部署状态见[任务书](architecture/Yuki-自动化删减与指令完整性任务书-2026-10-11.md#84-实施进度与本地证据持续更新)。

## 0107 查询删减优化

删除 `.env` 和实际Bot环境中的 `MEMORY_MAX_REFERENCED_TARGETS`；0107按真实键删除global/user/group的对应override，其余配置不变。迁移添加nickname、Work等待扫描及两条Rollup到期表达式索引，不重写事件、证据、Run版本、预算或效果回执。四索引需要真实迁移，不能只改head标记。

沿现用Compose文件仅更新Bot，默认direct构建；停写副本先演练0106→0107、完整性/FK与原执行记录保全，再迁移生产并确认数据库和OneBot连接。此次不更新SnowLuma。实施与各阶段验收见[任务书§11](architecture/Yuki-资料与数据库查询删减优化任务书-2026-10-12.md#11-2026-10-12-执行与证据)。

## 0109—0111 本轮资料与定时主入口

0108实施的首份/差量公共冻结由0109清退。普通运行资料每次仅装配当前有效值，空资料不展示；短记录工具仍使用 slot/text。迁移退出展示缓存、公共资料选取和无实际引用的自动来源，保留有用笔记、聊天及原Work/协议/产物依赖，不修改旧0054/0108迁移字节。

0110将高层目标及单一静态文字提醒转为共同Main任务；原版本/hash、Run、批准、Work、累计预算和已发送回执保留。无法直接表达为目标的模板、多步骤或未知旧DSL保留原件并停用未来安排，需要显式编辑新版目标。暂停未来安排与停止已发生Work仍是不同操作。定时主入口没有独立static/Profile/能力菜单，固定文字提醒会产生模型费用。

删除实际模型配置的死 automation_agent 路由键，保留其它路由、连接、协议、模型、密钥与显式参数。第三方供应商名称可独立修改，原私有协议不借新连接身份恢复；配置变化沿原revision边界建立合法输入。

0111只删除两项旧来源 CHECK；无聊天事件的 SELF 定时 Main 和派生 Work 以真实原发生、版本及 execution 提供 Memory 来源，不伪造 event 或 initiative。旧事实、证据和回执保持原字段与 ID。不能用 downgrade 或降 stamp 恢复旧封闭检查；回退先核对停写备份和升级后新增数据。

升级先在停写副本运行迁移与恢复回归，再仅更新Bot；SnowLuma及其他服务保持原部署。各阶段证据见[任务书](architecture/Yuki-本轮资料仅当前注入与主入口一致性任务书-2026-10-13.md)，不要把本草案更新当作生产已迁移。

## 0112 共同公共历史

一个真实 Conversation 只有一份公共事件历史，当前 Person、SELF/定时/SDK 入口、个人 LLM/web 偏好和工具清单不另建视图。群的历史窗口使用 Space/global 配置，私人会话仍使用其实际 Person 配置。当前时间与历史显示统一默认 UTC+8，个人/SELF时区设置、读取和设置工具退役。当前图片继续进入实际请求，公共事件保存原文本与附件引用。

0112 清空可重建的公共投影和选取，移除个人范围、自动观察来源与公共 contract_revision，并删除 person_time_settings/self_time_settings 两张偏好表；原 ContextObservation、来源 DAG、Work/checkpoint/journal、不可变版本、protocol objects、tool artifacts/refs 保持原件。明确设置的任务调度时区及已发生时间事实保留。任务笔记只由原 Work 私有路径读取，取消其公共自动发布与付费 scope-summary 链。

0113 随后删除 generation/reset 时清除原观察和 DAG 的旧触发器，保留原来源、私有回执、原聊天、Memory 与引用文件。会话 reset 是独立管理操作，不是数据库升级步骤；历史批量 `/ai new` 的范围只属于对应交付记录，升级不重复该次取消或重置。

迁移前保存一份当前停写冷备，迁移后逐值核对私有表、旧版本、全部 observation/DAG 和 artifact 引用，沿原文件校验器核实际被引用文件。不能将升级前的旧候选镜像直接换回读取新 schema，也不能覆盖上线后的新事实；使用兼容新 schema 的代码或向前修补。

管理操作使用 WebUI、CLI 或 `/ai`。`admin_*` 不再属于主 Agent 工具清单；原 Work 的历史调用与效果回执保留，尚未执行的退役调用返回工具不可用。迁移 `0114` 在升级库中保留旧群停用状态，之后可通过现有群配置编辑。

## 0114—0118 管理入口与恢复收尾

`0114` 把 `spaces.autonomous_enabled=0` 的旧群写入共享配置 `conversation.autonomous_enabled=false`，随后重置旧列，退出两个停用入口并存的状态。已有停用意图、配置历史、审计和工具回执保留；管理操作沿当前控制面进行，不恢复管理员 Agent 工具。

`0115` 只删除 Self Reflection 的五项无人消费诊断列，不重建 owner、水位、原 run、模型请求或记忆 mutation 回执。`0116` 仅把旧 detached continuation 的 `claimed` 状态一次性标为 `blocked / legacy_continuation_retired`；原 request/run、次数和回执继续保存。这不是外部执行失败证明，也不授予重新提交命令的资格。已有命令仍按原 run ID 和完成回执对账。

`0117` 删除无人读取的 `agent_actions` 通用 OneBot 诊断表，实际工具权限、Work effects 和发送回执仍由原执行链持有。`0118` 新建 `status='waiting' AND run_id IS NOT NULL` 的部分索引，让取消对账直接读取仍需处理的原 run；不改完成结果，也不把所有历史执行重新领取。

应用启动要求数据库 revision 与随包单一 head 一致。`0114`、`0117` 不提供事实重建型 downgrade，`0115`、`0116` 的 downgrade 也不恢复旧列或旧 claim；撤 `0118` 索引不代表旧镜像能读取整份新 schema。回退沿兼容版本或向前修补处理，保全升级后新增的消息、文件与效果。

## 尚未实施的替换设计

[Work 摘要退役、会话 Compact 与任务树报告](architecture/Yuki-Work摘要退役与会话Compact及任务树设计审查报告-2026-10-14.md)是 #417/#418 与子 Agent 完整能力继承的待实施方案。当前源码仍有 Conversation Rollup、独立 Work/ordinary 摘要及 worker 工具子集；迁移到 `0118` 不会自动完成这些替换。当前有效的摘要路由、配置与原任务资料按现行合同保留，实施时再同步退役全部消费者。

同样，下一大版本的多客户端重构尚未交付。当前部署继续使用现有 WebUI/控制面；未来接口、升级和数据处理以对应版本的实现及升级说明为准。日期部署、真实模型与 QQ 验收仍按各自原证据读取，不把这份升级文档当作已经上线。
