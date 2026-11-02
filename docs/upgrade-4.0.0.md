# Yuki 4.0.0 升级指南

<!-- release-baseline: version=4.0.0 schema=0155 -->

[English](upgrade-4.0.0.en.md) · [开发基线说明](releases/v4.0.0.md) · [完整 PR 目录](releases/v4.0.0-prs.md)

本指南面向从正式 3.9.0 或其后开发版升级的部署。2026-11-02 核对的待发布截点为 `81f10229`（#558）：数据库单一 head 为 `0155`，Plugin API 为 `9.0`，个人 Client API revision 为 `9`，Code API 仍为 `yuki.codemode.api.v2`。正式 3.9.0 的 `0119`、API `3.5`、原安装包和镜像标签保持其历史内容；4.0.0 尚未正式发行。

## 先确定实际部署

| 项目 | 正式 3.9.0 | 4.0.0 开发基线 |
| --- | --- | --- |
| Python 模块 / 包 | `qq_ai_bot` / 旧包名 | `yuki`，不保留旧模块 shim |
| 命令 | `qq-ai-bot-cli` / `qq-ai-bot` | `yuki` / `yuki-bot` |
| 数据库 head | `0119` | `0155`，新增 34 个迁移节点 |
| Plugin API | `3.5` | `9.0`，精确匹配 |
| 管理界面 | 临时 WebUI | 管理 HTTP API、CLI、`/ai admin`；个人客户端独立 |
| 管理资格 | QQ 账号名单 | Person 持久资格，operator 独立授权 |
| 文件 / 包操作 | 多项工作区工具和专用安装链 | 共同持久终端；查看与发布保留 artifact 桥 |
| 表情 / 入站媒体 | 旧表情池、自动展开 | 文件图库、后台配图、24 小时共享原件按需读 |

先记录实际 `DATABASE_URL`、数据库 revision、完整 Compose/systemd 启动链、Bot 与网关、Manager、环境容器及挂载、插件版本和批准。已经运行开发版的实例从自己的真实 revision 继续，不重复处理已经完成的变更，也不用应用版本猜数据库结构。

## 1. 保留原数据库与部署命名

新安装默认 Compose 项目是 `yuki`、网络是 `yuki-network`，数据库是 `data/yuki.db`。已有部署保留自己的项目、网络、volume、工作区和数据库路径。旧库无需改名；不能因为新默认值创建第二个空库。

旧源码/native 安装如果没有明确的 `DATABASE_URL`，在更新之前将原库绝对路径写入实际 `.env`，让迁移和服务读取同一值。例如：

```dotenv
DATABASE_URL=sqlite+aiosqlite:////srv/yuki/data/qq_ai_bot.db
```

更新 Python 安装、导入路径、systemd、Manager 的模块路径、脚本和实际 Compose command 为 `yuki` / `yuki-bot`。重新同步锁定依赖或构建目标镜像；只移动已安装源码不能替代包更新。未来镜像仓库名为 `ghcr.io/yuanyeyoutao/yuki`，本次开发基线不会使该仓库出现 4.0.0 发行标签。

## 2. 一致备份后前向迁移

停止 Bot 写入并保存主数据库、实际使用的参与/Manager/产物回执数据库、配置、插件数据、协议对象与所有被引用文件。持久终端还应保存原 HOME/runtime 文件系统和当前完整 rootFS；其镜像配置可能含 execd 凭据，应留在私有运维备份中。只暂停涉及迁移的真实写者，网关、代理和其他服务保留原实例。

在隔离副本中使用同一配置与目标版本验证迁移，再沿正常启动路径升级原部署。`0119` 到 `0155` 是单条迁移链，共 34 个新节点；`0124` 直接接 `0126`、`0145` 直接接 `0147`，无需补造缺号。不使用 `stamp` 跳过迁移，不重置会话或累计预算。

