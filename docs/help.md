# Yuki 使用与运维帮助

当前源码为未发布的 Yuki 3.9.0，Alembic head 为 `0102`，Plugin API 为 `3.3`，只支持 canonical runtime。永久 Yuki、
Person、Binding、Space、Presence 和 canonical Conversation 的关系见
[当前架构](architecture/canonical-runtime.md)。

## 安装与启动

Linux：

```bash
chmod +x install.sh
./install.sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

两个源码安装器的默认版本为 `3.9.0`，不表示该版本已正式发布。引导配置会询问主模型、
可选 Flash/Embedding/Web/Vision、QQ Provider、Plugin 与 Automation，不再配置 Genie。
密钥输入不回显，程序不会在线试用 API key。

日常运维：

```bash
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose logs --tail 200 bot
```

不要提交 `.env`、`data/`、`plugins/` 中的私有数据、Provider 登录目录或任何 Cookie/token。

## QQ Provider

Yuki 不再支持 NapCat，当前附带的 QQ/OneBot v11 Provider 为 SnowLuma。现有 Gateway 抽象保留，
业务 owner 不随网关实现改变。SnowLuma 可以连接不同 QQ；同一 QQ 只能有一条活动连接，
重复连接拒绝新的，不会自动挤掉旧连接。

安装向导只写配置，不执行停止、启动或自动切换。旧部署的 Compose 与 `.env` 不会自动替换，
需要按[旧 NapCat 配置清退](upgrade-3.9.0.md#旧-napcat-部署配置)逐项处理。

SnowLuma 默认地址：

- noVNC：`http://127.0.0.1:6081`
- WebUI：`http://127.0.0.1:5099`

默认 bind address 由以下变量明确控制：

```dotenv
SNOWLUMA_NOVNC_BIND_ADDRESS=127.0.0.1
SNOWLUMA_WEBUI_BIND_ADDRESS=127.0.0.1
```

只有明确需要远程访问时才设为 `0.0.0.0`，并同时启用强 VNC 密码、WebUI 认证、主机防火墙和
可信源限制。优先使用 VPN 或 TLS 反向代理；不要公网暴露 VNC、OneBot HTTP/WS、token 或 Cookie。
详见 [SnowLuma 部署](deployment/snowluma.md)。

Provider 合同检查：

```bash
docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
```

doctor 是只读检查，不发送消息、不调用私有 action，也不输出凭据。

## 模型配置

模型可在 WebUI 的「模型与用量」中配置，也可用 `qq-ai-bot-cli setup` 完成初始引导。
WebUI 选择供应商和协议，直接填写 API Base URL、模型 ID 与 API Key，然后按用途选择模型连接。
新输入的密钥保存在服务器私有文件，不会在页面回显；在线模型管理保存后热应用新连接与搜索后端，已开始的激活继续使用原固定 Profile。手工修改文件或启动环境时按页面提示与部署流程重新加载。
支持 Chat Completions、Responses、Claude Messages 和原生 Gemini GenerateContent；
具体思考、工具、图片、结构化输出与缓存能力取决于供应商和模型。

测试 Gemini 3.8 Flash：

1. 在「模型与用量」选择「添加模型连接」，供应商选「Google Gemini」。页面预填
   `gemini-3.8-flash`、`https://generativelanguage.googleapis.com/v1beta` 和原生 Gemini 协议。
2. 在 API Key 输入框粘贴 Google 提供的密钥。先仅把「主对话」指向新连接，其他用途沿用现有模型。
3. 点击「检查并保存」，确认页面提示已应用，再确认健康状态和实际已加载路由。
4. 用明确授权的测试会话发一条文字消息，按需再测图片及工具。到「轨迹与审计」查看该轮的请求、响应、
   工具与投递步骤；到「模型与用量」查看 Gemini 调用次数、输入/输出 Token 及缓存命中报告。
5. 测试后若要恢复原路由，在 WebUI 将「主对话」选回原连接并保存，确认已应用。

