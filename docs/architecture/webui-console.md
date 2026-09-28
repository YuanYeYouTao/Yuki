# WebUI 管理界面

遵守 [development-contract](development-contract.md) 与
[Control Plane 合同](control-plane-foundation.md)。这是当前源码的实现边界，
描述当前完整管理界面的源码边界；发布、部署和线上验收分别记录。

本轮可用性要求逐项记录在 [WebUI 验收清单](webui-usability-acceptance.md)。

## 装配与前端

React + TypeScript + Vite 的正式前端位于 `frontend/`，构建产物进入
`qq_ai_bot/webui/assets`。同一个 FastAPI 应用通过 `/ui/` 提供静态页面，
`/api/control/` 调用正在运行的 `ApplicationContainer.control_plane`。
没有第二个 Bot、独立数据库、前端业务执行器或通用工具 RPC。

复用经确认的 crescent-grove 手帐布局、五种主题、CSS 和 SVG；许可证随静态资源
进入 wheel 和容器。旧预览脚本和示例业务数据没有进入正式前端。
服务器数据不通过外部 CDN、浏览器存储或第三方脚本分发。

## 登录与请求

- `WEBUI_ENABLED` 默认 false。启用时缺少构建资源明确失败；不降级到空页面。
- `CONTROL_OPERATORS_FILE` 与 CLI 共用服务器声明；角色不隐含授权，浏览器不能提交主体或能力。
- Cookie 保存随机会话标识，HttpOnly、SameSite Strict；HTTPS 使用 Secure 与 `__Host-` 前缀。
  会话存于进程内，重启退出登录；每次 API 请求重新核验服务器声明和凭据摘要。
  变更环境凭据会撤销原会话，变更 TOML 声明按现有装配规则重启加载。
- 登录按直接连接的 peer 限流；最多 128 个有效会话、1024 个限流桶。
  反代信任与公网防护属于部署配置，本实现不相信客户端任意转发头。
- API POST 必须通过 Origin，已登录 POST 还需会话 CSRF；跨站 Fetch 拒绝。
  请求体流式计数，默认最大 1 MiB；未知字段、无效 ID、无权操作明确拒绝。
- 查询/命令是审核过的有限方法表。命令沿用原 request UUID、expected revision、
  领域目标和原持久回执；HTTP header 与 envelope 的 request ID 必须一致。
- 修改提交后禁止自动重发。响应丢失显示结果未知，并按原请求查询回执；
  不重新创建请求、不猜测成功、不清除原 unknown 状态。
- 管理页面与 API 使用 no-store、nosniff、同源 CSP、禁止嵌入与 referrer。
  浏览器仅保存主题偏好，不保存凭据。文本、模型输出、工具结果与文件按文本渲染。

## 消息与执行过程

聊天默认只取最近 40 条账本事件，画面旧到新并落在底部。上滑按绑定会话和筛选的
内部 ID 游标加载更早消息，每次最多 40 条，不计算聊天总量 COUNT；页面最多保留
160 条消息节点，截去较新节点时明确提供“回到最新”入口。时间筛选和指定
`chat_events.id` 定位会重建有界时间线；没有聊天页码或总页数。历史浏览时的新消息
以提示呈现，回到底部后再跟随。重复接入事件显示其 suppression 状态，
不将入账冒充已经执行主 Agent。显示原事件时间与保存的昵称，不改写历史或清空会话。

