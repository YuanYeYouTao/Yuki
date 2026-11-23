# Yuki 订阅 OAuth 登录（Sign in with ChatGPT 计划用量）调研与任务书（2026-11-23）

状态：**调研完成，任务书第 1 版，待所有者决定；只写文档，未实施**。对应 issue #493。所有者在 issue 下的意见是“有点风险，先放着吧”，本书不改变这个状态：§10 Q1（做不做、做到哪一步）**必须所有者明确表态**，不按推荐默认执行；其余各题标【推荐】，Q1 放行后按推荐执行。

基线 origin/main `a3b59c8e`，fallback 链（#713）已实施。路径省略 `src/yuki/`，行号以 `a3b59c8e` 为准。标记沿用近期任务书：【实证】源码或官方文档可复核；【假设】未经真实账户、真实请求核实；【解读】官方文字有多种读法时本书的读法；【决定】本书定下的边界；【推荐】待决问题的默认选项。官方文档均于 2026-11-23 读取，条款引文保留英文原文。本仓是公开仓库：本书和后续实现、测试、PR 都不得出现任何真实 token、issued client ID、账户邮箱或主机 ID。

## 0. 结论

1. **官方路线存在**【实证】。OpenAI 的 Sign in with ChatGPT（SIWC）提供“ChatGPT plan usage”：开源应用经用户 OAuth 授权，用用户的 ChatGPT 计划额度调用公共 `POST https://api.openai.com/v1/responses`，不需要 client secret 和合作方 API Key。官方 cookbook 写明发布时开放给 “open-source projects, personal projects that run locally, and selected private apps”。Yuki 是公开仓库、所有者自部署、不收费，**应用资格上**符合条件。
2. **条款不允许 Yuki 用所有者的订阅回答别人**【实证】。《Sign in with ChatGPT Terms》（2026-11-04 生效）§3.1 明确禁止：
   - “Using one user’s subscription to fulfill another user’s requests.”
   - “Requests must be for the authenticated user and arise from their activity or expressly authorized automations or background processes.”
   - “Another user’s activity must not trigger requests to the authenticated user’s account.”

   Yuki 的主要用法是 QQ、TG、飞书群里的统一主 Agent，绝大多数请求由其他人的消息触发；记忆提取、Dream 这些后台任务也是由群消息累积触发的。所以**不能把订阅 Profile 直接挂成 `chat_agent` 的全局路由**。合规的范围只有“授权人本人”发起的请求，比如所有者和 Yuki 的私聊、所有者自己的设备会话，以及所有者明确授权的自动化。
3. **不借用本机 Codex 的登录**【实证＋解读】。Codex CLI 用的是 Codex 自己固定的 OAuth 客户端，scope 里没有 `chatgpt.tokens.use.direct`，请求发往 `chatgpt.com/backend-api/codex`。条款 §1 要求 token 只能经官方登录流程取得、用自己应用的名字、不得冒充其他应用；§3.1 要求 “Use the user’s plan only for the application they connected”。推理文档也明确写了 “do not point it at ChatGPT’s `backend-api` endpoints”。另外 refresh token 每次刷新都会轮换，Yuki 和 Codex 刷新同一个会话会互相把对方挤下线。这条路线只作为备选记入 §4，不实施。
4. 因此本书的方案是：**订阅凭据作为 Profile 的一种认证方式；Bot 持有凭据并负责刷新；在执行处核验请求是否由授权人本人发起，不是就在派发前跳过该 Profile（0 次物理请求），交给所有者显式配置的下一跳，或者直接报错**。这一步核验的是身份事实，不看内容，与 #713 的能力跳过属于同一类。
5. 订阅额度和所有者自己在 ChatGPT、Codex 上的额度共用（help center：“the included ChatGPT Work and Codex allowance”）。Yuki 用订阅会挤占所有者委派 Codex 时的额度，见 Q9。

## 1. 要解决什么，不解决什么

### 1.1 目标（issue #493）

所有者在 Yuki 中用浏览器完成 ChatGPT 登录，把订阅可用的模型绑到模型连接上，不用另配 API Key。上下文、工具、记忆、Work 和消息发送仍由 Yuki 执行；Responses 适配补齐订阅路线要求的 SSE、请求投影和返回语义。

### 1.2 本书【决定】

1. 走官方 SIWC 计划用量路线（§2）。注册、登录、刷新、退出全部按官方合同执行，应用名用 Yuki 自己的名字。
2. 凭据是 Profile 的一种认证方式（`auth = "chatgpt_plan"`）。Profile 只保存账户引用，token 存在模型文件旁一个独立的私有文件里，不进 TOML、不进 API Key sidecar、不进 SQLite（Q5）。
3. Bot 是唯一持有并刷新 token 的一方。Desktop 和 CLI 只负责把浏览器回调交给 Bot，不保存 token（§6）。
4. 执行处做“授权人范围”核验：只有能证明请求由凭据所属 Person 本人发起时，才派发到订阅 Profile（§7，Q2、Q3）。
5. fallback 沿 #713：只在确定未受理时切换。订阅的额度和资格错误在流开始前以 HTTP 429/403/503 返回时，按 #713 现有分类触发；流开始后才出现的 `response.failed` 本版不触发（§8，Q6）。
6. 不按内容切换 Provider，也不新增付费调用。订阅 Profile 和付费 Profile 之间是否互为备用，完全由所有者在 `[fallbacks]` 里显式配置（Q7）。

### 1.3 本书不做

- 不借用 `~/.codex/auth.json` 或 Codex 的客户端身份，也不调用 `chatgpt.com/backend-api`（§4）。
- 不让其他人的消息、群聊后台任务或插件消耗所有者的订阅（§7）。
- 不建账户池，不做多账户轮换。条款 §3.1 明确禁止用 “rotating accounts” 绕过额度限制。
- 不做逐字流式展示，不提前执行增量工具参数，也不新建事件存储。SSE 只在 Provider 内部消费，最终仍返回完整的 `ChatResponse`。
- 不做 Yuki 自己的额度统计或限额。额度上限由所有者在 ChatGPT 设置里给 Yuki 这个 app 设置（Q9）。
- 不在 Yuki 内收费，也不为订阅路线设置付费门槛。条款 §3.1 “No charge” 本来就要求这一点。
- 不做 WebSocket 模式，也不用 `previous_response_id`。

## 2. 官方路线调研【实证，2026-11-23 读取】

来源：`developers.openai.com/siwc`、`/siwc/quickstart`、`/siwc/token-sharing-open-source` 及其子页 `sign-in`、`profiles-and-sessions`、`models-and-inference`、`preview-limitations`、`errors-and-recovery`、`token-reference`、`self-hosted-vms`、`codex-app-server`，以及 `/siwc/ui-ux-guidelines`、`/cookbook/articles/sign-in-with-chatgpt`、`openai.com/policies/sign-in-with-chatgpt-terms`、`openai.com/policies/terms-of-use`、help center 文章 20001542。

### 2.1 资格与条款