这套配置不会在保存时发出模型请求；真实模型效果、密钥有效性和上游可用性需第 4 步验证。
Gemini 的 Google 搜索通过 `search_mode="bridge"` 的独立请求接入，主对话保留本地 `web_search` 工具；该连接与上游仍须实际支持搜索。Interactions、Live 和 TTS 尚未接入。用量是 Yuki 入账的调用，
不是供应商账单；没有可靠单价时页面不估算费用。

通用原则：

- 不在聊天、日志、Issue 或 Git 中粘贴 API key。
- Responses 请求默认不发送 `temperature`。
- 思考与结构化模式沿显式 Profile/请求设置，不强制开启、设置最低档位或自动抬档。
  各协议参数及真实能力见[模型供应商合同](architecture/model-providers.md)。
- Web、Embedding 和 Vision 都是可选能力；不可用时应有界降级，不影响纯文本主路径。
- secret 只能写入或查询“是否已配置”，不能通过控制面读回。

修改启动环境或需重启的配置后先检查，沿用原项目名及全部 Compose 覆盖文件：

```bash
docker compose config --quiet
docker compose up -d --no-deps --force-recreate bot
docker compose logs --tail 200 bot
```

## Conversation 与群聊

- 私聊 Conversation 属于 Person；同一 Person 的多个 QQ Binding 共用会话。
- 群聊 Conversation 属于 Space；更换 Yuki Presence 不会产生新的群会话。
- `/ai new` 是改变 Conversation generation 的显式操作。
- 群 ingest 路由决定哪个 Presence 处理某个外部群。路由暂停或不匹配时，消息在 Agent 和 Memory
  之前失败关闭。
- 切换账号时，不在当前账号群列表中的旧群只是暂时不可达，不应删除群设置或按共同群交集关闭。
  切回旧账号后，未手动暂停的旧路由可继续使用。
- 历史遗留的暂停路由可由超级管理员在**目标群内**单独发送 `/ai on` 恢复。该命令经过真实连接、
  canonical 身份和实时群成员校验，在同一事务中启用群并修复必要的入站/发送路由，保留健康的
  既有路由。不需要 `/ai new`，不会重置会话或记忆，也不进入聊天历史。
  暂停群中的其他 `/ai` 命令只向超管提示恢复方法，不执行原命令；普通用户不能绕过路由围栏。
  多个候选账号且没有健康接入 pin 时拒绝恢复；仅私聊发送 `/ai group <群号> on` 只修改群启用设置，
  不能替代目标群的连接证明。
- 当前事件回复优先使用 ingress 连接；自动化和主动通知读取持久路由。
- @、reply 与 mention-only 消息从 OneBot `original_message` 保序投影；任一 Yuki Presence 都
  被识别为 Yuki。

Rollup 只压缩 Prompt 历史，原始 `chat_events` 不被摘要替代。摘要是不可信输入，不进入 Memory。
详见 [Conversation Rollup](architecture/conversation-rollup.md)。

## Memory 与关系

Memory owner：

- SELF：永久 Yuki 自身。
- PERSON：永久 Person。
- GROUP：永久 Space。
- PERSON_GROUP：某 Person 与某 Space 的共同经历。

结构化读取按后端记录的历史共同群关系授权：本人 Person 始终可读；他人 Person 需要直接历史
共同群；Group 需要请求者的历史 membership；PersonGroup 需要双方都曾属于该群。退群、群停用、
Provider 离线或 Presence 切换不会自动撤销这项历史关系。共同群授权允许读取 Person 中来自私聊的
结构化事实，但不开放原始私聊、他人 evidence/private SELF，也不扩大 mutation 权限。群聊可直接
查询其他历史共同群，私聊查询群记忆时必须指定群名或兼容 ID；同名歧义会要求澄清，无结果则正常
返回空结果。