元数据与正文分别授权。元数据查询不加载消息正文、ASR、视觉摘要或原 segments。
从消息来源事件、Work 或模型调用可进入执行轨迹，查看实际 Provider 请求、
当轮注入提示词、Provider 返回的可读思考、工具参数/结果以及原 Social 投递回执。
支持 Chat、Responses、Claude、Gemini 的文本展示，并保留完整的脱敏诊断 JSON。
不推断或补写未保存的思考。超期、正文超限、未授权或诊断缺失明确展示。
诊断索引沿用 turn/operation/parent/work/execution/event ID，不反向驱动执行或恢复。
已发送消息按原成功回执核验后记录 `delivered_event_id`，可进入真正执行轮次；
跨会话发送按目标事件的会话核验。原事件来源不改写，旧投递关联不猜测回填。
“现在的 Yuki”按所选会话读取同一进程真实进入的 Runner 观察 span 与有期限的
诊断步骤；空闲时只展示最近已记录轮次。孤立的开始记录不能证明当前仍在执行，
Jev 观察也不是主 Agent 运行。消息的“查看本轮”按可信 `source_event_id` 或
`delivered_event_id` 列出关联轮次，零轮次说明证据缺失或未触发，多轮次供人选择，
跨会话投递展示原执行会话。查询只读，不建立新的执行状态或恢复依据。
现场轮询只返回最近有限的根记录；逐轮步骤按原轮次索引读取，仍核验原会话，
不加载压缩正文。该读取不会扩大到整段历史。
“查看本轮”的轮次起点和轨迹列表也通过原轮次索引定位候选记录，再核验会话；
轨迹列表的筛选、计数和分页语义不变。

聊天附件通过原 `ConversationMediaService` 核验会话、generation、starts_after 和
24 小时有效期；读取后再次核验。单次最多 32 MiB、验证摘要、安全打开文件，
不暴露宿主路径或网关 URL。PNG/JPEG/GIF/WebP 按内容识别；其余内容强制附件下载。
出站图片从已入账事件的 `segments_json` 提取原 `artifact_id` 或 `emoji_id`，仅接受
内部 UUID；不将网关 URL 当成浏览器图片来源。聊天附件、出站媒体、表情与工作文件
共用预览组件，缩略图可展开为窗口，过期或读取失败显示具体提示。
共享 artifact 仍走原 `WorkspaceStore`：其快照版本和真实 Linux 工作文件分别展示，
删除 artifact 不代表删除工作文件。文件管理器直接列出 Manager 的持久
`/home/yuki/workspace`；Bot 只读挂载用于预览，写入、移动、删除及交互终端沿原
Manager 控制链执行。二进制预览最多 32 MiB，按内容识别图片，其他类型作为附件下载。
管理后台沿用现有登录与命令回执，不为文件操作增加逐文件审批或新的权限状态。

## 已接入的业务页面

| 页面 | 当前接入 |
| --- | --- |
| 手帐/聊天 | 会话、接收/发送账本、附件、事件到执行轨迹、当前人格 |
| 运行状态 | 现有 System/Health；未知健康状态保留 null，不调用模型探测 |
| 模型/用量 | 已加载 Profile/Route、磁盘配置表单与原子保存；实际调用、tokens/缓存/耗时/错误。元数据不显示地址/环境变量引用，文件正文单独授权；密钥与请求头始终不显示 |
| Work/自动化 | 原 Work 预算、等待、子工作、输入、效果/投递意图、检查点与恢复元数据、轨迹；自动化脚本、执行与步骤历史分页；创建、编辑、暂停/恢复、取消、run_now |
| 自主参与 | 当前控制器状态、已接纳轮次/全部反馈分页、Jev/Host 决策诊断时间线、完整数学参数表单与原热更新文件；查询不 tick、不重算、不调用 Jev |
| Memory | 按原主体/内部证据筛选 fact 与证据、原版本确认/隔离、关系与自省、rebuild 原候选审核/暂停/续跑/提交/重试及 dream/maintain |
| 插件/MCP | 原 schema 配置、GitHub queue/cursor/诊断、通知 outbox；原 manifest/权限复选授权、Manager 启停/doctor、background turn 分页、MCP refresh/reconnect |
| 身份/配置 | canonical Person/Space/Presence、Binding 与三种原路由的详情/注册/编辑/暂停/恢复；Registry schema、作用域有效配置、保存/删除覆盖 |
| 工作区/素材 | 共享 artifact 上传/版本编辑/删除、文本预览、授权下载、原 Linux 文件与终端；表情与语音目录及原管理动作 |
| 审计 | 执行诊断、Control/rebuild/dream 原状态与回执、管理审计、Social 投递确定性 |