| 项 | 官方说法 | 对 Yuki 的含义 |
| --- | --- | --- |
| 应用资格 | cookbook：“available at launch to open-source projects, personal projects that run locally, and selected private apps”；overview：“If you're interested in offering it in a paid or remotely hosted app, complete the interest form.” | 所有者自部署的开源 Yuki 符合条件。若以后变成面向他人的托管服务，需要另走 interest form |
| 用户资格 | help center：“available with Go, Plus, Pro, and Business”；quickstart、cookbook 写的是 “Plus and Pro”。两处不一致 | 以真实授权时的结果为准；拿不到 `chatgpt.tokens.use.direct` 就不派发 |
| Business 管理员 | help center：“Your organization must allow plan usage for that app.” 管理员在 “Tenant settings > External access” 里把 “Use ChatGPT plan” 设为 Allowed。Enterprise、Edu 未提及 | Business 账户可能登录成功但没有用量权限，见 F5 |
| 应用身份（Terms §1） | “Use your app’s own name during sign-in and activation.” “Do not impersonate OpenAI, another application or entity, or another open-source project.” “Obtain and use SIWC access and refresh tokens … only through OpenAI’s supported sign-in flow and as authorized by the user.” | 注册时 `agent_name_hint` 用 Yuki；不借用 Codex 的身份 |
| 存放（Terms §1） | “Any persistent storage of Authentication Tokens must be local and under the user’s control, not in a remote or managed environment.” | 【解读】所有者自己控制的主机或 VM 属于 “under the user’s control”；官方 self-hosted VM 文档也允许把凭据转到用户自己的 VM。如果 Bot 部署在别人托管的平台上，就不符合，见 Q5 |
| 他人请求（Terms §3.1） | 见 §0 第 2 条的三句引文 | 决定性约束，见 §7 |
| 后台任务（Terms §3.1） | “expressly authorized automations or background processes. Obtain express consent before background use.” | 后台用途必须由所有者显式同意，并且触发源不能是其他人，见 Q3 |
| 仅限所连应用（Terms §3.1） | “Use the user’s plan only for the application they connected. Do not provide general-purpose API access for other tools or unrelated requests.” | token 不得交给插件、委派出去的外部 Agent 或连接器，也不能开放成通用代理 |
| 绕过额度（Terms §3.1） | “Creating multiple accounts, splitting usage, rotating accounts, or otherwise bypassing usage limits.” “Pooling, transferring, reselling, gifting, or sharing ChatGPT plan usage or Authentication Tokens” | 一条 fallback 链里不得出现两个不同账户的订阅 Profile（§8.3）；不做账户池 |
| 不收费（Terms §3.1） | “Users must be able to use their ChatGPT plan through SIWC without paying you…” | Yuki 不收费，满足 |
| 执行（Terms §7） | OpenAI 可监控，并在违规或有滥用风险时暂停或停用该应用的访问 | 违规的后果是账户或应用被停用，所以 §7 的闸门不是可选项 |
| 通用使用条款（Terms of Use，2026-01-01 生效） | “You may not share your account credentials or make your account available to anyone else” | 与 §3.1 一致，订阅不能拿来服务他人 |

### 2.2 注册与登录

- 首次注册：`client_id=dynamic_agent_client`，`agent_name_hint=<应用名>`，`ext_agent_host_id=<本主机 ID>`。回调返回 issued `client_id`（形如 `oaiapp_…`），之后的登录和刷新都使用它。“`dynamic_agent_client` is the first-time registration entrypoint, not the client ID to save or use for token exchange.” 不需要 client secret。
- 主机 ID：每台主机一个，稳定且不透明。推荐 `urn:ietf:params:oauth:jwk-thumbprint:…`，也可以用 `urn:uuid:…` 或 `did:key:…`。不得用邮箱或用户 ID；它不是认证凭据。
- 授权端点：`https://auth.openai.com/api/accounts/authorize`。参数包括 `client_id`、`agent_name_hint`（仅首次）、`ext_agent_host_id`、`id_token_hint`、`login_hint`、`response_type=code`、`redirect_uri`、`scope`、`resource`、`state`、`nonce`、`code_challenge_method=S256`、`code_challenge`。每次尝试都重新生成 state、nonce 和 PKCE verifier。
- scope：身份部分是 `openid profile email`，计划用量部分是 `offline_access resource.invoke chatgpt.tokens.use.direct`；`resource=https://api.openai.com/v1`。必须检查实际授予的 scope 中包含 `chatgpt.tokens.use.direct`：“A valid ID token alone does not authorize ChatGPT plan usage.”
- 回调地址：只允许 HTTP loopback `127.0.0.1`（例如 `http://127.0.0.1:1455/auth/callback`），不能换成 `localhost`。端口可以变，scheme、host 和 path 不能变。先启动监听再打开浏览器。授权请求和换码请求的 `redirect_uri` 必须完全一致。
- 回调结果：首次注册时回调带回 `code`、`state` 和 issued `client_id`；重新授权时可能不带 `client_id`。如果带回的 `client_id` 与已存注册不同，应拒绝这次结果，不能用它替换原注册。`error=access_denied` 时先校验 state，再停止，不换码。
- 换码：向 `https://auth.openai.com/api/accounts/oauth/token` POST 表单，字段为 `grant_type=authorization_code`、`client_id`、`code`、`code_verifier`、`redirect_uri`、`resource`。遇到 `invalid_grant` 时丢弃这个 code，重新发起授权。
- ID token：用 OpenAI JWKS 验签，校验 iss、aud（issued client ID）、exp 和 nonce。用 `sub` 作为账户身份；“Email and `sub` are not workspace identifiers”，所以身份要和 issued client ID 一起保存。

### 2.3 Token 生命周期

- 访问令牌 1 小时（`expires_in: 3600`）。refresh token 30 天，每次刷新都轮换出一个新 refresh token，新 token 重新计 30 天。token 响应里还有 `earliest_refresh_at`，文档没有说明它的语义【假设：不早于这个时间刷新】。
- 刷新：向同一个 token 端点 POST `grant_type=refresh_token`、`client_id`、`refresh_token`、`resource`，不带 `scope`。“serialize refreshes for the same session”。刷新成功后，访问令牌、到期时间、scope 和新 refresh token 要一起替换。
- 终态刷新错误：`invalid_grant`、`invalid_refresh_token`、`token_expired`、`refresh_token_expired`、`refresh_token_invalidated`、`refresh_token_reused`。遇到这些就清掉不可用的 token，用已存的 issued client ID 重新登录。`invalid_client` 表示客户端配置有问题。
- 退出：从 `https://auth.openai.com/.well-known/openid-configuration` 读出 `revocation_endpoint`，POST `token=<refresh_token>`、`token_type_hint=refresh_token`、`client_id`。返回空的 200 即成功，token 已失效时也返回 200。网络错误或 5xx 时退避重试；如果没确认撤销就清掉了本地 token，要告诉用户远端撤销没有确认。退出后保留账户与 client 的映射和主机 ID，供下次登录使用。
- 用户在 ChatGPT 设置里断开连接时，OpenAI 不会通知应用，只有等请求或刷新失败才会发现。不要因为暂时的网络故障清除凭据。
- 文件：“Write files atomically with owner-only permissions (`0600` on Unix), and never commit or log them.”

### 2.4 推理合同（与标准 Responses 的差异）

| 项 | 订阅路线 | 标准 OpenAI API Key 路线 |
| --- | --- | --- |
| 端点 | `POST https://api.openai.com/v1/responses`，`Authorization: Bearer <OAuth access token>` | 同一端点，用 API Key |
| `stream` | 必须为 `true`；只有收到 `response.completed` 才算成功 | 可选 |
| `store` | 必须为 `false` | 可选（Yuki 已固定为 false） |
| 历史 | 每次请求都在 `input` 里带上所需上下文；HTTP 下不用 `previous_response_id` | 可选 |
| 指令 | 用 `instructions` 或 developer 消息；“Explicit system-role message items are rejected.” | 两者都接受 |
| 不支持的参数 | `background`、`conversation`、`max_output_tokens`、`max_tool_calls`、`metadata`、`moderation`、`multi_agent`、`prompt`、`prompt_cache_retention`、`safety_identifier`、`temperature`、`top_logprobs`、`top_p`、`truncation`、`user` | 都支持 |
| 函数工具 | 必须放进 namespace，或者通过 `additional_tools` 输入项提供；`programmatic_tool_calling` 不能放在顶层 `tools` | 可以直接平铺在顶层 |
| 不支持的工具 | 图像生成、file search、Code Interpreter、原生 computer use、托管 MCP/connectors、Responses `tool_search` | 支持 |
| 原生搜索 | “subject to model and account/workspace policy” | 按模型支持情况 |
| 输入 | 所选模型接受时支持文本、图片和文件；不支持音频、视频、Files 上传 API 和转写 API | 都支持 |
| 模型目录 | `GET /v1/models`，返回的 `models` 数组中保留 `visibility == "list"` 的项，显示 `display_name`，请求时把 `slug` 作为 `model`；切换账户后要重新读取 | 标准 `data` 数组 |

