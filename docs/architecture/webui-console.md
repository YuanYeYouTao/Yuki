# WebUI 管理界面

遵守 [development-contract](development-contract.md) 与
[Control Plane 合同](control-plane-foundation.md)。这是当前源码的实现边界，
不表示已经发布、部署或完成全部管理功能。

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

聊天默认显示最近 40 条账本事件，按内部 ID 排列，可向前翻页、按时间范围或单个
`chat_events.id` 查询；分页绑定会话、筛选和方向。重复接入事件显示其 suppression 状态，
不将入账冒充已经执行主 Agent。显示原事件时间与保存的昵称，不改写历史或清空会话。

元数据与正文分别授权。元数据查询不加载消息正文、ASR、视觉摘要或原 segments。
从消息来源事件、Work 或模型调用可进入执行轨迹，查看实际 Provider 请求、
当轮注入提示词、Provider 返回的可读思考、工具参数/结果以及原 Social 投递回执。
支持 Chat、Responses、Claude、Gemini 的文本展示，并保留完整的脱敏诊断 JSON。
不推断或补写未保存的思考。超期、正文超限、未授权或诊断缺失明确展示。
诊断索引沿用 turn/operation/parent/work/execution/event ID，不反向驱动执行或恢复。
已发送消息按原成功回执核验后记录 `delivered_event_id`，可进入真正执行轮次；
跨会话发送按目标事件的会话核验。原事件来源不改写，旧投递关联不猜测回填。

聊天附件通过原 `ConversationMediaService` 核验会话、generation、starts_after 和
24 小时有效期；读取后再次核验。单次最多 32 MiB、验证摘要、安全打开文件，
不暴露宿主路径或网关 URL。PNG/JPEG/GIF/WebP 按内容识别；其余内容强制附件下载。
长期工作区复用原共享 `WorkspaceStore`：预览最多读取 1 MiB，显示最多 32 KiB
UTF-8；完整下载最多 32 MiB，校验原摘要。大文件与不存在/过期文件明确拒绝。
浏览器没有主 Agent、SELF 或插件执行身份；管理下载权限由 operator 单独声明。

## 已接入的业务页面

| 页面 | 当前接入 |
| --- | --- |
| 手帐/聊天 | 会话、接收/发送账本、附件、事件到执行轨迹、当前人格 |
| 运行状态 | 现有 System/Health；未知健康状态保留 null，不调用模型探测 |
| 模型/用量 | 已加载 Profile/Route、磁盘配置表单与原子保存；实际调用、tokens/缓存/耗时/错误。元数据不显示地址/环境变量引用，文件正文单独授权；密钥与请求头始终不显示 |
| Work/自动化 | 原 Work 预算、等待、子工作、输入、效果/投递意图、检查点与恢复元数据、轨迹；自动化脚本、最近 20 run/200 step；创建、编辑、暂停/恢复、取消、run_now |
| 自主参与 | 当前只读控制器状态与已接纳轮次/最新反馈；不 tick、不重算、不调用 Jev |
| Memory | fact、证据、维护工作、确认/隔离、既有 rebuild/dream/maintain 入口 |
| 插件/MCP | 现有目录与 Manager 状态、批准/启停/doctor、refresh/reconnect |
| 身份/配置 | canonical Person/Space/Presence 与原动作；Registry schema、作用域有效配置、保存/删除覆盖 |
| 工作区/素材 | 共享 artifact 列表、文本预览、授权下载；表情与语音目录及原管理动作 |
| 审计 | 执行诊断、Control/rebuild/dream 原状态与回执、管理审计、Social 投递确定性 |

列表有界分页；“筛选本页”只过滤已读取的一页，不冒充全库搜索。
页面刷新不触发 Agent 唤醒、自动化执行或模型健康调用。run_now 为显式新调度。
复杂的自动化与维护输入当前使用结构化 JSON 编辑，后端仍执行原完整 schema 校验。

## 启动文件的编辑与生效

`read_config_file` / `save_config_file` 只接受 `model_profiles`、`system_prompt`、
`bot_persona` 三个逻辑文件 ID，路径来自原 Settings；不接受浏览器提供的宿主路径。
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

### 容器中的可写启动文件

基础 Compose 的 `/app/config` 为只读。可选 `docker-compose.webui.yml` 把三个启动文件
放到 `/app/webui-config` 可写目录；启用前将当前实际使用的 Profile、System Prompt、
人格原文分别复制到宿主 `./webui-config/model_profiles.toml`、`system_prompt.md`、
`persona.md`，逐一核对存在且内容正确。不要用示例文件覆盖现有配置。
沿现有 Compose 文件列表追加此 overlay；它不发布端口，不设置域名或 operator。

原子替换需要挂载整个目录并允许目录写入，不能只挂载单个文件。页面显示目录不可写时
禁用保存；实际保存仍以服务端文件操作结果和持久回执为准。operator 声明与认证材料
继续通过原只读配置目录设置，不复制到可写目录；配置写权限不授予 operator 修改能力。
`webui-config` 不进入 Git 或镜像构建上下文。保存后按原运维流程重启 Bot 才生效。

## Work 详情的观测边界

`read_work` 只按原 Work UUID 查询，区分元数据与目标/等待条件正文授权。
读取原父子关系与累计预算、journal 的 chain/contract/phase、恢复原因/次数/时间、
已接纳输入的内部事件 ID、效果和投递意图状态。每类最近 20 项，并明确标注还有更多；
`0076` 的索引支持按原 Work 关联查找，不增加业务状态或新调度器。

不读取恢复 journal 的 payload、私有签名、原 authority/source、投递正文、工具私有回执、
transport target 或子任务 brief/result。等待条件只返回审核字段和是否满足，
不会反射任意 matched payload。内容详情沿已授权的原执行诊断查看；诊断过期/缺失
不从私有恢复包补造。页面查询不 tick、不启动模型、不改变等待、预算、输入消费或回执。

## 仍在建设的完整功能

本层不是完整 WebUI 的最终验收。后续沿原领域服务继续建设：

- Work 领域允许的取消/续跑及更多历史关系分页，不以新 run 替代旧执行恢复。
- Jev 完整决策历史、参与参数编辑与关系/自省统计；当前未持久化的数据不能伪造为历史。
- 插件 schema 配置、监控游标/queue/outbox 详情与有证据的处理。
- Memory 主体/证据筛选与关系详情、完整 schema 表单。
- 工作区上传/编辑/删除/终端，与对应文件审批、版本与持久环境合同。

新增这些能力时补齐公共 Query/Command 合同，禁止页面绕过服务直接写 ORM、
读取任意宿主路径、调用任意工具或凭空制造回执。

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
  tests/unit/test_control_config_files.py tests/unit/test_control_work_details.py
```

wheel 构建会包含已生成资源；源码安装启用 WebUI 前必须运行上述构建。
Docker 多阶段构建自动生成页面，运行镜像不包含 Node 工具链。
前端开发服务器默认 `127.0.0.1:18765`，将 `/api/control` 代理到原后端 8080。
生产从同一个后端 `/ui/` 访问；`WEBUI_ORIGIN` 必须等于浏览器实际 origin。
非回环 HTTP origin 拒绝；同源 HTTPS 和反代/域名配置另行部署。

本轮不配置 DNS、Cloudflare、公网端口或生产凭据，不修改线上数据库。