人、群、会话优先展示已入账的可读名称；原内部 ID 保留在详情中供复制与排错，
不把名字或平台号码用于执行所有权判断。QQ 头像由后端根据 canonical 绑定代理固定
QQ 图像来源，同源返回有界图片；失败时显示名称首字。
信息列表以真实时间降序、相同时间以原 ID 稳定排序。编号分页由后端对相同查询范围
计算总条数，支持直接跳页；“筛选本页”只过滤已读取的一页，不冒充全库搜索。
页面刷新不触发 Agent 唤醒、自动化执行或模型健康调用。run_now 为显式新调度。
自动化安排/步骤/原能力参数、Memory 维护范围和插件批准使用原 schema 表单；
步骤支持原内置变量与前一步结果模板。能力的 permitted_levels 来自原 Registry.permits，
SELF 不可委托的选项禁用，原执行层仍核验全部权限、场景及额度。
自由键值支持结构化增删与原 JSON 值；不增加通用工具执行入口。

## 启动文件的编辑与生效

`read_config_file` / `save_config_file` 只接受 `model_profiles`、`system_prompt`、
`bot_persona`、`autonomous_model` 四个逻辑文件 ID，路径来自原 Settings；不接受浏览器提供的宿主路径。
`control.config.file.content.read` 与 `control.config.file.mutate` 为独立 operator 能力，
普通配置元数据读取不获得文件正文或写入权限。保存原文不进入审计正文，审计只保留
文件 ID、版本、状态；请求摘要仍绑定原内容，按原 UUID 防重放。

Profile 使用原 ModelProfile schema 显示字段，按原任务表编辑路由。启动、CLI 和保存共用
TOML 文档校验及 Settings 环境变量引用解析；不按型号猜供应商、不热换原 Work 的 Provider。
新增/删除 Profile、修改协议/参数/路由作为一个完整文件校验后保存；引用未解除时不能删除。
密钥只接受环境变量名，不读取变量值。已有自定义 Headers 留在服务器，保存时保留；
浏览器不能读、添加或替换 Headers。需要修改 Headers 时使用服务器配置文件。

人格与模板分别编辑，保留原 `{{YUKI_PERSONA_CORE}}` 组装语义。未配置文件的内联模板
明确不可文件编辑，不擅自创建路径或改写环境配置。Windows CRLF 按与启动加载一致的
换行语义比较；文件正文读取仍保留原文。页面显示磁盘版本、校验结果、与当前加载是否一致；
成功回执 `saved_pending_restart` 表示已保存，需重启加载，不表示已经热生效。

文件最大 256 KiB，安全打开普通单链接文件；不读符号链接，不创建父目录。保存核对
读取到的摘要版本，短 SQLite intent 提交后才在事务外执行文件校验和同目录原子替换，
沿原 Control 持久回执收口；线程写入取消时保留所有权直到 OS 写入结束。
同一应用内按原资源围栏串行处理。手工编辑器不参与该围栏，应避免同时写同一文件；
替换前再次核对原字节，不能声称跨所有外部编辑器实现原子 compare-and-swap。
文件替换或最终回执提交后结果未知，保留原 request/unknown，不自动保存第二次。
不会重写聊天历史、清空会话或重跑已有 Work。

`autonomous_model` 是原控制器的热更新 JSON 参数文件。完整数值 schema 直接来自
固定独立库，页面按语义分组显示；不复制默认值/校验边界或重算机会率。
`apply_mode=hot_reload`，保存回执为 `saved_pending_reload`；磁盘与当前实际生效参数
分别显示。查询不刷新控制器，下一轮原采样才应用；原请求回执与 unknown 围栏不变。

### 容器中的可写启动文件

基础 Compose 的 `/app/config` 为只读。可选 `docker-compose.webui.yml` 把三个启动文件
放到 `/app/webui-config` 可写目录；启用前将当前实际使用的 Profile、System Prompt、
人格原文分别复制到宿主 `./webui-config/model_profiles.toml`、`system_prompt.md`、
`persona.md`，逐一核对存在且内容正确。不要用示例文件覆盖现有配置。
现有自主模型挂载参数也需复制为 `./webui-config/autonomous-model.json`；没有原覆盖
文件时可保持不存在并使用默认值。overlay 将原热更新路径指向同一可写目录。
沿现有 Compose 文件列表追加此 overlay；它不发布端口，不设置域名或 operator。