### 2.5 错误码

| 错误 | 时机 | 官方处理建议 |
| --- | --- | --- |
| HTTP 401 | 流开始前 | 检查所选账户和授予的 scope |
| HTTP 403 | 流开始前 | 政策或权限检查（如服务地区）拦截；如实告知限制 |
| HTTP 503 | 流开始前 | 直连路由不可用；保留凭据，有界退避 |
| `subscription_sharing_user_not_eligible`（403） | 结构化错误 | 说明限制；不重复请求，也不反复走 OAuth |
| `subscription_sharing_usage_limit_exceeded`（429） | 结构化错误；也可能在流开始后以 `response.failed` 到达 | 暂停新的计划用量请求，引导用户到 ChatGPT Settings → Usage；不要据此推断额度已空或何时重置 |
| `subscription_sharing_usage_unavailable`（503） | 同上 | 保留凭据，稍后有界退避重试 |
| `subscription_sharing_unsupported_capability`（400） | 结构化错误 | 根据 `error.param` 删掉不支持的输入、工具或参数；不要原样重发 |
| `subscription_sharing_route_not_supported`（403） | 结构化错误 | 检查 HTTP 方法和端点 |
| `subscription_sharing_invalid_user`（401） | 结构化错误 | 保留 request ID 诊断；只有确认撤销或刷新进入终态后才要求重新登录 |
| `chatpass_v2_scope_not_authorized`、`chatpass_v2_invalid_authorization_context`（403） | 结构化错误 | 检查 client 和授权配置；不重试，不改换计费路径 |
| `subscription_sharing_user_unavailable`（503） | 结构化错误 | 有界退避 |

“ChatGPT plan errors stop inference, and OpenAI doesn’t silently switch billing paths.” 官方唯一给出的替代做法，是让用户自己选择另一条计费路径（例如自己的 API Key）。

### 2.6 额度

- help center：应用请求消耗的是 “the included ChatGPT Work and Codex allowance or applicable credits”，计入用户的整体周用量。用户可以在 Settings → Usage 给每个应用设周上限：“An app limit is a cap - not a separate pool of usage.”
- 超出计划额度后改用 credits 的功能默认关闭；只有把应用的上限设为 100% 才会使用 credits。**credits 需要付费**，见 Q9。
- 协调者提到 “Plus 的 5 小时限额在所有 app 之间共享”。我读过的官方页面只提到周用量，没有提到 5 小时窗口，**这一条未核实**。
- 文档没有说明计划用量请求是否返回 `usage` 字段【假设：返回，按上游真实字段记录，缺失就记为未知】。

### 2.7 UI 要求（`/siwc/ui-ux-guidelines`）

- 登录按钮文案为 “Continue with ChatGPT”。
- 请求使用计划额度时，在模型选择器或账户控件附近显示 “Using ChatGPT plan”，旁边放 **Manage usage** 链接，指向 `https://chatgpt.com/settings/usage`。账户设置里也要保留这个入口，让用户在两次请求之间也能找到。
- 遇到额度错误时，以 “Manage usage” 作为主要操作，建议文案是 “Usage limit reached / Review your plan or this app’s limit in ChatGPT settings.”
- 首次启用计划用量时提示一次 “You’re using your ChatGPT plan”，之后不再提示。
- 唯一一条带 “must” 的要求是 “Your app must clearly show which of its plans support ChatGPT plan usage.” Yuki 没有付费档位，这条不适用。

### 2.8 自托管 VM

官方流程：VM 先生成并持久化自己的 `ext_agent_host_id`；用户在本机用同一个工具、client、用户和 workspace 完成 OAuth；经 SSH 等安全通道把凭据文件复制到 VM，复制时保留 VM 自己的主机 ID；之后由 VM 独自负责刷新。“A `127.0.0.1` callback reaches the computer running the browser, not the remote VM.” 已知局限：“Host-specific usage attribution and revocation of ChatGPT plan access for transferred sessions are not yet available.” 文档没有提到远端 HTTPS 回调或 device code，本书不假设这两种方式可用。

## 3. Yuki 现状【实证，`a3b59c8e`】

| 位置 | 现状 | 订阅路线需要的改动 |
| --- | --- | --- |
| `model_runtime/models.py:97-122` `ModelProfile` | 认证只有静态 `api_key_env`；`headers` 不允许覆盖 `authorization` | 增加 `auth` 类型和账户引用 |
| `model_runtime/pool.py:127-158` | 构造 Provider 时取一次 Key，按 `(profile_id, timeout)` 缓存 Provider；连接池按 `(provider, base_url, api_key_env)` 共享 | 凭据改为每次派发时取当前值；连接池按账户区分 |
| `llm/json_http.py:143-147`、`:211-230`、`:332-370` | `Authorization` 在 `_prepare` 里写进请求；同一个 prepared request 被重试复用 | 每次物理 attempt 前重新取 token，否则刷新后的重试仍会带旧 Bearer |
| `llm/json_http.py:240-330` | `response.json()` 读取完整 body，诊断记录 `response.content` | 增加 SSE 消费：读到终态后再交给现有解析器 |
| `llm/deepseek_responses.py:86` | `"stream": False` | 订阅路线为 true |
| `llm/deepseek_responses.py:90-91` | 发送 `max_output_tokens` | 订阅路线不发 |
| `llm/deepseek_responses.py:95-105` | 函数工具平铺在顶层 | 订阅路线改为 namespace |
| `llm/deepseek_responses.py:235-237` | 尾部控制消息保持原 role，包括 `system` | 订阅路线把 system 改投影成 developer，位置不变 |
| `llm/deepseek_responses.py:306-308` | `status: failed` 只抛出泛化的 `LLMUnavailableError` | 保留上游错误码和 request ID |
| `llm/deepseek_responses.py:398-416` | 只读 `output_text`，合法的 `refusal` 内容会被当成空回答 | 解析 refusal（这一点三条 Responses 路线都受益） |
| `llm/openai_responses.py:11-37` | 继承 DeepSeek 实现；设置 `strict:false`、`store:false`、`include`、`tool_choice` | 订阅方言单独建子类，不改动 DeepSeek 和 API Key 路线 |
| `llm/json_http.py:51-59`、`model_runtime/executor.py:71-79` | #713 未受理集合：连接、代理、池、5xx、429、401/403；504 和已计量的拒绝除外 | 订阅错误码沿用这套分类（§8） |
| `model_runtime/secrets.py`、`admin/config_files.py:71-75` | API Key 存在 `model_profiles.secrets.json`；配置 bundle 的 CAS revision 包含 secret 文件字节 | 短期 token 放进 sidecar 会让每小时一次的刷新改变 revision，与 Admin 保存发生无关冲突，所以另放一个文件 |
| `model_runtime/executor.py:439-447` `execute()` | 签名里没有触发 Person；只能通过 `current_work_control` 拿到 Work 的上下文 | §7 的闸门要取来源 Person；`runtime/work_session.py:1068` 已有 `control.source["actor_person_id"]` |
| `admin_api/http.py:585-628` | `/models`、`/models/apply`、`/models/secrets` 等接口复用 operator 授权 | 新增 OAuth 账户接口 |

现有的有序 output items、工具回执、私有续接状态、reasoning 和 usage 都可以复用，不需要重写 Agent 链。