普通自动提取按同一 canonical owner 聚合；达到 12 条、8,000 字符或最老事件等待一小时中的任一
条件才领取。显式记住、纠正与删除仍立即执行；没有足够价值的新事实时 noop 是健康结果。只自动
保留稳定事实、持续偏好及有意义的一次性共同经历。

`memory_change.visibility` 只对 SELF 生效。PERSON、PERSON_GROUP 和 GROUP 目标若带合法的
`current_scope` 或 `global` hint，会忽略该 hint 后继续授权与 mutation；非法 visibility 仍会
被拒绝。Memory 事实保留 Evidence、authority、confidence、状态、冲突和版本链。

同一 Person 的多个 Binding 共享关系、偏好、人物记忆和历史。Yuki Presence 与第三方机器人不
创建人物关系。`/ai forgetme` 按 Person 删除其拥有的数据，删除后旧 Binding 不再能读取关系或
Memory。

更多资料：

- [Memory V2](architecture/memory-v2.md)
- [Memory 变更](architecture/memory-change.md)
- [Memory 质量运维](operations/memory-quality.md)
- [Memory 重建](architecture/memory-v2-rebuild.md)

## 接收语音

私聊可以直接发送 QQ 语音。群聊仍按原有启用状态、@、引用和触发规则决定是否回复；
不会把群里的每条语音都提交识别。触发回复的消息可以包含语音，也可以引用一条语音。
Yuki 会根据转写内容回复，之后可以回忆或搜索这条语音。识别不成功会明确提示，不会猜测。

`ASR_ENABLED` 控制入站和引用语音识别；Genie 合成及专属语音发送功能已退出，通用管理员 OneBot 接口仍遵守原权限合同。默认使用 `qwen3-asr-flash`，复用现有千问
连接。缺少可用连接时会说明服务未配置。详见 [配置与验收](speech/recognition.md)。

## 工具、权限与控制面

主 Agent 在启动时冻结工具合同，执行处按当前 Principal 核验授权。默认 direct 模式直接声明部署内固定完整工具清单；显式启用 Code Mode 时，固定直调工具保留终端、联网搜索和网页读取，其余工具经 Code Mode 调用。
Capability 决定 metadata、外部 ID、正文、mutation 与 destructive 操作的不同权限。

Control Plane 提供 CLI、QQ command 和 WebUI 共用的 Query/Command 服务。WebUI
把登录身份转换为 `ControlPrincipal` 与 `DecisionContext`，不直接访问 ORM、数据库或
Gateway；当前功能边界见 [WebUI 合同](architecture/webui-console.md)。

高风险边界：

- 不开放任意 OneBot action、plugin arbitrary run 或 raw SQL。
- `SUPERUSERS`、数据库 URL、token、Cookie 和 API key 不可读回。
- `/healthz` 只返回公开瘦健康载荷。
- 管理审计、路由和内容查询必须经过对应 Capability。

在 QQ 中使用 `/ai help` 与 `/ai capabilities` 查看当前可用命令和能力；实际结果以当前
Principal、会话和运行配置为准。

## Plugin API 3.3

当前 Host 只接受精确声明 Plugin API `3.3` 的插件，Genie 专属 facade、事件及权限，以及直接模型 LLMFacade 已移除；模型任务使用 `agent.run`。插件可以使用固定 primary `conversation_key`，也可读取可选的
person、space、conversation 和 presence ID。插件不能自报超级管理员，也不能绕过
Control Plane、Capability 或 Gateway Registry。

插件发布的主动事件继续以独立 `external_event` 落账，不伪装成真人聊天。插件若请求 Yuki
点评，只会创建可靠 WakeupRequest；Worker 加载该 canonical Conversation 与普通聊天完全相同的
Rollup、raw history、当前人物与场景资料、Prompt compiler、工具合同和模型 profile，再把当前事件摘要作为
唯一的临时 user 尾部。这个提醒不写入 history；成功发送的 Yuki 主动消息会用
`caused_by_event_id` 指向来源事件。pending/processing 唤醒任务会阻止 Rollup 提前覆盖来源，任务
终态后自动解除。没有真实用户事件证明时，管理员与 mutation 能力继续失败关闭，当前目标允许的
Web、Memory read 和 history read 仍可使用。