原子替换需要挂载整个目录并允许目录写入，不能只挂载单个文件。页面显示目录不可写时
禁用保存；实际保存仍以服务端文件操作结果和持久回执为准。operator 声明与认证材料
继续通过原只读配置目录设置，不复制到可写目录；配置写权限不授予 operator 修改能力。
`webui-config` 不进入 Git 或镜像构建上下文。保存后按原运维流程重启 Bot 才生效。

## Work 详情、历史与操作

`read_work` 只按原 Work UUID 查询，区分元数据与目标正文授权。
读取原父子关系与累计预算、journal 的 chain/contract/phase、恢复原因/次数/时间。
`list_work_history` 分别分页子工作、输入、等待、效果及投递意图；按不变的登记时间与
原 ID 排序，游标绑定 Work、集合及正文范围。删除原固定 20 项快照；
`0076` 的索引支持按原 Work 关联查找，不增加业务状态或新调度器。

不读取恢复 journal 的 payload、私有签名、原 authority/source、投递正文、工具私有回执、
transport target 或子任务 brief/result。等待条件只返回审核字段和是否满足，
不会反射任意 matched payload。内容详情沿已授权的原执行诊断查看；诊断过期/缺失
不从私有恢复包补造。页面查询不 tick、不启动模型、不改变等待、预算、输入消费或回执。

`mutate_work` 独立要求 `control.work.mutate`，按原 UUID/revision/request 写原 Work
与管理回执。取消根工作只取消其工作树、待处理输入及等待，撤销子工作的租约；
不取消其他根工作、不撤销整个会话租约。取消单个子工作向原父 mailbox 登记实际取消
状态。新的工具/模型/投递额度登记核验原工作仍有效；已入执行边界的外部效果可能继续
完成，保留迟到与未知回执，不承诺远程强制撤销。取消退出不生成额外失败通知。

续跑仅将暂停/等待用户的原工作排入现有 scheduler。原 source、journal、预算、
失败次数与恢复时间保持；执行仍由原入口重新核验权限。活跃租约、有效信号等待、
树内未决效果/未知投递、失效 generation、缺失必要 journal 拒绝续跑。
自动化 Work 仍归原 run/step worker，不通过此入口建立独立恢复链。

## 自动化执行历史

`read_automation` 返回原定义与版本；删除原固定 20 run/200 step 快照。
`list_automation_runs` 与 `list_automation_steps` 按原内部记录 ID 分页，游标绑定
自动化及所选执行；步骤查询核验执行属于该自动化，不按平台目标重新推断归属。
页面可选择一次执行查看步骤，也可浏览全部步骤。

历史元数据独立使用 `control.automation.read`，不需要脚本正文权限。
SQL 只读取状态、时间、能力、计数和错误类别，不加载 authority snapshot、私有
输入/输出摘要或结果正文。查询不重跑、补发或调用模型；执行内容沿原授权诊断查看。

## Jev 与 Host 决策诊断

原观察适配器的实际 Snapshot、返回概率、无效维度、usage 和耗时沿既有 Recorder 保存；
原 Host 接纳记录 proposal、owner、参数版本与实际 accepted/busy/rejected 结果。
页面按会话及 origin 分页，正文授权后显示来源、全部概率与完整诊断；没有记录的
旧历史、超期或隐私删除记录不回填。没有 usage 时保留未知，不从文本估算 tokens。
它是有保留期限的诊断历史，不能用于重放或恢复，也不冒充长期计费统计。

## 插件配置与只读状态

`read_plugin_configuration` / `configure_plugin` 使用 Manager 已批准并注册的原 Pydantic
schema；查询不发现、导入或启用插件。没有已加载 schema 时明确不可用。表单与 Profile
共用递归 schema 字段组件，支持仓库、分支、事件类型和嵌套通知目标的增删。自由结构键值保留原 JSON 类型，不把对象转换成普通字符串。

