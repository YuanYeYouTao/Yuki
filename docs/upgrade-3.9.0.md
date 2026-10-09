# Yuki 3.9.0 配置与升级草案（未发布）

<!-- release-baseline: version=3.9.0 schema=0102 -->

本指南对应测试分支兼容主线 `25cd6083` 后的开发源码。**3.9.0 尚未正式发布**，正式下载仍为 [3.8.4 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4)。开发提交镜像已有各自运维记录，但应用版本号不代表可拉取的 `:3.9.0` 正式镜像。全部 102 个已合并 PR 与功能变化见 [发布说明草案](releases/v3.9.0.md)。

默认构建、发行和部署使用 direct：镜像不包含 Monty binding、worker、launcher，Code Mode 默认关闭；当前作用域全部获准工具仍经同一 Agent loop 执行。Code 为显式 `--target codemode` 可选构建，启用前须完成目标主机隔离验收。模式切换沿原回执收尾旧 composition，不重置预算或重派发。

## 版本与迁移范围

| 项目 | 3.8.4 正式包 | 当前 3.9.0 源码 |
| --- | --- | --- |
| 数据库 head | `0072` | `0102`，按随包 Alembic 单一 head 核对 |
| Plugin API | 3.0 | 3.3，插件需适配并重新批准 |
| MCP | 旧实现 | 完全退役 |
| 管理 WebUI | 不包含 | 可选、默认关闭，共用现有控制面 |
| Genie-TTS 语音输出 | 旧可选组件 | 合成、SDK、配置、Worker 与发布依赖全部退役；ASR 保留 |

已运行开发镜像的部署从**实际数据库 revision**继续迁移，不按应用版本猜测。正式 3.8.3 的 head 为 `0061`，须先满足 [3.8.4 升级指南](upgrade-3.8.4.md) 的停写和媒体会话切换前提。已经完成 `0072` 的部署不得再次执行离线 generation 重置；更早或过渡态不承诺直接升级，不使用 `stamp` 跳过迁移。

| 迁移 | 内容与数据边界 |
| --- | --- |
| `0073`—`0075` | 控制请求状态/回执、执行诊断及发送事件关联 |
| `0076`—`0078` | Work、轮次/来源事件与模型用量的有界查询索引 |
| `0079` | 区分逻辑模型调用和已派发的物理请求，保留未知用量 |
| `0080`—`0081` | 新拆分发送计划份数、Claude 缓存创建及 5 分钟/1 小时用量；不推断历史缺项 |
| `0082`—`0085` | 来源 revision 失效覆盖、receipt 引用、媒体/联网清理和 Work 查询索引 |
| `0086` | 保存工具效果类型及原工具正文引用，保留已确认和未知效果 |
| `0087` | 新 Work 可配置空累计上限；旧任务有限预算和累计用量不重置 |
| `0088` | 私有协议对象按原 Work 管理引用及存储配额 |
| `0089` | 保存有序观察来源与引用边界，保护原 artifact owner |
| `0090` | Memory 维护候选读取索引 |
| `0091` | 普通消息接纳事实，按原内部事件去重，不创建第二套执行日志 |
| `0092`—`0093` | 普通反馈、协议清理游标及 evidence compaction 候选索引 |
| `0094` | pending 且未完成准备的 Work 输入部分索引，避免空轮询扫描已消费历史 |
| `0095` | Person/inbound 发送来源部分索引，保留全会话来源资格和原权限语义 |
| `0096` | 核验完整形状和外部依赖后，仅删除 `mcp_server_states`、`mcp_tool_cache` 两张派生表 |
| `0097` | 核验 Speech 四表完整形状与合法 FK，拒绝未知依赖及未终态生成；精确清退配置键并删除四张专属表。真实事实与被引用 WAV 必须先冷备 |

迁移不重建原 Work、预算或发送回执，也不回填未知请求和旧账单。建索引及 DDL 的锁与 I/O 成本应在独立副本测量，不能假定在线零影响。

当前完整集成树只有 `0102` 一个 head；`0102` 撤四个 active/key 唯一索引，保留全部事实和回执。`0096` 保留调用索引并兼容主线 MCP 已退役的形状；`0097` 保留主线语音退役身份，`0098` 汇合 MCP 派生表退役，`0099` 冻结摘要 kind/renderer，`0100` 关联插件后台 Job 与原 Work，`0101` 为旧效果回执补规范 outcome（不改原 result）。不能用 stamp 或重建数据库解决编号冲突。

本次操作者已确认仅有当前 main 系谱 `0096` 生产库需要升级，没有另一个需保留的 Pi `0097/0098` 数据库。若其他安装报告这种旧分支编号，先核完整 schema 与生产者提交，不能按编号相同直接升级。

正常升级保留原 Work ID、内部事件、invocation/composition、发送回执、协议对象引用和累计预算。孤立 running 执行由取得有效新租约的 owner 保守结算；未知效果不重跑。语音 queued/generating、未知表形状或外部依赖会阻止迁移。停写一致快照和冷备完成后再执行。