开发入口：

- [Plugin 开发索引](plugin-development/index.md)
- [架构](plugin-development/architecture.md)
- [权限与安全](plugin-development/security.md)
- [从旧 Plugin API 迁移](plugin-development/api-3.3-migration.md)

## Emoji 与 Vision

- Emoji 资产有独立生命周期、审核和作用域；见 [Emoji 文档](emoji-system/architecture.md)。
- Vision 是可选 Provider，失败时不会把任意外部 URL 当作可信媒体。

这些扩展都服从同一 Principal、Capability、审计、路由和 canonical owner 规则。

## 数据与升级

当前完整迁移链至 `0102`。`0098` 退役 MCP 状态，`0099` 冻结已提交摘要表示；`0100` 关联插件后台任务的原 Work，`0101` 将旧效果结果归一为明确 outcome 并保留原正文；`0102` 移除同 memory_key 单 active 的唯一索引，同 key 的独立事实可以共存。历史 `0048` bridge 的来源限制仍有效，不能用版本号或
手工 stamp 跳过来源校验。`0097` 退役四张 Genie 表及固定配置键，执行前必须核原
Work/发送回执及被引用 WAV，保全专属事实；不能把生成表当作纯缓存。

升级前停止所有写入，并按同一时点备份：

- `qq_ai_bot.db`
- `qq_ai_bot.db-wal`
- `qq_ai_bot.db-shm`
- 配置、Compose 文件、镜像 digest 和 Provider 登录目录
- 宿主插件、协议对象、工具 artifacts、持久 Work 证据及被引用音频

`0097` 与 `0102` 明确拒绝丢失事实的 downgrade，优先向前修补。换回旧镜像不会恢复已删事实；恢复历史
快照也不能覆盖升级后新消息、文件和回执。不要手工 stamp revision 或只恢复主 DB。
当前权限清退、旧执行核对、冷备、插件 API 更新及切换步骤见
[3.9.0 升级指南](upgrade-3.9.0.md)。

## 故障排查

### 私聊可用但群聊不回复

检查：

1. Space 与 SpaceBinding 是否 enabled。
2. SpaceBinding ingest route 是否 paused。
3. 当前 Presence 是否为该外部群的唯一 ingest。
4. 当前 QQ 是否仍是群成员，且 Provider 成员探针可用。
5. 群策略是否要求 @，以及当前消息是否正确投影到 Yuki Presence。

不要为“先能说话”而伪造旧 group/scope 行或随意改 Conversation generation。

### Provider 已登录但没有连接

```bash
docker compose --profile snowluma ps --all
docker compose logs --tail 200 snowluma
docker compose logs --tail 200 bot
```

若看到 `provider_conflict`，先停止旧 Provider并等待 Registry 注销。同一 QQ 的新连接不会挤掉旧
连接。

### 回复很慢

分别检查 Gateway 延迟、模型首 token、工具循环、Web/插件调用、Rollup backlog、Memory worker
和发送回执。不要只根据最终回复时间判断网络不稳定。管理健康可提供分类状态，但不包含 secret
或模型 reasoning。

### 数据库升级失败

不要继续启动 Bot。保留日志和故障现场，确认快照 checksum，然后按升级指南判断来源是否满足
historical 0048 bridge。pre-3.8 数据库不能通过关闭检查强行启动。

## 发布与版本

发布流程见 [版本化 Docker Release](operations/versioned-docker-release.md)，当前开发草案见
[3.9.0 说明](releases/v3.9.0.md)，旧版本记录见 [3.8.1 说明](releases/v3.8.1.md)。正式镜像与 Release 由 tag 流水线核对版本身份及 main 祖先关系，构建后验证已发布镜像、部署包、release smoke 与匿名拉取；Quality 的源码、前端与测试结果另行核对。