配置沿用 `plugin_config_values`，global、user、group 各范围独立核对原行摘要版本；
user/group 必须提供 live canonical Person/Space UUID，不能拿平台 ID 重建归属。
完整 schema 的字段边界和跨字段 validator 在写事务前执行；短 immediate 事务重新
核对版本及所有权后整体保存，原行版本递增。配置最多 256 KiB/256 个键，不再声明的
旧键不投影，保存时删除；Secret 始终属于原 Secrets 服务。保存回执为 `saved`，
是否立即生效由插件自身的读取逻辑决定；schema 默认值预览不证明当前运行值。
配置正文/观察需 `control.plugin.config.content.read`，保存需独立
`control.plugin.config.mutate`；审计不记录配置值，原 UUID/unknown 围栏继续生效。

SDK 可选 `ObservablePlugin.observe(context, request)` 返回插件自己维护的 JSON 投影。
Host 只传插件自身的全局 config.get 与 storage.get，保留其批准权限；不提供写入、
HTTP、Secret、通知或 Agent 方法。工厂和 callback 合计最多 5 秒，结果最多 64 KiB，
不持有 Manager lock 或 SQLite writer；返回前核验原运行实例。不支持的插件明确不可用。
这是可信进程内插件的只读调用合同，不能声称隔离任意 Python 代码；已有生命周期不变。

GitHub Monitor 按原配置仓库分页，读取原 authoritative queue 和诊断类别，显示
accepted/committed cursor、轮询/成功时间、暂停、失败、限流、pending/inflight 数量及
有界投递元数据。不存在的队列保留未知，损坏状态明确报错；不迁移旧状态、不请求 GitHub、
不触发轮询/封口/重发。各读取是即时观测，不保证跨仓库或跨 config/state 的事务快照。
不返回 prepared 通知原文、原 payload、媒体句柄或请求私有材料；GitHub 来源 ID 明确
标为 `github_event_id`，Host 因果仍使用原 `source_event_id`（`chat_events.id`）。

`list_plugin_outbox` 有界分页绑定插件，SQL 只加载元数据，不加载通知正文/媒体/平台目标；
平台回执只显示是否存在。重试复用原 `mutate_plugin(action=retry)`：仅 failed、
无平台回执、确认未发送的三种失败类别且剩余预算/原内部归属成立；执行时重新核验。
结果 unknown/uncertain、已发送、预算耗尽或缺少原 canonical owner 不进入 live 队列。
历史缺少归属返回持久 `state_mismatch`，不按平台目标重新寻找主人。

## Memory、关系、自省与维护

事实/证据按 canonical Person/Space、scope、kind、status、原 event/tool receipt/fact ID
筛选并进行 keyset 分页。游标绑定全部筛选及正文授权范围；无正文能力的 SQL 不读取
内容、证据原文或旧平台身份。关系编辑沿原 Person 行/revision，共用原关系写算法及审计。
自省统计取原 run/cycle/request/result，不补造未记录的 usage，不触发处理或模型调用。

维护详情仅投影原水位、计数、版本和经过原类型校验的计划统计；不暴露私有 selection。
重建候选可按原 run 分页、读取已授权内容/证据、批准或拒绝后提交。批量审核严格核验
原 run 与全部 pending proposal ID。CLI 与 Control 共用 pause/resume/commit/retry 状态机，
重试只重置原失败项到暂停，不重置成功项、水位或已消耗请求计数，随后显式续跑。
Dream 重试复用原失败/过期簇恢复，并保留原成功簇与 attempt budget。

## 共享工作区与终端

`control.workspace.mutate` 提供原 artifact 上传、版本编辑和删除，上传最多 640 KiB，
正文预览截断时不允许拿片段覆盖文件。共享 artifact 仍为不可变版本快照；发布及 checkout
使用原 WorkspaceService/WorkspaceStore，成功快照但导入失败的状态单独显示。