## 4. 备选路线：借用 Codex CLI 登录（记录，不实施）

### 4.1 Codex CLI 的做法【实证，`openai/codex` main 源码与本机 codex-cli 0.169.0】

- 登录：`codex-rs/login/src/server.rs` 向 `https://auth.openai.com/oauth/authorize` 发起 PKCE 授权，使用 Codex 固定的公共 client ID（`auth/manager.rs` 里的 `CLIENT_ID`）。scope 为 `openid profile email offline_access api.connectors.read api.connectors.invoke`，**没有 `chatgpt.tokens.use.direct`**，也不带 `resource`；附加参数有 `originator`、`codex_cli_simplified_flow`、`id_token_add_organizations`。另外还有 device code 登录（`device_code_auth.rs`）。
- 换码和刷新走 `https://auth.openai.com/oauth/token`，撤销走 `/oauth/revoke`。访问令牌在到期前 5 分钟内会刷新，距上次刷新超过 8 天也会主动刷新（`manager.rs` 中的 `TOKEN_REFRESH_INTERVAL` 和 `CHATGPT_ACCESS_TOKEN_REFRESH_WINDOW_MINUTES`）。
- 存放：`~/.codex/auth.json`，权限 0600，也可以存进系统 keyring。本机文件只核对了键名：`auth_mode`、`OPENAI_API_KEY`、`tokens.{id_token, access_token, refresh_token, oauth_provenance.{issuer, client_id}, login_owner_id, account_id}`、`last_refresh`。**没有读取任何值。**
- 推理：发往 `https://chatgpt.com/backend-api/codex`（`model-provider-info/src/lib.rs` 的 `CHATGPT_CODEX_BASE_URL`），带 ChatGPT 账户 ID 请求头（`http-client/src/openai_headers.rs`）。请求形态是 Codex 内部的 Responses 变体，不是公共 `/v1/responses`。

### 4.2 不采用的理由

1. 条款：只能经官方支持的登录流程取得 token，必须用自己应用的名字，不得冒充其他应用（§1）；计划用量只能用于用户连接的那个应用（§3.1）。借用 Codex 的 token 或 client ID，相当于冒充 Codex。
2. 文档：“do not point it at ChatGPT’s `backend-api` endpoints.”
3. 技术：refresh token 会轮换。Yuki 和 Codex 刷新同一个会话时，后刷新的一方会遇到 `refresh_token_reused`，导致所有者本机的 Codex 被登出。Codex 的 scope 本身也不含计划用量权限。
4. 先例：协调者提供的信息是，同类做法在 Claude 上已于 2026-04 被禁。我查到的公开报道是：Anthropic 在 2026-02-19 更新文档，明确订阅 OAuth token 只能用于 Claude Code 和 Claude.ai；多篇报道称 2026-04-04 开始服务端执行。我没有找到 Anthropic 关于这个执行日期的一手声明，所以这一条**作为风险旁证，不作为事实依据**。
5. 合规的替代方式已经存在：Yuki 通过连接器委派本机 Codex（`external_agents/`）时，Codex 用的是它自己的登录，由 Codex 作为被连接的应用。这条路径不在本书范围内，但同样受 §3.1 “他人触发” 条款影响，因为它走的是 Codex 自己的条款。是否限制他人触发委派，见 Q8。

## 5. 设计总览

```
Profile(auth="chatgpt_plan", oauth_account="main", protocol="responses", provider="openai")
   │
TaskModelExecutor 逐跳（#713）
   ├─ 能力 / 容量 / 原生搜索检查（现有）
   ├─ 【新】授权人范围检查：该 Profile 是订阅凭据，且请求来源不是凭据所属 Person
   │        → skipped_scope，0 次物理请求，继续下一跳；没有下一跳就抛 LLMConfigurationError
   └─ 调用 Provider
        ChatGPTPlanResponsesProvider（订阅方言）
          ├─ 请求投影：stream:true、store:false、不发不支持的字段、namespace、system→developer
          ├─ 每次物理 attempt 前取当前 token：OAuthAccountStore.access_token(account)
          │    到期前 5 分钟内 → 本进程内按账户串行刷新，加跨进程文件锁，原子写入
          ├─ SSE：逐事件读取，直到 completed / incomplete / failed / error 或断流
          └─ 到终态后复用现有 Responses 解析器 → ChatResponse
```

## 6. 凭据、注册与回调

### 6.1 数据与存放【推荐，Q5】

- 新增私有文件 `model_oauth_accounts.json`，与 `model_profiles.toml` 同目录，权限 0600，以“写临时文件 + fsync + rename”的方式原子替换，读取时沿用 `read_secret_file` 的检查（普通文件、单链接、owner-only、大小上限）。它**不在**配置 bundle 的 CAS 里，所以 token 刷新不改变 bundle revision。
- 文件内容：`version`，部署级 `host_id`（`urn:uuid:…`，首次登录前生成并持久化），`accounts.<label>` 下保存 `client_id`（issued）、`issuer`、`subject`、`email`、`id_token`、`access_token`、`refresh_token`、`expires_at`、`earliest_refresh_at`、`scopes`、`person_id`（授权人）、`saved_at`、`status`（`active`、`signed_out`、`needs_login`、`plan_disabled`）。
- `person_id` 是这份凭据所属 Yuki Person 的事实，在登录时确定：发起登录的 operator 必须显式指定，默认是所有者自己的 Person。之后不随平台账号或 Binding 变化。
- Profile：`auth = "chatgpt_plan"`、`oauth_account = "<label>"`、`api_key_env = ""`、`base_url = "https://api.openai.com/v1"`。`oauth_account` 进入 Profile revision，换账户等于换链，保留原 Work、预算和回执；token 刷新不进入 revision。
- 不放进 SQLite 的理由：生产库会被备份，也会被只读审计查询，token 放进去会扩大暴露面；而且刷新属于网络 I/O，写入要和 writer 队列完全分开。

### 6.2 注册与登录（所有者操作）

1. 所有者执行 `yuki admin model oauth login --account main [--person <id>]`，或者调用 Admin API `POST /models/oauth/login`。Bot 生成 state、nonce 和 PKCE verifier，放在进程内存中，10 分钟过期；返回授权 URL，其中 `redirect_uri=http://127.0.0.1:<port>/auth/callback`。
2. 首次登录时 `client_id=dynamic_agent_client`、`agent_name_hint=Yuki`；之后用已存的 issued client ID，并附 `id_token_hint`（退出后不再附）。`ext_agent_host_id` 始终是 Bot 自己的 host_id。
3. 所有者在自己的浏览器里同意授权。授权页显示的应用名默认是 “Yuki”，用户可以改。
4. 回调交给 Bot，方式见 §6.3。Bot 依次校验 state，处理 `error=access_denied`，核对 `client_id`，再换码、验证 ID token（JWKS、iss、aud、exp、nonce）、检查 scope 是否包含 `chatgpt.tokens.use.direct`。全部通过后原子写入凭据。
5. 读取 `/v1/models`，展示可用模型；所有者从中选择 `slug` 写入 Profile 的 `model`，并显式把这个 Profile 写进路由或 `[fallbacks]`。
6. 首次启用时提示一次 “You’re using your ChatGPT plan”，并附 Manage usage 链接。

【推荐】fork 部署也用 `Yuki` 作为应用名，`agent_name_hint` 不提供配置项；fork 如果改了产品名，自行修改这个常量（条款 §1 不允许冒充其他开源项目）。

### 6.3 回调由谁接【推荐，Q4】