`0097` 不提供事实重建型 downgrade；当前完整数据库只能使用兼容 `0101` 的 reader 或前向修复。早期仅撤销索引的降级步骤不适用于本次生产升级，不能恢复旧备份覆盖升级后的事件和效果。历史 producer 的完整升级、幂等重跑及降级拒绝由集成测试验证。

备份校验步骤见 [Work 协议与证据完整备份](operations/work-evidence-backup.md)。

## 升级前整理配置

保留实际 Compose 项目名、全部覆盖文件、挂载、模型连接和插件目录。新模板用于对照，不覆盖现有部署；不要依赖一个简化的 `docker compose` 命令替代原参数链。

- **模型连接**统一使用 `webui-config/model_profiles.toml`，密钥 sidecar 与其同目录。旧 `config/model_profiles.toml` 按[路径迁移说明](operations/model-profile-path-migration.md)核对；指定文件缺失会阻止启动。实际协议、能力和路由按[模型供应商合同](architecture/model-providers.md)配置，不因模型名称自动猜测。
- **联网默认值**：新安装未提供 `WEB_MODE` 或旧 `WEB_ENABLED` 时默认 native。显式禁用和既有 Profile 选择不被覆盖；仅旧 `WEB_ENABLED=true` 仍沿 Tavily 模式核验凭据。Gemini 独立搜索桥、Claude/Responses 原生搜索、DeepSeek 独立搜索连接各按真实能力配置。默认开启不能补出模型没有的能力。
- **持久环境**为单独部署的可选组件。保留工作区 volume、原 Manager 和运行回执，核对 Bot 与 Manager 使用同一目录；普通部署包不会自动安装 gVisor 或新 Manager。见[持久环境说明](operations/persistent-environment.zh-CN.md)。
- **管理 WebUI**默认关闭；按[WebUI 合同](architecture/webui-console.md)设置身份、认证与管理授权。既有配置保存、热切换和实际请求生效分别核对。

### generated 模型路由退役

`generated` / `yuki.generate` 已退出新执行，`automation_text_generation` 不再是可配置的模型任务。升级实际 `webui-config/model_profiles.toml` 时，仅从 `[routes]` 删除 `automation_text_generation = ...` 这一项；保留其他文件字节、Profile、连接、密钥引用和有效路由，不用新模板覆盖现有配置，也不把旧路由自动改成 `automation_agent`。新版本会明确拒绝仍含此退役路由的完整旧 TOML，不静默忽略。当前自动任务按已有 `automation_agent` 主 Agent 合同执行。

历史 `model_invocations.task`、统计和错误记录继续保留并读取原字符串；不改写旧账单，不据此重跑旧自动任务。备份已核验后，由操作者对实际配置作上述单项删除，再用目标版本 parser 验证。

### MCP 退出与管理授权

1. 清理旧 MCP 环境变量、`.mcp.json` 挂载、配置和专属服务定义；对照实际生效的 Compose 链，而不只检查模板。已存在的未知调用不取得重放资格。
2. 检查 `CONTROL_OPERATORS_FILE` 指向的**实际**管理授权文件，从每个 operator 的 `capabilities` 显式撤去 `control.mcp.read`、`control.mcp.mutate`、`mcp.web_search` 等退役权限。保留 `principal_id`、`person_id`、`roles`、`token_env` 和其他有效授权，不自动改授新权限，不用[示例文件](../config/control-operators.example.toml)覆盖生产身份。
3. 用目标版本代码加载实际授权文件验证。管理声明仍严格校验，包括禁用的 operator；残留 MCP 权限会阻止 Bot 启动。它们不会像旧 MCP 环境设置一样被忽略。

原数据库内 MCP 两张派生表由 `0096` 迁移删除。共享工具正文、图片、来源证据、Memory 调用证据和 Work 回执迁入 `tool_results`，原存储路径、读取授权及历史内容保留；不要把这些共享数据当成 MCP 缓存删除。

### Genie-TTS 退出与旧执行清退

升级前停止接纳新的语音效果，按原 Work/operation/plugin-execution ID 核对进行中合成、
已派发和未知发送。已确认不重发，未知不自动失败或退款，确定未派发的旧 voice/AUDIO
明确拒绝，不转文字。旧图片仅兼容五个精确默认值的退役字段，兼容解码不改冻结计划、
hash、effect_key、序列或预算；混合剩余计划在任何新派发前完整校验。

停写后保存一致数据库与文件冷备，核 `speech_generations` 真实执行事实、原通用回执和
WAV 引用；另保全用户自有参考音频。冷备包含校验和、原路径和内部 ID 映射，且须验证。
不把生成表当缓存，不回写归档到运行库。未完成核对与保全时不执行 `0097`。
迁移拒绝 queued/generating 记录不代表其他终态已完成现场核对，不能用 `sent` 证明 QQ 送达。