`control.environment.file.mutate` 按原 FileWorkspace 路径/schema 和 SHA 版本执行有限文件动作，
不读取宿主路径；`control.terminal.mutate` 复用原 SandboxClient 的同一 Manager/socket/Linux
环境。页面命令来自固定 exec/write/control 方法，不提供任意工具 RPC。文件/终端内容有
独立读取权限，查询不执行命令；输出按原 run UUID/byte cursor 读取并标明截断或已丢失内容。

文件管理器的主目录是原环境中的 `/workspace`，不是已发布 artifact 列表。目录层级、分页、
打开、文本编辑、重命名/移动、空目录或文件删除都作用于原工作文件；更新和删除文件核对原
SHA。浏览器上传二进制文件使用有限的 `workspace_upload` Manager 动作，最多 4 MiB，
当前目录同名文件返回版本冲突；不会先建 artifact 或通过 checkout 猜测导入路径。
Bot 必须将 Manager 实际的 `home/workspace` 只读挂载到本容器，并把
`WEBUI_WORKSPACE_DIRECTORY` 指向该挂载点，供编号目录和媒体预览读取。原
`./workspace` artifact 目录与 Manager 工作区不同，不能拿它替代。写操作仍走同一个
Manager，无额外逐文件审批或权限状态。
仅这一路 WebUI 命令和 socket 请求允许 6 MiB 帧，其他请求继续使用原限额。已发布
artifact 作为独立快照只读展示，删除快照不再冒充删除工作文件。终端页面连接原 tty，
击键使用原 Control 请求回执顺序发送；断线按原 run 和请求查询，未知结果不自动重输。
上线时先同步宿主机 `yuki-sandbox` Manager 的 `persistent.py`、`manager.py`、`client.py`
和 `workspace/files.py`，再替换 Bot 镜像；仅更新 Bot 会让旧 Manager 拒绝
`workspace_upload`。Manager 重启沿原回执恢复，保留运行中的 `yuki-environment` 容器和
持久工作区，SnowLuma 不参与替换。

终端 request token 为 `control:<authenticated principal UUID>:<original request UUID>`，
Manager 的原持久启动标记和 run UUID 保留。操作先提交短 Control intent，再在 SQLite
事务外发送；完成事件核验原 intent/actor/request/run 并追加去重的管理审计，不生成聊天
事件、不创建 Agent continuation、不唤醒 Yuki、不新建 worker 或数据库表。
按原 request 查询 Manager 时主体来自认证上下文，浏览器不能替换主体。响应丢失保留
Control unknown 围栏，不自动重跑、重发或把晚到完成记录伪造成原成功回执。

插件队列维护使用原 Manager 启停与有证据的 outbox retry；队列观测不迁移/封口/补发。
background turn 只分页实际元数据，不读取 agent_intent、生成正文或平台目标。
原模型/诊断未保存的历史、过期媒体、真实环境未部署的功能明确显示缺失或不可用。

## 本地开发与构建

Node.js 24；先构建前端，再运行包含 HTTP 静态资源的测试，避免 `emptyOutDir`
清空生成目录时与测试启动竞争。

```sh
cd frontend
npm ci
npm run format:check
npm run lint
npm run test
npm run build
cd ..
uv sync --frozen --extra dev
uv run pytest tests/unit/test_webui_http.py tests/unit/test_webui_activity.py \
  tests/unit/test_control_config_files.py tests/unit/test_control_work_details.py \
  tests/unit/test_control_automation_history.py \
  tests/unit/test_control_plugin_configuration.py plugins/github-monitor/tests
```

wheel 构建会包含已生成资源；源码安装启用 WebUI 前必须运行上述构建。
Docker 多阶段构建自动生成页面，运行镜像不包含 Node 工具链。
前端开发服务器默认 `127.0.0.1:18765`，将 `/api/control` 代理到原后端 8080。
生产从同一个后端 `/ui/` 访问；`WEBUI_ORIGIN` 必须等于浏览器实际 origin。
非回环 HTTP origin 拒绝；同源 HTTPS 和反代/域名配置另行部署。

本轮不配置 DNS、Cloudflare、公网端口或生产凭据，不修改线上数据库。