| 迁移 | 实际变化与保留范围 |
| --- | --- |
| `0120`—`0122` | 私聊按原账号/平台入口与 topic 分开；加入个人设备与配对，保留旧历史的原入口 |
| `0123`—`0124` | 独立 Memory 操作保原回执；通知批准保存原 Conversation，歧义不猜路由 |
| `0126`—`0129` | 原监视器目标与 Rebuild 改内部身份；临时媒体清理后保 publication 线索，人物名称与同人归属独立 |
| `0130`—`0131` | 删除聊天/Work 双窗口与自动搜索抓页的当前 override，配置审计保留 |
| `0132` | ACP session 与原请求增加独立 Conversation scope；原 NULL 回执和私有请求保留 |
| `0133` | 公共投影原字节迁入不可变追加块，顺序和当前内容保留 |
| `0134` | 删除无消费者的工具/回合统计表及旧诊断索引，真实工具与业务回执保留 |
| `0135`—`0137` | 名称查询索引、Person 超管资格和原管理请求关联；删除设备 GET 心跳写入 |
| `0138`—`0140` | 退役旧表情池；保存后台配图意图与真实引用附件快照，不重写旧请求 |
| `0141` | Account + 唯一 AccountAssignment 替代双归属表；保原阶段 UUID、作者、会话、设备和执行事实 |
| `0142` | 规范派生名称并建立原位提及点查索引；历史事件和发送回执不改写 |
| `0143` | 回填原事件附件引用、增加现行查询索引、删除独立插件会话无效能力列 |
| `0144`—`0145` | 统一历史检索结构、唯一索引和原事件/回执锚点；必须停业务写入升级 |
| `0147` | 增加单一 SOUL 与名称资料；后续权限实现已移至 Person 超管，旧 owner 行不自动授予资格 |
| `0148`—`0150` | 历史来源索引、本机原生 Agent 与原观察范围；保原设备、会话和请求 |
| `0151`—`0152` | 自动化定义与本地电脑操作资料，保原执行回执 |
| `0153` | 原 float32 向量移入同库 Vec1；需使用含 Vec1 的构建，不调用新 Embedding |
| `0154`—`0155` | 通知 outbox 原事件与信封/接入键/native 引用分离，本地投递原子发表；保旧已付来源与真实回执 |

`0141`、`0143` 等变更不可逆，部分退役诊断/配置也无法重建。回退优先采取向前修复；旧镜像不能直接运行已升级数据库。确需恢复旧备份时，先保全升级后的新消息、文件和已经发生的效果，单独处理数据一致性，不能用旧库覆盖新事实。

## 3. 管理资格改归 Person

升级前保留旧 `SUPERUSERS` 配置，用目标版本的只读脚本导出历史账号、原 Person 和当前同人根的候选：

```sh
python scripts/prepare_person_authority.py --deployment-root /path/to/yuki --output /private/person-authority-candidates.json
```

原数据库不在部署默认位置时，传 `--database /absolute/path/original.db`。核对候选的真实人物和账号转移历史，然后从 `.env`、Compose/systemd 和实际进程环境删除 `SUPERUSERS`。迁移不自动授予任何人超级管理员资格。

升级后，由已有独立 operator 使用 `yuki admin person list/show` 找到内部 Person ID 和 revision，再逐项执行 `person superuser grant`。没有账号的人可以先创建 Person。账号转移不携带资格，解绑不撤销资格；撤销后重放旧请求也不重新赋权。具体凭据、命令和保原请求的操作顺序见[人物授权迁移](operations/person-admin-control-plane-upgrade.md)。

管理聊天语法改为 `/ai admin <领域> <操作>`。`/ai help` 与 `/ai admin help` 分别显示自助/会话和管理操作；个人 App/ACP 的普通正文不执行平台管理命令。Person 超管可通过原 Desktop 设备凭据使用现行管理 API，独立 operator 凭据仍沿自身 policy；普通配对不授予超管。

独立 character owner 角色已退出：共享名称/头像、SOUL、模型与共享服务器 Computer 操作按当前 Person 超管核验。旧 owner 记录和请求回执保留，不自动升权。可信本地部署沿原显式 setup 请求授予部署者 Person 超管；已有远端部署核对原 Person 后由 operator 显式授予，不能靠重新配对取得权限。

## 4. 插件直接适配当前 API 9.0