| 方案 | 做法 | 利 | 弊 |
| --- | --- | --- | --- |
| A 粘贴回调（推荐，首版） | 浏览器跳到 `127.0.0.1` 后页面会打不开，所有者把地址栏里的完整 URL 粘贴给 `yuki admin model oauth complete`，或 Admin API `POST /models/oauth/callback`；Bot 校验后换码 | Bot 不管部署在哪里都能用，不依赖 Desktop；token 从不离开 Bot | 多一步手动操作；【解读】标准授权码流程，只是 code 的转交由人完成，官方没有专门写这种方式 |
| B Desktop 中转（第二阶段） | Desktop 在所有者的 Mac 上监听 `127.0.0.1:<port>`，从 Bot 取得授权 URL，打开系统浏览器，收到回调后把 query 原样经管理通道转给 Bot；换码仍由 Bot 完成 | 一键完成；token 不经过 Desktop | 需要给 Desktop 明确的模型管理授权（现有 `/client/v1` device token 没有这个权限） |
| C Bot 本机监听 | Bot 与浏览器在同一台机器时，Bot 自己监听 loopback | 全自动 | 生产 Bot 跑在 VM 或容器里，浏览器的 `127.0.0.1` 到不了它，只适合开发机 |
| D 凭据文件转交（官方 self-hosted VM 流程） | 在 Mac 上用 Yuki CLI 完成整个 OAuth，再把凭据文件复制到 Bot | 与官方文档逐字一致 | token 要过一次网络和第二台机器；还得在 Mac 上装 Yuki CLI |

刷新始终由 Bot 负责：Bot 是实际执行推理的一方，官方 VM 流程也要求导入后由 VM 独自刷新。Desktop、CLI 和连接器都不保存 token，也不刷新。**连接器不参与**：它的职责是委派本机 Agent，把 OAuth 放进去会扩大它的权限面。

### 6.4 刷新

- 每次物理 attempt 前调用 `access_token(account)`：
  - 剩余有效期大于 5 分钟（与 Codex 相同）且已过 `earliest_refresh_at`（如果有）时，直接返回内存中的 token。
  - 否则在进程内按账户加 `asyncio.Lock`，再加跨进程的 `fcntl` 文件锁；拿到锁后重新读文件，若别的进程已经刷新过就直接用；没有才发刷新请求，成功后立即原子写入，再释放锁。
- 刷新属于网络 I/O，不在任何 SQLite 事务里，也不占用模型调用的 writer。
- 遇到终态刷新错误（§2.3），把账户状态改为 `needs_login`，清掉 access 和 refresh token，保留注册映射；这次请求以 `LLMAuthenticationError(diagnostics.oauth="needs_login")` 失败，派发前已失败，物理请求为 0，所以 #713 可以切到下一跳。看门狗报告这个状态。
- 刷新遇到网络错误或 5xx：凭据保持不变，本次请求以 `LLMUnavailableError` 失败（派发前，0 次物理请求），下一次请求再试。不另设刷新退避计时，因为每次请求最多只刷新一次。
- 流开始前收到 HTTP 401：只刷新一次，然后用新 token 在同一跳内重发一次。401 是未受理，重发不会重复计费；这次重发和传输重试一样计为辅助请求、预留 Work 预算。如果仍然是 401，就按 `LLMAuthenticationError` 处理。
- 已知风险：刷新成功之后、写盘之前进程崩溃，新 refresh token 丢失，旧 token 已经失效，下次刷新会得到 `refresh_token_reused`，需要重新登录（F12）。写盘紧跟在刷新之后，这个窗口只有毫秒级，本书接受这个风险，不另做补偿机制。

### 6.5 退出与换账户

- `yuki admin model oauth logout --account main`：撤销 refresh token（§2.3），清除本地 token，保留注册映射和 host_id。撤销没有得到确认时，CLI 和 Admin 响应里写明 “remote revocation not confirmed”。仍有 Profile 引用这个账户时，退出照样执行，之后对它的请求会在派发前失败（F14）。
- 换账户就是改 Profile 的 `oauth_account`，走现有换链规则。不做自动轮换。

## 7. 授权人范围（执行处核验）【决定，范围见 Q2、Q3】

### 7.1 判定

订阅 Profile 只接受**能证明完全由凭据所属 Person 本人发起**的请求。判定依据全部是现成的事实，不读消息内容：

| 请求来源 | 首版判定（推荐） | 依据 |
| --- | --- | --- |
| 凭据 Person 与 Yuki 的私聊 Conversation（任何平台、client_api 设备、Desktop），Work 来源 actor 是这个 Person，且本次激活合入的所有事件 actor 都是这个 Person | 允许 | “arise from their activity” |
| 同一 Work 的执行 compact 和子工作者（委派给 Yuki 自己的子 Agent） | 允许，跟随父 Work 的来源 | 仍然属于授权人自己的活动 |
| 群聊，或者本次激活合入了其他人的事件（插话） | 不允许 → `skipped_scope` | “Another user’s activity must not trigger…” |
| 公共 compact（Conversation 级） | 仅限上面允许的私聊 Conversation | 同上 |
| 记忆提取、自省、Dream、`utility_structured` | 首版不允许（校验拒绝把订阅 Profile 绑定到这些用途） | 由多人消息批量触发；后台使用还需要单独的 express consent，见 Q3 |
| SELF 自主行动、定时任务 | 首版不允许；第二阶段可以允许“所有者本人创建并显式勾选同意的定时任务” | “expressly authorized automations… Obtain express consent before background use” |
| 插件会话 `plugin_agent_session` | 不允许 | “Do not provide general-purpose API access for other tools” |

### 7.2 实现位置

- 在 #713 的逐跳循环里，紧跟现有的能力和容量检查，加一项 `skipped_scope`。数据来源：从 `current_work_control` 取 Work 来源的 `actor_person_id`、Conversation 类型和本次激活合入事件的 actor 集合，这些都已经在内存里，不查库。没有 Work 上下文的调用（记忆等后台任务）一律视为不满足。
- 跳过时物理请求为 0，与 #713 的能力跳过同样处理。所有跳都被跳过或都失败时，抛 `LLMConfigurationError("chatgpt plan profile is limited to its authorized person")`，并在诊断里写明 `route_hops`。
- 链中途的约束：请求带着订阅 Profile 的续接状态时（#713 规定链内要跟随续接状态），如果这时有其他人的事件要合入这条链，不能派发到订阅 Profile，也不能把续接状态搬到别的 Profile。【推荐】私聊里只有授权人自己会插话，这个情况不会发生；万一发生（例如私聊 Conversation 被改成了多人会话），本次请求按 `skipped_scope` 失败，Work 沿现有失败路径收尾，看门狗报告，不搬运续接状态（F18）。

### 7.3 配置示例（全部显式）

```toml
[profiles.chatgpt_plan_main]
provider = "openai"
protocol = "responses"
auth = "chatgpt_plan"
oauth_account = "main"
base_url = "https://api.openai.com/v1"
api_key_env = ""
model = "<从 /v1/models 选出的 slug>"
capabilities = ["tools", "reasoning", "image_input"]

[routes]
chat_agent = "chatgpt_plan_main"      # 所有者私聊用订阅

[fallbacks]
chat_agent = ["openai_terra_low"]     # 群聊和他人请求都会跳过订阅，落到这里（付费 API Key）
```

所有者也可以反过来配：链首用 API Key，把订阅放在备用位置，这样订阅只在 API Key 未受理时才给所有者本人的请求使用。两种写法都需要所有者显式配置。**按这个设计，群聊永远不会用到订阅**。

校验（`ModelProfileCatalog`）：
- `auth="chatgpt_plan"` 要求 `protocol="responses"`、`api_key_env=""`、`oauth_account` 存在于账户文件，并且不得设置 `headers.authorization`（已有约束）。
- 订阅 Profile 只能出现在 `chat_agent` 的链里（首版）。
- 一条链里最多一个订阅 Profile；不同账户的订阅 Profile 不能互为备用（§8.3）。
- 链里只有订阅 Profile、没有其他跳时，配置照样有效，但群聊请求会全部失败；Admin 保存时给出明确提示，不拒绝。

