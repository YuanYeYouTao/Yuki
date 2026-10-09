# Yuki 3.9.0 配置与升级草案（未发布）

<!-- release-baseline: version=3.9.0 schema=0103 -->

本指南对应 main `0ddd7eee`（2026-10-09）的开发源码。**3.9.0 尚未正式发布**，正式下载仍为 [3.8.4 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4)。源码版本号不代表已有 `:3.9.0` 正式镜像；109 个已合并 PR 和最终行为见[发布说明](releases/v3.9.0.md)。

默认构建与发行使用 direct：同一 Agent loop 提供固定完整工具声明，不包含 Monty binding、worker 或 launcher，`CODE_MODE_ENABLED=false`。Code Mode 已合入主线，是显式 `--target codemode` 构建与启用的可选能力。切换模式保留原 Work、composition、预算和回执，不能取得重派发资格。

后续 NapCat 退役已通过 [PR #276](https://github.com/YuanYeYouTao/Yuki/pull/276) 合并，并按开发提交部署；另见[任务书与交付状态](architecture/Yuki-NapCat删除与既有网关抽象保留任务书-2026-10-09.md)及[上线回执](operations/napcat-retirement-20261009.md)。下述旧部署处理适用于包含该修改的源码；3.9.0仍未正式发布。

## 版本与迁移范围

| 项目 | 3.8.4 正式包 | 当前 3.9.0 源码 |
| --- | --- | --- |
| 数据库 head | `0072` | 单一 head `0103` |
| Plugin API | 3.0 | 3.4，需适配并重新批准 |
| 管理 WebUI | 不包含 | 默认关闭，共用控制面 |
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

历史主线的 `0096` 曾删除 MCP 派生表；当前同号保存调用索引，`0098` 按实际形状汇合。编号相同不能证明 schema 相同。其他旧实验数据库需先核生产者与完整形状，不能强行 stamp 或重建数据绕过冲突。

迁移不重建原任务，不重置累计预算、不回填未知用量或重放效果。DDL 和索引成本在停写副本演练，回退使用能读取现行 schema 的兼容代码或向前修补。

## 升级前整理配置

保留实际 Compose 项目名、全部覆盖文件、挂载、模型连接和插件目录。模板只作对照，不覆盖现有部署。

- **模型文件**：使用 `webui-config/model_profiles.toml` 及同目录密钥 sidecar；旧路径按[迁移说明](operations/model-profile-path-migration.md)处理。保留有效连接、模型、密钥引用与 routes，仅移除退役的 `automation_text_generation`、`memory_consolidation`、`memory_attribution`、`relationship_evaluation` 路由和 `gemini_schema_format` wire option。使用目标版本 parser 校验，不用模板重建整份配置。现行 `memory_extraction`、`memory_self_reflection`、`memory_dream` 可独立绑定；Rebuild 复用提取，Embedding 独立配置。
- **结构化输出与思考**：任务沿 Profile 的 `function_tool`、`text_json` 或 `json_schema`；Gemini JSON Schema 使用 `responseJsonSchema`。旧 `responseSchema` projector 与降级开关退役。思考沿显式设置，不再强制开启或抬档；正文 DSML 不再转工具，Claude 适配器不隐式追加付费续跑。核对端点实际协议响应，不能靠配置名证明可用。
- **Memory 策略**：删除仅服务自动注入、归因/强化/意图评分、治理、容量腾位与经验过时的旧设置，对照当前 `.env.example`。`valid_until`、用户显式变更和真实权限仍有效。同 key 不是唯一槽位，管理修改和删除沿 fact ID；不要把多条旧事实压成一个值。
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

### Plugin API 3.4

更新插件 manifest 与代码，移除 `ctx.mcp`、`ctx.speech` 和专属权限/事件/TTS 注册，通过现有批准流程重新核对权限，保持原启用意图。迁移范围见 [API 3.1](plugin-development/api-3.1-migration.md)、[API 3.2](plugin-development/api-3.2-migration.md) 和 [API 3.4](plugin-development/api-3.4-migration.md)。宿主挂载的插件代码不会随镜像自动更新。

## 迁移与启动

1. 固定目标提交和镜像，核随包 head、配置与插件版本；正式发行时再核 `v3.9.0` 资产。
2. 在独立数据库和文件副本演练迁移及启动，核原身份、Work、预算、协议和发送回执；不启动第二个主动 Bot 写生产库或向 QQ 发消息。
3. 停止旧 Bot 及需停写的相关 Manager，保存一致数据库、配置、插件、媒体、工具正文、协议对象和持久环境回执。QQ 网关可保持运行。
4. 完成配置、授权和语音保全后，沿原 Compose 参数用目标镜像执行 `qq-ai-bot-cli init-db`，核 head `0103`。来自正式 3.8.3/3.8.4 的旧投递计划先在停写副本运行 `work import-legacy-deliveries --dry-run` 再导入；原计划、未知效果和预算保留。
5. 启动一个 Bot，核真实 revision、数据库、QQ 连接、工具合同、模型路由与 worker。启动健康、真实 API、自然聊天和长任务交付分别验收。

## 回退与已知问题

应用核对随包 schema head；完成新迁移后不能仅换旧镜像。先保全升级后产生的消息、文件与回执，在副本验证兼容 reader 或向前修补。`0097` 无事实重建型 downgrade，重建空表也不会恢复语音事实；不回灌旧备份覆盖新数据。

10-09 临时 SELF 排空暴露旧代次重试阻塞，新队列已通过隔离失效窗口恢复，但领取源码尚未修复；禁用语义空间的旧 owner 调度也仍待处理。见[残留核查](operations/documentation-residual-audit-20261009.md)。保留历史失效批次、检查点和效果，不以清空历史失败数冒充升级成功。

## 旧人物评分关系系统退役

后台评分模型、关系工具/管理员指令、WebUI和SDK Facade全部退出，不再支持旧评分合同。停旧写者后，从实际模型TOML撤 `relationship_evaluation`，从实际环境及Compose frozen override撤关系配置，从operator声明撤 `control.relationship.read/mutate`；其余连接、密钥和授权保留，用目标parser验证。

`0103`清退三张评分表、专属索引、七项评分override，并移除automations/automation_versions的 `context.include_relationship`、按已有算法重算hash。默认false也改变hash；尚未结束的原run须按原ID明确收尾，不能改cursor为新hash或清预算重跑。当前生产库0102正常升级，新安装不再创建旧评分表；不承诺任意旧评分中间版本兼容。

本次运维另获授权清退全部旧Work及子任务，关联plugin Job、自动化run、外部执行和待投递记录必须一并处理；不删除原聊天、Memory或共享产物。具体范围及实际执行状态见 [R23与交付记录](architecture/Yuki-旧关系好感度系统彻底删除任务书-2026-10-09.md#123-全部旧work清退r23用户新增授权)。这是本次实例的明确授权，不是以后升级默认清Work的行为。

迁移成功后旧0102镜像无法识别0103。故障恢复保留新数据库与上线后的消息/效果，采用支持新schema的代码版本或前向修复，不降stamp、不恢复旧数据库冒充代码回退。SnowLuma与QQ登录资料不在本次更新范围。