Host 只加载精确声明 `plugin_api = "9.0"` 的插件。产品 4.0 与 Plugin API 9.0 是不同版本；改 manifest 数字前先修改真实调用。旧插件需累计适配：

| API 节点 | 需要修改的消费者 |
| --- | --- |
| [4.0 身份](plugin-development/api-4.0-migration.md) | 通用 People/Memory/消息/配置等目标使用内部 Person、Space、Conversation，不再传 QQ 裸号 |
| [5.0 表情](plugin-development/api-5.0-migration.md) | 删除 `ctx.emoji`、旧 emoji 权限/Feature/Hook；媒体发送沿现行 artifact 与消息接口 |
| [6.0 提及](plugin-development/api-6.0-migration.md) | 使用 `CurrentMessage.mentions` 与 `message.current.mentions.v2`，退出旧 mentioned 字段 |
| [7.0 独立会话](plugin-development/api-7.0-migration.md) | 使用 `plugin.agent_session.v2`，删除创建时 metadata/allowed_capabilities、运行时 allowed_capabilities/max_tool_calls 和返回值 tool_calls_used |
| [8.0 自动化](plugin-development/api-8.0-migration.md) | 按当前 TaskSpec 及原 request 身份适配自动化定义 |
| [9.0 消息身份](plugin-development/api-9.0-migration.md) | 使用可空 `native_message_id`，内部任务/回复/历史仍沿 `event_id`；不将原生号重建为业务身份 |

独立会话仍保留逐消息 metadata、历史、实际 `max_model_requests` 和请求计数。Main 的 `ctx.agent.run()` 继续沿自身工具与授权合同，不与无工具独立会话混为一谈。

保存插件原 KV、job、outbox、批准和未完成回执，更新插件后重新发现、审阅实际 manifest 并按所需权限批准。不要扩大批准来绕过接口变化，也不重发旧通知或已付模型请求。随仓库插件以实际 SDK 9.0 示例和 manifest 为准；[Kun](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game)及[GitHub Monitor](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor)使用各自仓库的当前兼容实现。GitHub Monitor 不再随主仓部署包附带。

## 5. 同步持久环境、媒体挂载和文件图库