## 8. 请求方言、SSE 与 fallback

### 8.1 Responses 方言拆分

- 把 `DeepSeekResponsesProvider` 里的共用部分（items 转换、续接合并、usage、输出解析）收拢成 `ResponsesBase`。DeepSeek、OpenAI API Key 和 `ChatGPTPlanResponsesProvider` 各自声明请求差异。按 Profile 的显式 `auth` 选类，**不按模型名、回答内容或某次 400 去猜**。
- 订阅方言的投影：
  - `stream: true`、`store: false`；不发 `max_output_tokens`、`temperature`、`top_p`、`metadata`、`safety_identifier`、`prompt_cache_retention`、`truncation`、`user` 等不支持的字段。
  - 开头的控制消息放进 `instructions`；续轮中的 system 控制项在原位置投影成 developer 消息。
  - 函数工具全部放进一个名为 `yuki` 的稳定 namespace，固定工具清单和顺序都不变。
  - `include: ["reasoning.encrypted_content"]` 照旧【假设：订阅路线接受】。
  - 原生搜索只在 Profile 显式声明 `native_web_search` 时才发送；默认不声明。
  - `prompt_cache_breakpoint` 与 `x-session-id` 是否被接受【假设】，由 §9 的真实探测决定，不接受就不发。
- namespace 的回传形态【假设，必须先用真实请求确认】：返回的 `function_call.name` 是否带 namespace 前缀，带 namespace 时 `tool_choice` 怎样强制指定其中某个函数，`strict:false` 的可选参数在 namespace 内是否保持原语义。如果真实结果表明 namespace 不能满足“强制选某个函数”，结构化任务本来也不绑定订阅（§7.1），只需核对主 Agent 的 `tool_choice=required/auto`。
- 本地 Work 预算继续按原合同计数。订阅路线不发 `max_output_tokens`，单次输出上限由上游决定；`incomplete` 照现有逻辑收尾。
- refusal：`output_text` 之外的 `refusal` 内容块作为合法回答解析，不当作空回答。这一项同时修正三条 Responses 路线。

### 8.2 SSE（只在 Provider 内部消费）

- 在 `JSONHTTPProvider` 里增加流式 `_post_stream`：用 `client.send(request, stream=True)` 发出；状态码非 2xx 时读完 body，沿用 `check_provider_response`，所以 #713 的未受理分类不变；2xx 时逐行解析 SSE（处理分块、多行 `data:`、注释行），只保留最后的终态事件和已出现的 `response.output_item.done`，不逐 token 写库。
- 终态处理：
  - `response.completed`：取出完整的 response 对象，交给现有 `_parse_response`。
  - `response.incomplete`：沿用现有 incomplete 处理。
  - `response.failed`：保留 `error.code`、`error.message` 和 request ID，抛出带这些诊断的 `LLMUnavailableError`，或者按错误码映射成对应的 `LLMRateLimitError`、`LLMAuthenticationError`；已报告的 usage 按现有规则记账。
  - `error` 事件按同样方式处理。
  - 流在终态前结束（EOF、读错误、读超时）：结果未知，统一为 `LLMUnavailableError`，不重发。
- 订阅路线的传输重试只覆盖确定没离开本机的错误：`ConnectError`、`ConnectTimeout`、`ProxyError`、`PoolTimeout`，以及流开始前的 503。`ReadTimeout` 不重试，因为它可能发生在上游已受理之后。官方 SDK 示例本身也是 `maxRetries=0`。
- 诊断：成功时记录已解码的终态 response 对象，标注 `transport: "sse"` 和事件计数；失败时记录终态事件。沿用现有的脱敏和容量规则，**不记录请求头**（含 Bearer）。

### 8.3 与 fallback 链（#713）

| 订阅侧失败 | 时机 | 是否触发下一跳 | 理由 |
| --- | --- | --- | --- |
| `skipped_scope`（他人请求） | 派发前 | 是（跳过） | 0 次物理请求 |
| 账户 `needs_login` 或 `signed_out`、刷新网络失败 | 派发前 | 是 | 0 次物理请求；看门狗持续报告 |
| HTTP 429 `subscription_sharing_usage_limit_exceeded` | 流开始前 | 是（#713 已把 429 视为未受理） | 上游明确拒绝受理 |
| HTTP 503 `subscription_sharing_usage_unavailable` / `user_unavailable` / 直连不可用 | 流开始前，跳内重试用尽后 | 是 | 同上 |
| HTTP 401/403（含 `user_not_eligible`、`invalid_user`、`chatpass_v2_*`、`route_not_supported`） | 流开始前 | 是 | 同上；401 先按 §6.4 刷新一次 |
| HTTP 400 `subscription_sharing_unsupported_capability` | 流开始前 | **否** | 请求形状问题，属于配置或实现缺陷，不能靠换 Provider 掩盖 |
| `response.failed`（含额度错误码） | 流开始后 | **否（首版）** | 流已经开始，按 #713 视为已受理、结果按已发生处理；见 Q6 |
| 流在终态前断开 | 流开始后 | 否 | 结果未知 |

- 一条链里不得有两个不同账户的订阅 Profile：额度用完后切到另一个订阅账户，就是条款禁止的 “rotating accounts … bypassing usage limits”。校验直接拒绝这种配置。
- 订阅额度用完后切到所有者付费 API Key 的备用 Profile，属于 “separately authorized API use”，条款不禁止；但这会让这次请求从“消耗订阅额度”变成“付费”。所有者把这个 Profile 写进 `[fallbacks]` 就视为显式授权，这与 #713 Q5 的口径相同。看门狗会报告“当前在用备用模型”。见 Q7。
- 订阅额度用完时，看门狗用一条状态事实 `model_plan_usage_limit:<account>` 报告，并附 Manage usage 链接。下一次在订阅 Profile 上成功调用后清除。不设计时，也不根据它推断额度何时重置（官方要求 “Don’t … infer a reset time”）。

## 9. 管理入口与 UI

- Admin API（复用 operator 授权）：`POST /models/oauth/login`、`POST /models/oauth/callback`、`GET /models/oauth/accounts`（只返回 label、email、subject 的掩码、scopes、`expires_at`、`status`、`person_id`、`manage_usage_url`，**绝不返回 token**）、`POST /models/oauth/logout`、`GET /models/oauth/accounts/{label}/models`（按需读取 `/v1/models`）。
- CLI：`yuki admin model oauth login|complete|status|models|logout`。`status` 显示 “Using ChatGPT plan” 和 Manage usage 链接。
- Manage usage 链接（`https://chatgpt.com/settings/usage`）出现的位置：
  1. CLI `status` 输出和 Admin 账户视图。
  2. Desktop 模型设置页：订阅 Profile 旁边显示 “Using ChatGPT plan · Manage usage”（第二阶段）。
  3. 首次启用的一次性提示。
  4. 额度用完时的看门狗报告和所有者私聊通知，以链接作为主要操作。

  群聊里的普通回复不显示，因为群聊本来就不会使用订阅。
- Desktop（第二阶段，另开 issue）：§6.3 方案 B 的一键登录、账户状态、Manage usage 链接。Desktop 需要明确的模型管理授权。
- 账户目录和真实推理结果分开记录：模型列表能读出来，不代表这个模型可用；只有一次成功完成的推理才算证明。

## 10. 待决问题

**Q1 做不做，做到哪一步（必须所有者表态，不默认执行）**

| 选项 | 内容 | 评价 |
| --- | --- | --- |
| A 不做 | 维持 issue 下的“先放着” | 没有条款风险。所有者要用订阅时，仍可以经连接器委派本机 Codex |
| B 只用于授权人本人（如果做，推荐这一项） | §6–§9：所有者私聊、所有者自己的设备会话和 Work 走订阅；群聊和他人请求跳过 | 合规范围清楚；代价是要新增执行处范围闸门，且主要的群聊流量用不上订阅 |
| C 全量挂到 `chat_agent` | 群聊也用订阅 | **违反 Terms §3.1，不可选** |