按实际 Compose 链撤去 Worker/speech profile、socket volume 和 Bot speech 挂载；清退
`SPEECH_*`、`GENIE_DATA_DIR`、`BOT_VOICE_NAME`，包括旧 `SPEECH_AGENT_EFFECTS_ENABLED`
与现行 `SPEECH_AGENT_DELIVERY_ENABLED`。从每个实际 operator 的 capabilities 显式撤去
`control.speech.read`、`control.speech.mutate`，保持其余身份、权限和启用状态，用目标代码严格加载验证。
不以模板覆盖生产配置，不自动忽略退役权限，不删除 ASR/Qwen 配置。

服务器文件另按“精确绝对路径—归属/使用者—引用—保全位置”清单清理，`speech_root` 可
自定义；源码移除不授权直接删整个 data、用户音频、共享 artifact 或有效备份。
部署包中的恢复核对至少覆盖：已确认 AUDIO 跳过派发、unknown/prepared 维持原围栏、
确定未派发 AUDIO 零效果拒绝、已确认语音加合法剩余图片/文本按原身份恢复、混合剩余
计划在任何新发送前完整拒绝退役部分，以及取消后迟到确认只结算原事实。图片的五个
兼容默认值为 `spoken_text=""` 和其余四字段 `null`；缺失允许，false/0/错误类型不当作空。
共享 `local_path`、`duration_milliseconds` 仅兼容旧图片中的 `null`，不作为合成字段恢复。
跨工具合同升级也须保留原投递链和回执，不能丢计划再进模型重新生成答案。

### 插件 API 3.3

更新插件 manifest 与代码，移除 `ctx.mcp`、`ctx.speech`、相关 facade、权限、事件及 TTS 注册；原批准失效后，通过现有插件批准流程重新核对请求权限并批准，保持原启用意图。不得为适配增加未经核验的授权。依次核对 [API 3.1 历史迁移](plugin-development/api-3.1-migration.md) 、[API 3.2 迁移](plugin-development/api-3.2-migration.md) 和 [API 3.3 迁移](plugin-development/api-3.3-migration.md)。

网易云 MCP 插件不再提供；普通插件 HTTP、自有工具和 QQ 音乐卡片发送保留。后台表情分类、SDK 显式视觉任务和 ASR 仍可能使用独立 Qwen 连接，不能因主 Agent 原生读图而删除这些消费者所需凭据。

## 迁移与启动顺序

1. 固定目标源码/镜像提交，核对随包 head、Plugin API、全部迁移与发行资产；正式发布时再核对 `v3.9.0` 的实际下载和镜像。
2. 在**独立数据库及文件副本**演练完整迁移、配置加载、插件批准和应用启动。核对原 Work 身份、预算、协议对象、发送回执与工具正文引用；隔离演练不启动第二个主动 Bot 写生产库或向 QQ 发消息。
3. 停止旧 Bot 和需要停写的相关 Manager，保存一致的数据库、配置、插件、媒体、工具正文、私有协议对象及持久环境回执。QQ 网关可保持运行。完整范围及恢复映射见 [Work 证据备份](operations/work-evidence-backup.md)。
4. 完成 Genie 旧执行、真实事实与 WAV 冷备门槛，撤去退役 operator 权限/环境/挂载，再沿实际 Compose 参数，以目标镜像执行 `qq-ai-bot-cli init-db`，检查 head 为 `0102`（`0100` 关联插件后台 Job 与原 Work，`0101` 为旧效果回执补规范 outcome，不改原 result）。若数据库来自 v3.8.3/3.8.4 备份，升级后在停写副本运行 `qq-ai-bot-cli work import-legacy-deliveries --dry-run` 再正式运行；未导入的旧计划只保持暂停，不会被在线执行。更新宿主挂载的插件代码并完成原权限重新批准；只更新镜像不会更新独立挂载目录。
5. 启动一个新 Bot，核对实际 revision、数据库、QQ 连接、主工具合同、插件/worker 状态与模型路由。健康检查不代表自然聊天速度、真实 API 或长任务交付已验收。

QQ 登录目录不受迁移影响。若另外重启 SnowLuma，保留原持久挂载；保存登录态、自动启动与成功自动登录是不同步骤，重启后须确认真实 QQ 连接，必要时手动登录。

## 回退与失败处理

应用严格核对 schema head。完成 `0097` 后**不能只换旧镜像**，优先在新 head 向前修补。若启动被残留 MCP/Speech 管理权限挡住，撤去旧权限并用当前镜像验证，不通过恢复旧库绕过校验。

`0096` 的离线 downgrade 仅重建空 MCP 派生表；`0097` 不提供能恢复语音事实的自动回退。即使重建空结构也不能恢复已删除配置、偏好、生成记录或 WAV，更不授权生产数据库回退。`0091` 的接纳事实、Work 检查点、模型用量与发送回执必须保留；早期针对单个索引的 downgrade 命令不适用于当前完整数据库。

需要回退时，另行选择能读取现行 schema 和事实的兼容代码，在升级后的副本演练。保全升级后产生的消息、文件、预算及回执，不回灌旧备份覆盖新事实；旧的未知发送、终端或原生效果先沿原执行 ID 核对，不能因为升级失败盲目重发或重跑。