Bot 更新之外，还要核对宿主 `src/yuki`、Manager unit/依赖、Supervisor 和表情 helper。Manager 从选定 Bot 导出配套源码，以实际 Bot 用户连接验证，不能把 socket 文件存在当成已就绪。环境内执行用户为 root，HOME 保留 `/home/yuki`；旧 apt/pip/npm 安装、服务、HOME/runtime、jobs 和原 rootFS 继续使用。旧环境先保存完整快照，再按[持久环境升级手册](operations/persistent-environment.zh-CN.md#安装升级与回退)明确更换配置或挂载。不能用空基础镜像替代已安装环境，也不能启动两个活动环境共用同一家目录。

- 新的外部原生 Agent 工作沿本人本机 Connector，旧服务器 ACP B 新建/配置链已退役；旧执行事实按原 ID 查询和结算，不再配置第二套执行器。
- Bot 的同一份媒体缓存只读挂载到环境 `/inputs/media-cache`，Manager 的 `YUKI_MANAGER_MEDIA_CACHE` 指向实际宿主目录。既有容器不会自动新增挂载；Bot 仍是原件写入和 24 小时清理 owner。
- 现行表情原图与 JSON 位于 `/workspace/stickers/<platform>/`；helper 与其可重建检索索引同步更新。`0138` 不将旧池自动导入新文件库。旧图如需保留，先按真实文件另行保存，再确认引用后清理专属旧目录，不删除 `data`、workspace 或 artifact 父目录。
- gVisor 自定义 bridge 的 DNS 使用 Manager 显式 `--resolv-conf` 挂载普通 resolver 文件；保留原持久文件和现场可达 DNS，不假设 Docker 的 `127.0.0.11` 可用。

原终端/APT/Python/外部 Agent 按原 request/run 和 completion 继续读取。环境实际重建导致中断时保存真实中断/未知；升级本身不授予重跑资格。挂载检查可以使用原实际 Compose 链中的 `yuki setup environment-check`；实际管理员终端、PTY、包安装、后台恢复、公网和共享只读缓存另行验证。

## 6. 配置和提示文件

| 项目 | 当前配置与处理 |
| --- | --- |
| Compact | `CONTEXT_COMPACT_TRIGGER_TOKENS=96000` 是默认提前整理水位；硬容量由 Profile 的明确 `max_input_tokens` / `context_window_tokens` 与本次输出额度计算 |
| 旧窗口 | 删除 `CONTEXT_WINDOW_TOKENS`、`CONTEXT_WORK_WINDOW_TOKENS`；不将旧值自动搬成水位或模型容量 |
| 旧 Rollup | 清理 `CONVERSATION_ROLLUP_*`、`CONTEXT_COMPACTION_WINDOW_TOKENS`、`WORK_COMPACTION_*` 退役设置及旧 `conversation_compaction` 路由；Memory evidence compaction 的现行设置保留 |
| 网页 | 删除 `WEB_EXTRACT_MAX_RESULTS`、`WEB_SOURCE_MAX_RUNS_PER_CONVERSATION`；网页正文改为显式读取 |
| 代理 | 使用标准 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 和实际 CA 配置，见[代理说明](operations/http-proxy.zh-CN.md) |
| ASR | 明确选择 `chat_completions` 或 `audio_transcriptions`，不按模型名猜协议；见[Provider 合同](architecture/model-providers.md) |
| 表情 | 清理退役 `emoji_replacement` 路由、旧 emoji 专属热配置与 `VISION_MAX_RETRIES`，保留其他模型、视觉/ASR连接和密钥 |

人格原件统一为 `character/SOUL.md`，启动只读取 `SOUL_FILE`；角色名直接认数据库角色资料。旧系统/人格文件、配置变量与占位符已退出。已有部署先比较并明确整理原生效内容，保留用户设定，再按[单 SOUL 升级说明](operations/soul-deployment-20261022.md)切换文件、可写目录挂载和角色初始化。只更新镜像不会替用户选择或合并旧人设。历史已付请求、原主体和回执保持原件。

## 7. 模型配置与本机连接器

旧模型配置先按[一次性离线升级](upgrade-model-config-20261031.md)备份 bundle、私有 sidecar 和一致数据库；停 Bot 后执行 `yuki admin model migrate-legacy`，核对退出码、实际保存 revision 和明确关闭状态，再从真实来源退休已消费的旧变量。部分保存或结果未知先查原文件和行版本，不盲目重跑；辅助连接需要重启时如实重启 Bot，不重置业务身份。

客户端需匹配 Client API revision 9 和实际服务；桌面仓库固定 OpenAPI 来源。连接器 0.4.2/协议4 与服务器配套更新，沿原 device、instance、凭据和 journal 换版；桌面管理的进程不再注册登录自启，托盘期间仍连接，真正退出时停止。原独立安装沿显式接管流程处理。Tailscale/HTTPS 与 SSH 是可选路由，客户端和 Connector 地址分别保存，换线不重新配对。见[本机连接器](operations/local-agent-connector.zh-CN.md)和[Tailscale 指南](operations/tailscale-connection.zh-CN.md)。

## 8. 升级后核对

核对实际 schema `0155`、原 Person/AccountAssignment、会话、Work、累计预算、协议和发送回执，以及原媒体、配置、插件批准和数据。分别验证 operator/Person 超管、普通个人权限、旧请求查询、实际配置的平台连接、Manager 实际用户连通、原环境安装与文件、共享媒体只读挂载、表情 helper 和固定工具；App/ACP/Connector 继续使用原设备与会话。

HTTP 接纳、Main 结束、Work 完成和平台投递是不同回执。断连或确认丢失时查原 operation/request/run，保留 running/unknown，不换编号补跑。真实 Provider 用量、缓存和自然聊天效果按实际样本记录；单个健康检查只说明其对应服务。

架构入口见[当前合同](architecture/README.md)，个人与 ACP 使用见[客户端 API](architecture/client-api.md)，逐项已合并变更见[PR 目录](releases/v4.0.0-prs.md)。