工作量（B）：Responses 方言拆分和 SSE 2–3 人日；OAuth、凭据文件和刷新 1.5–2 人日；范围闸门和校验 1 人日；Admin/CLI 0.5–1 人日；E2E 和真实抽检 1–1.5 人日。后端合计约 6–8.5 人日，Desktop 第二阶段另计 1–2 人日。

**Q2 授权人范围按什么判定**【推荐：私聊 Conversation、Work 来源 actor、本次激活全部事件 actor 三者都必须是凭据 Person】
理由：三项都是现成的身份事实，不看内容；任何一项不满足就跳过。备选是只看 Work 来源 actor，但那样会把插话的他人消息也算进去，不符合 “Another user’s activity must not trigger…”。

**Q3 后台用途**【推荐：首版只开放 `chat_agent`；SELF、定时任务、记忆任务都不绑定订阅】
理由：条款要求后台使用先取得 express consent，而且触发源不能是其他人。记忆提取和 Dream 由群消息批量触发，不满足条件。第二阶段可以开放“所有者本人创建、创建时勾选同意的定时任务”，同意记录作为任务事实保存。

**Q4 回调方式**【推荐：首版方案 A（粘贴回调）；第二阶段 Desktop 方案 B；不做 C、D】
理由：A 不依赖部署形态，token 始终只在 Bot 上，Desktop 也不需要先补管理授权。D 让 token 多经过一台机器。

**Q5 凭据存放**【推荐：模型文件旁的私有 JSON 文件，不放 SQLite，不进 bundle CAS】
理由：见 §6.1。【解读】条款要求 “local and under the user’s control”：所有者自己的主机或 VM 满足；如果以后部署到第三方托管平台，需要重新评估。

**Q6 流开始后的 `response.failed` 额度错误是否触发 fallback**【推荐：首版不触发】
理由：#713 的原则是只在确定未受理时切换，流开始后的失败按已受理处理。代价是：如果上游总在流开始后才报额度用完，链就不会切到备用，这一轮会失败并由看门狗报告。备选：真实抽检（§11）确认这种失败不带 usage、也没有任何 output item 之后，再把“流开始后、无 output、无 usage、错误码属于 `subscription_sharing_usage_*`”加入未受理集合。这仍然是按协议事实判断，不看内容。

**Q7 订阅之后是否允许备用付费 API Key**【推荐：允许，只要所有者显式写进 `[fallbacks]`；默认空链】
理由：与 #713 Q5 的口径相同。条款明确 “These restrictions do not affect separately authorized API use”。但官方 UI 指南建议额度用完时引导用户去 Manage usage，而不是悄悄换计费路径，所以看门狗必须持续报告。

**Q8 委派本机 Codex 是否也要限制为授权人本人**【推荐：本书不处理，另开 issue 评估】
理由：委派 Codex 走的是 Codex 自己的登录和条款，不属于 SIWC 的 token 处理；但 Terms of Use 不允许让他人使用自己的账户，群里他人触发的委派也有同类风险。现有委派授权是否只允许所有者本人发起，本书没有核对，需要另行核对。

**Q9 额度挤占与 credits**【推荐：Yuki 不自建限额；上线前所有者在 ChatGPT Settings → Usage 给 Yuki 设一个周上限（例如 30–50%），保持 “credits after limits” 关闭】
理由：订阅额度和所有者的 Codex 额度共用（help center），Yuki 用量大时会挤占所有者委派 Codex 的额度。credits 要付费，开启后 Yuki 可能在额度用完后继续花钱，这违背“不增加付费调用”。这两项都在 ChatGPT 侧设置，Yuki 只在状态页提示，不做重复的机制。

**Q10 SSE 是否推广到所有 Responses 路线**【推荐：共用 SSE 读取器，首版只在订阅方言启用】
理由：DeepSeek 和 API Key 路线没有这个需求，启用后会改变它们的断连和诊断口径，属于无关风险。

**Q11 实施前先做一次真实探测**【推荐：做，需要所有者同意，并由所有者本人完成授权】
内容：用所有者账户发 3–5 次最小请求（消耗计划额度，不是 API 付费）。核实：namespace 的回传名称和 `tool_choice` 写法、`include`、`prompt_cache_breakpoint`、`x-session-id` 是否被接受、usage 字段、`earliest_refresh_at` 的含义，以及 `/v1/models` 的形态。只记录字段名和形态，不保存 token 和回答正文。

## 11. 失败情况与 E2E

先列全部失败情况，再写 E2E。E2E 在隔离数据库、真实 Runner 和执行器上进行，配一个本地假 OAuth 服务（authorize 跳转、token、JWKS、revocation、openid-configuration）和一个本地假 Responses SSE 上游，**不调用真实 OpenAI**。每个用例结束时导出产物 `test-results/chatgpt-plan-oauth-e2e.json`，内容包括：账户文件的键名和状态（token 值替换为 `<redacted>` 后再比对）、各跳 `model_invocations` 行、`route_hops`、Work journal 的阶段和预算计数、假上游收到的请求次数和 payload 摘要（字段集合）。时间和随机 ID 归一化之后，重复运行必须得到相同的产物。每个用例最后都要断言：全部日志、诊断、trace 和 Admin 响应里，搜不到测试用的 token 字符串。

| # | 失败情况 | 期望 |
| --- | --- | --- |
| F1 | 回调 `error=access_denied` | 先校验 state 再停止；不换码；账户不变 |
| F2 | 回调的 state 不匹配，或者 10 分钟过期 | 拒绝；不换码 |
| F3 | 同一回调重复提交 | 第二次因 state 已消费而被拒；token 端点只收到一次请求 |
| F4 | 换码返回 `invalid_grant` | 丢弃 code，提示重新登录 |
| F5 | 授予的 scope 中没有 `chatgpt.tokens.use.direct`（包括 Business 管理员未允许的情况） | 保存身份，状态为 `plan_disabled`；不派发推理；提示启用或改用其他计费路径 |
| F6 | ID token 验签、iss、aud、exp 或 nonce 不符 | 拒绝；不写入 |
| F7 | 重新授权时回调带回的 `client_id` 与已存注册不同 | 拒绝；原注册不变 |
| F8 | 重新授权得到的 `sub` 与所选账户不同 | 拒绝替换 |
| F9 | 访问令牌剩余不到 5 分钟时，5 个并发请求 | 只刷新一次；5 个请求都用新 token |
| F10 | Bot 和 CLI 两个进程同时需要刷新 | 文件锁串行；后拿到锁的一方重读文件，不重复刷新 |
| F11 | 刷新返回终态错误（`refresh_token_reused` 等） | 状态改为 `needs_login`，token 清除、注册保留；本次请求在派发前失败，物理请求为 0；看门狗报告 |
| F12 | 刷新成功后、写盘前进程被 kill | 重启后下一次刷新得到 `reused`，按 F11 处理（已知风险） |
| F13 | 刷新时网络错误或 5xx | 凭据不变；本次请求在派发前失败；下一次请求再试 |
| F14 | 退出时撤销成功、撤销返回网络错误、仍有 Profile 引用该账户 | 分别为：清除；清除并提示远端撤销未确认；之后的请求在派发前失败 |
| F15 | token 刷新前后 Profile revision 和配置 bundle revision 的变化 | 都不变；持久 Work 不换链 |
| F16 | 换 `oauth_account` | revision 变化，下次激活显式开新链；Work ID 和预算不变 |
| F17 | 群聊请求，链为 [订阅, API Key] | 订阅记为 `skipped_scope`，物理请求为 0；API Key 成功；订阅上游收到 0 次请求 |
| F18 | 私聊 Work 带着订阅续接状态时合入了他人事件 | 不派发到订阅，也不搬运续接状态；按现有失败路径收尾 |
| F19 | 记忆、Dream、插件、`utility_structured` 尝试绑定订阅 Profile | 配置校验拒绝 |
| F20 | 一条链里有两个不同账户的订阅 Profile | 配置校验拒绝 |
| F21 | 所有者私聊，带工具的完整一轮 | namespace 中的函数调用 → 本地执行 → `function_call_output` → 继续推理；call ID、reasoning、opaque items 和消息顺序保持不变；含图片观察后的续轮 |
| F22 | 请求投影 | payload 中没有任何不支持的字段，没有 system item，`stream=true`、`store=false`；DeepSeek 和 API Key 路线的 payload 与改动前逐字节一致 |
| F23 | SSE：分块、多行 `data:`、注释行 → `completed` | 解析结果与非流式时的同一 response 一致 |
| F24 | SSE 终态为 `incomplete`、`failed`（含额度错误码）、`error` 事件 | 分别为：incomplete 收尾；保留错误码和 request ID；不切下一跳 |
| F25 | SSE 在终态前断开或读超时 | 结果未知；不重发；不切下一跳；journal 与现有 `paired` 一致 |
| F26 | 流开始前 HTTP 429 `usage_limit_exceeded`，链为 [订阅, API Key] | 切到 API Key；订阅那一行的物理请求为 1、token 为空；看门狗出现 `model_plan_usage_limit`，下一次订阅成功后清除 |
| F27 | 流开始前 HTTP 401 | 刷新一次后重发一次；再次 401 → `LLMAuthenticationError`，可以切下一跳 |
| F28 | HTTP 400 `unsupported_capability` | 不切下一跳；保留 `error.param` |
| F29 | 取消或 Turn 被取代发生在 SSE 读取中途 | 连接关闭；按现有取消收尾；不进入下一跳 |
| F30 | refusal 内容块 | 作为合法回答返回，不判为空回答（三条 Responses 路线各一例） |
| F31 | Admin 和 CLI 的所有响应、日志、诊断、trace、异常信息 | 不出现 access、refresh 或 id token |
| F32 | 账户文件权限不是 0600、是符号链接或超过大小上限 | 读取失败，Profile 视为未配置；错误信息不含文件内容 |

E2E 用例：

- **用例 1（F1–F8）** OAuth 登录的各种结果。
- **用例 2（F9–F16）** 刷新、并发、轮换、重启、退出、revision。
- **用例 3（F17–F20）** 范围闸门和配置校验（配置校验 F19、F20 按参数化测试先写全）。
- **用例 4（F21–F25、F29、F30）** 所有者私聊的工具链和 SSE 回放。
- **用例 5（F26–F28、F31、F32）** fallback 交互、错误码、泄露检查。
- **回归**：DeepSeek Responses、OpenAI API Key 和兼容端点的现有测试保持通过；#713 的 `test_model_fallback_chain_e2e.py` 保持通过。

真实验收（实施后、上线前，需要所有者同意并本人授权）：方案 A 走一遍真实登录 → 所有者私聊完成一轮带工具的对话 → 退出 → 重新登录。确认 Manage usage 链接、账户状态和群聊跳过都符合预期。只记录形态和计数，不记录 token 和正文。

## 12. 安全

- token（access、refresh、id）只存在于账户文件和 Bot 进程内存里。不进 TOML、API Key sidecar、SQLite、日志、trace、诊断、`model_invocations`、异常信息、Admin 或 CLI 响应、模型输入，也不进看门狗报告。`email` 和 `subject` 在公开状态里只显示掩码。
- `Authorization` 请求头不进入 `provider_request` 诊断。现有 `_PreparedHTTP.full()` 只记录 body（`llm/json_http.py:81-86`），SSE 诊断要保持这一点，用 F31 核对。
- 公开仓库：测试只用假 OAuth 服务签发的假 token。文档、PR 和 issue 里不写真实 issued client ID、host ID、账户邮箱或模型账户的配额数字。
- 账户文件不加入任何备份或导出命令；`yuki` 的诊断打包命令把它排除在外。
- token 不交给插件、委派出去的外部 Agent、连接器或沙箱环境变量（条款 “Connected application only”）。

## 13. 硬约束对照

| 约束 | 怎么满足 |
| --- | --- |
| 不按内容隐式切换 Provider | 选用订阅方言靠 Profile 的显式 `auth`；跳过靠身份事实；fallback 只看 #713 的失败类别 |
| 不增加付费调用 | 订阅消耗的是计划额度；切到付费备用必须由所有者显式配置；建议关闭 credits（Q9）；结果未知不重发 |
| 权限在执行处核验，不改固定工具声明 | 范围闸门在执行器逐跳循环里；工具清单只在 namespace 里包一层，名称和顺序不变 |
| 身份按事实处理 | 凭据所属 Person 在登录时显式确定，之后不随账号或 Binding 变化；判定只用来源 actor 事实 |
| 写事务里不等待外部工作 | 刷新和换码都在任何 SQLite 事务之外；凭据不放数据库 |
| 恢复按原执行 ID 和回执，不重置预算 | 续接状态跟随实际 Profile；token 刷新不改 revision；401 刷新后的重发计为辅助请求 |
| 禁止防御性编程、删除优先 | 不做账户池、自建限额、健康探测或刷新退避计时；复用 #713 的分类、跳过和看门狗；SSE 只在订阅方言启用 |

## 14. 要改的文件（Q1 放行后）

- `model_runtime/models.py`：`ModelProfile.auth`、`oauth_account`。
- `model_runtime/oauth_accounts.py`（新）：账户文件读写、文件锁、刷新、撤销、ID token 校验（JWKS）。
- `model_runtime/profiles.py`：§7.3 校验。
- `model_runtime/pool.py`：订阅 Profile 构造 `ChatGPTPlanResponsesProvider`，注入凭据取用函数；连接池键加上账户。
- `model_runtime/executor.py`：`skipped_scope`；`profile_revision` 包含 `oauth_account`。
- `llm/json_http.py`：每次 attempt 取 Authorization；`_post_stream` 与 SSE 读取器。
- `llm/deepseek_responses.py`、`llm/openai_responses.py`、`llm/chatgpt_plan_responses.py`（新）：共用部分收拢、订阅方言、refusal。
- `application/watchdog.py`：`model_plan_usage_limit:<account>`、`needs_login`。
- `admin/`、`admin_api/`、`cli.py`：§9 的接口和命令。
- `config/model_profiles*.example.toml`：订阅 Profile 示例（注释状态）。
- 文档：`model-providers.md`（订阅认证、方言差异、范围闸门、错误码与 fallback）、`development-contract.md` §3 补一句边界、`docs/architecture/README.md` 索引、`execution-trace.md`（`transport: sse`），以及运维说明（登录、退出、Manage usage、上限建议）。
- 测试：§11 的 E2E 和参数化校验；CI 沿用现有 unit/integration，不新增 job。

## 15. 实施顺序（Q1 放行后）

1. Q11 真实探测（所有者授权），按结果修订 §8.1 中标【假设】的部分。
2. 先写 §11 的参数化校验和 5 个 E2E 用例（预期失败），再写实现。
3. Responses 方言拆分、refusal、SSE 读取器。
4. 账户文件、OAuth 登录、刷新、退出，以及 Admin/CLI。
5. 范围闸门、校验、看门狗。
6. 文档同步，README 索引。
7. 本地 ruff、mypy、unit、integration；归档 E2E 产物。
8. PR、CI、合并、Bot-only 部署分别记录；部署后由所有者本人完成登录并显式配置路由。
