# Yuki 与 Memoh 工具数量、多平台装配及删减参考报告

日期：2026-10-16。状态：**只读调研和讨论方案，未授权、未实施任何工具删改**。

2026-10-17 状态补充：上述状态仅指本页原调研阶段。后续已授权并本地实现通用工具/平台适配、内部ID、固定入口合同、默认raw第二出口退役和新READ调用重新执行，见[实施记录](Yuki-299与多平台解耦实施记录-2026-10-16.md)。§5的进一步取消/查询/自动化/历史入口合并和说明精简仍是未实施候选，不在[人物身份任务书](Yuki-人物身份解耦与原回执修复任务书-2026-10-17.md)范围内。下文旧工具数量和源码行号保持原基线，不当作当前工具数；聊天检索能力增强另与[#448](https://github.com/YuanYeYouTao/Yuki/issues/448)协调。

本文与 #299 同步 main 的审查任务书分开：先说明现有会话和工具事实，再给出可讨论的精简方向。工具出现在声明里不代表调用者有权执行，也不代表对应网关、模型服务或沙箱已经可用。

本轮重点是 **Memoh 的实际工具名、数量及多平台加载方式**。固定源码核得 50 个唯一内置工具名，由 20 种内置 Provider 提供；另有两个动态外部工具源。它统一注册 Provider，再按本次会话条件装配，不维护 QQ、Telegram、Discord、Web 四份独立清单。平台会影响默认目标、参数必填、用法和执行支持，但不能只看平台名就推算工具数。完整清单见 §4.0，多平台链和矩阵见 §4.1.1，模型与 ACP 的计数边界见 §4.1.2。

## 1. 调研基线与结论

| 对象 | 本次核实的基线 | 核实方法与范围 |
| --- | --- | --- |
| Yuki | 本地合成分支 `c346041bcdec46955bab048ab40864b6057879c2`；已纳入 main `9e6f6147` | 读现行合同和工具实现；完整 ApplicationContainer 的离线装配清单由本轮主审生成，插件关闭、自动化启用、假模型配置，不调用模型或 QQ |
| Memoh | `felinics/Memoh` main `7e40b52ec46d076c17d93e578f5fc18f88742f97`；提交时间 2026-10-16 02:00:05 UTC+8，`fix(session): 清空历史后不再显示已清除的运行 (#1490)` | 本轮联网 `git ls-remote`；用户指定的完整 checkout 为 `D:/Code/My_Github_Repo/Memoh`，origin 与 SHA 实查一致且干净；不是继续沿用 #299 任务书的 `f6a4674739b40495fef9da928bb19092e144ff51` |
| Memoh 官方文档 | `https://docs.memoh.ai`，本轮 HTTP 读取 sessions、skills、approvals、memory、schedule、web-search 原始 Markdown | 用于解释产品语义；网站未提供本轮可核对的部署 commit，因此实现细节以本文固定 SHA 的源码为准 |

值得借鉴的是能力按领域组织、文件与命令共用工作区、技能说明按需读取、历史查询明确会话范围。**不能直接移植的是按每次会话甚至运行中安装结果变化的模型工具集合。** Yuki 已有部署冻结合同和 Code 查询投影；精简应先找重复说明、重复入口和实际可替代能力，不以减少工具数量本身判断成功。

本报告没有运行 Memoh、安装扩展、调用搜索或模型，没有测量工具使用频率、真实 token 成本或缓存命中率。

`.cache/memoh-tool-reference` 是早期临时取证浅克隆，不是用户交付目录；报告引用的 Go 文件已与上述持久 checkout 逐文件比对一致。UI 子模块不参与本报告的 Go 工具源码判断。

## 2. “每入口独立 session”的准确含义

Yuki 的持久概念是 canonical Conversation；模型请求、Work activation 与它不同。当前 #299 源码中，同一 Person 的不同私聊 Binding 各自有 Conversation，primary alias 为 `binding:<id>`。App 则是该 Person 的一个服务器生成 Binding 和一个主会话，**多个设备共用这个 App 会话**。群会话按真实 Space 共享，不因同群换一个在线 Yuki 账号就另造群史。`/ai new` 只推进命令所在 Conversation 的 generation，不重建其他入口。见 [Canonical runtime 第 15–46 行](canonical-runtime.md#L15-L46)、[Client API 第 19–23 行](client-api.md#L19-L23)。

因此，“入口独立”不等于按设备各建一份会话，也不等于 App 默认把本人全部 QQ 私聊拼成共同历史。App 若需看本人其他私聊，用额外的只读 `read_personal_entry` 显式读取，保留平台、Binding、Conversation、内部事件 ID；历史正文是资料，不能给当前调用补造授权。该工具不读群聊、不写别的会话，见 [实现第 17–107 行](../../src/qq_ai_bot/services/personal_entry_tool.py#L17-L107)。这些是正常主会话的合同与装配事实，不表示本轮另列的 App 子 Agent 缺口已经修复。

模型侧隔离按 Conversation／generation，并另有 Work 分支：普通 Main 的 key 为 `yuki:<conversation>:g<generation>:main`，Work 为 `yuki:<conversation>:g<generation>:work:<workid>`。因此这里没有“每入口永远只有一个物理模型 session”的保证，更不能按 Person 共用私有续跑状态。见 [请求构造第 899–924 行](../../src/qq_ai_bot/services/turn_execution.py#L899-L924)。

Memoh 同样把 Session 当独立上下文和历史；一个 Bot 可有多个 Session，旧 Session 不因 `/new` 消失。其真实历史工具只接受当前会话、同一外部 route 或同一认证用户创建的可见会话；Bot 级 Memory 与逐会话原文的可见性分开。这可参考，但不意味着 Yuki 应照搬 Bot 级共享记忆范围。[官方 Sessions][D01]、[历史可见范围实现][M07]

后续 ACP 每个 ACP session 独立 Conversation 是 #299 任务书的后置计划，不是本分支已经提供的入口。也不能把 Memoh 的多 Session 产品界面当成本项目已实现多标签个人会话的证据。

## 3. Yuki 当前完整内置工具声明

### 3.1 数量和体积

来源为本轮 `.cache/new-main-review/root/tool-inventory-{direct,code}.json` 及同目录复现探针、日志。以下 JSON 只序列化 `{name,description,parameters}`，紧凑格式、`ensure_ascii=False`；字符数和 UTF-8 字节数均非 Provider wire 的 token 数。

| 模式／入口 | 冻结完整执行合同数 | 交给模型直调的工具数 | 模型声明 JSON 字符 | UTF-8 字节 |
| --- | ---: | ---: | ---: | ---: |
| direct／平台 | 56 | 56 | 35,258 | 46,774 |
| direct／App | 57 | 57 | 35,764 | 47,428 |
| Code／平台 | 58 | 42 | 25,968 | 36,260 |
| Code／App | 59 | 43 | 26,474 | 36,914 |

这是完整内置装配、插件关闭时的结果，不是早期精简测试 harness 的 40 项，也不是旧版本的工具数。批准插件的增量另计。App 与平台当前只差 `read_personal_entry`；平台工具没有因为进入 App 就自动整组消失。

部署首次冻结时从完整 registry 生成排序声明，并分别派生平台和 App 的 revision；选择依据是 canonical Conversation 的 Binding.platform，设备、当轮正文与当前用户权限不参与裁剪。执行时仍核真实 ToolActor、来源、权限、预算和回执。见 [冻结装配第 59–145 行](../../src/qq_ai_bot/services/main_agent_contract.py#L59-L145)、[入口选择第 147–166 行](../../src/qq_ai_bot/services/main_agent_contract.py#L147-L166)、[Tool Kernel 第 9–24 行](tool-kernel.md#L9-L24)。

Code 额外包含 `execute_code` 和 `lookup_tools`；16 项既有能力通过固定脚本 API 调用：11 个 `automation_*`，以及 `call_onebot_api`、`environment_packages`、`environment_service`、`recall_own_message`、`save_conversation_attachment_to_workspace`。`lookup_tools` 只读这份已冻结 API 的说明／schema，不给模型追加函数声明，不赋予权限。其余能力仍可直调。Code 是同一工具实现的调用语法，不是删掉这些能力或换一个执行系统。[固定直调集合及查询实现](../../src/qq_ai_bot/codemode/tool_visibility.py#L14-L145)

不能由上述字节差推导实际费用、命中率或 Code 整体更省：脚本说明、公共上下文、私有协议、模型具体序列化及真正 usage 都未计入这一表。

### 3.2 平台 56 项的完整分类

分类仅为阅读方便，未提出新的权限层或路由。

| 领域 | 当前工具 |
| --- | --- |
| 联系与投递，6 项 | `find_contacts`、`get_group_members`、`send_message`、`poke_person`、`recall_own_message`、`call_onebot_api` |
| 聊天读取，4 项 | `get_recent_chat_history`、`search_chat_history`、`get_chat_history_around`、`read_conversation_history` |
| Memory 与短状态，5 项 | `search_memory`、`get_memory_fact`、`get_memory_evidence`、`memory_change`、`update_short_state` |
| 输入附件／工具结果，3 项 | `inspect_conversation_attachment`、`save_conversation_attachment_to_workspace`、`read_tool_artifact` |
| 工作区，10 项 | `workspace_read`、`workspace_write`、`workspace_list`、`workspace_search`、`workspace_inspect`、`workspace_patch`、`workspace_mkdir`、`workspace_move`、`workspace_delete`、`workspace_publish` |
| 终端／环境，9 项 | `terminal_exec`、`terminal_read`、`terminal_write`、`terminal_control`、`get_code_run`、`cancel_code_run`、`environment_status`、`environment_packages`、`environment_service` |
| 自动化／时间，12 项 | `automation_create`、`automation_update`、`automation_get`、`automation_list`、`automation_list_history`、`automation_history`、`automation_diagnose`、`automation_pause`、`automation_resume`、`automation_cancel`、`automation_run_now`、`time_get_current` |
| Work／子 Agent，4 项 | `task_control`、`subagent_start`、`subagent_control`、`subagent_message` |
| Web，2 项 | `web_search`、`read_webpage` |
| 自身权限查询，1 项 | `get_my_capabilities` |

App 再加 `read_personal_entry`。`get_my_capabilities` 查询真实调用主体可读／可管理范围，和 Code 的 `lookup_tools` 查询冻结函数目录不同。不能把“当前可执行权限”和“模型看见的声明”混为一谈。[权限与当前聊天工具第 446–516 行](../../src/qq_ai_bot/services/agent_tools.py#L446-L516)

最大六项声明为 `memory_change` 3,653 字符、`task_control` 2,652、`automation_list` 2,480、`automation_create` 2,334、`automation_update` 2,162、`send_message` 2,063；合计 15,344 字符，约占平台 direct 这份 JSON 的 43.5%。这是整项声明体积，含必要参数结构，**不能把这部分全当冗余提示词**。

## 4. Memoh 真正怎样把工具交给模型

### 4.0 内置 50 项从哪里来，每组何时加载

统计固定 `7e40b52e` 的 `toolname.All()`、core Provider 注册和每组 `Tools`；另与仓库提交的 19 组 schema golden 对照。golden 覆盖 45 个无重名工具，缺的 5 项是 `app_manage`、`app_search`、`mcp_manage`、`search_memory`、`list_execution_locations`，补齐后与 50 项目录完全相等。golden 的 Memory 只含三个写工具，Container 用 nil Manager，不能直接把 45 当完整清单。[工具名全集][M26]、[schema 对照的构造条件][M27]、[注册入口][M01]

| 内置 Provider | 最大项数 | 实际工具名 | 主要加载条件 |
| --- | ---: | --- | --- |
| Capability | 3 | `app_search`、`app_manage`、`mcp_manage` | 非子 Agent，Bot 与渠道身份、Access 及对应管理服务存在；执行时再核真实权限 |
| AskUser | 1 | `ask_user` | 非子 Agent，交互模式且满足 CanRequestUserInput 或 CanListUserInput；后者供外部 Agent 预列，仍须交互模式 |
| Message | 2 | `send`、`react` | 非子 Agent；注入 Sender/Reactor/Resolver 能力，未按当前 IM 的原生 reaction 能力裁清单 |
| Contacts | 1 | `get_contacts` | contacts 服务存在 |
| Schedule | 5 | `create_schedule`、`update_schedule`、`delete_schedule`、`get_schedule`、`list_schedule` | schedule 服务存在，整组五项 |
| Workdir | 1 | `list_workdirs` | workdir 服务存在，不要求已有工作目录 |
| ACPAgents | 1 | `list_acp_agents` | pool 和 queries 存在，不要求已有可用 Agent |
| Memory | 4 | `search_memory`、`create_memory`、`update_memory`、`delete_memory` | 解析到该 Bot 的 Memory Provider，ListTools 成功；当前 builtin 搜索一项＋会话层写三项 |
| Web | 1 | `web_search` | settings/searchProviders 服务存在即声明；Bot 没选搜索 Provider 时调用才报错 |
| Container | 7 | `read`、`write`、`list`、`edit`、`apply_patch`、`exec`、`list_execution_locations` | 基础六项总是构造；clients 实现 workspaceTargetResolver 再加位置查询。core Manager 实现该接口，不要求已有远程机器 |
| Background | 5 | `list_background`、`get_background_status`、`kill_background`、`wait`、`wait_until` | bgManager 存在，不要求已有后台任务 |
| Browser | 5 | `browser_observe`、`browser_action`、`browser_remote_session`、`computer_observe`、`computer_action` | settings/Bot 可读且 DisplayEnabled；整组五项 |
| WebFetch | 1 | `web_fetch` | Tools 本身无配置 gate，调用时处理配置和提取错误 |
| Spawn | 4 | `spawn_agent`、`send_message`、`list_agents`、`list_models` | 非子 Agent且 agent 执行器已注入；整组四项 |
| Skill | 2 | `list_skills`、`use_skill` | 实际有效 Skills 非空；无技能则不声明 |
| TTS | 1 | `speak` | 对应服务存在且 Bot 配置 TTS 模型 |
| Transcription | 1 | `transcribe_audio` | 对应服务存在且 Bot 配置 ASR 模型 |
| ImageGen | 1 | `generate_image` | 对应服务存在且 Bot 配置图像模型 |
| VideoGen | 1 | `generate_video` | 对应服务存在且 Bot 配置视频模型 |
| History | 3 | `list_sessions`、`get_messages`、`search_messages` | 对应 sessions/messages/queries 依赖分别存在，逐项装配 |
| **静态内置并集** | **50** | 唯一名字去重后的全集 | **不是默认部署或每轮必带数量** |

总共注册 22 个 Provider 实例：上述 20 种内置 Provider，另两个 `FederationProvider` 分别接 Connector 和 MCP/Federation 源。外部目录会变化，不计进固定 50；最终还按 schema 有效性、保留名称和重复名规则处理，不能简单用“装了几个 MCP server”推函数数。[内存工具的实际组合][M10]、[外部工具源][M14]、[位置查询条件][M28]

在**所有可选组条件都成立、core Manager、builtin Memory、无动态外源、模型支持函数调用**的共同假设下，可以按源码推导：

| 运行条件 | 内置函数数量 | 差量 |
| --- | ---: | --- |
| 主 Agent，chat／空模式／acp_agent，交互输入通路可用 | 50 | 所有内置条件满足 |
| 同配置，discuss／schedule | 49 | 非交互模式，不声明 ask_user |
| 同配置，子 Agent | 40 | 隐藏管理 3、AskUser 1、Message 2、Spawn 4；中央 denylist 的 7 项就是后三组，不再重复扣数 |
| native 模型不支持 tool_call | 0 个实际函数声明 | 工具可以先装配，但传给该模型的 Request.Tools 为空 |

这是**固定源码的条件化推导**，本轮没有启动 Memoh 或捕获真实 Bot 请求。生产某一轮的确切数量还需实际 Bot 配置、SessionContext 和外部目录；没有给出“通常多少”或线上测量的虚构值。[交互模式][M29]、[子 Agent 过滤][M21]、[模型调用边界][M04]

### 4.1 注册、装配和查询是不同层

服务启动时 `provideToolProviders` 装入领域 Provider，包括消息、联系人、计划、工作区、后台任务、Memory、Web、技能、媒体、子 Agent、MCP 等。[装配入口][M01]

Native Agent 的 `assembleTools` 随运行构造 SessionContext，把 Bot、Session、用户／渠道身份、workspace target、model capability 等交给每个 Provider 的 `Tools`。Provider 错误会记日志并跳过该组；重复名称跳过；子 Agent 还有专门的过滤。仅真正注册成功的组才贡献对应 Tool usage 说明。最终转换成没有 Execute handler 的 `{name,description,parameters,cacheControl}` 交模型。这里是**按运行能力生成当前清单**，不是 Yuki 的每部署固定清单。[会话装配][M02]、[模型声明转换][M03]、[Provider 请求使用工具定义][M04]

在本次 native 注册和执行路径中，没有查到通用 `query_tools`／`load_tools` 协议供模型先选函数再追加 schema。需要区分：

| 所见操作 | 实际作用 |
| --- | --- |
| `list_skills`／`use_skill` | 先列有效技能名、说明、路径，再取某项技能的完整说明；这是加载操作指导，不是给模型追加函数权限 |
| Federation 的 `ListTools` | Host 从已配置外部工具源取描述与 schema，并纳入当前 Provider 装配；不是模型可自由越权查询全部外部工具 |
| `app_search`／`app_manage`、`mcp_manage` | 搜索扩展市场及安装／配置扩展或 MCP 连接；**这里的 App 是扩展应用，不是 Yuki 的个人 App 会话** |
| ACP 的 NativeToolSource `ListTools`／`CallTool` | 向外部 Agent 暴露宿主现有工具；调用时仍走可用项和运行授权检查，不等于 native 模型有同名加载函数 |

对应实现：[技能两阶段读取][M12]、[Federation 工具源][M14]、[扩展管理及真实 access 检查][M15]、[ACP 工具源读取和调用][M16]。

Memoh 还允许安装能力后在**同一次运行中**重新装配工具、替换 system usage 和下次请求的定义，同时保留原线程和 step cursor。它为新请求重做 prompt-cache 标记，但这不能证明前缀没有变化或缓存命中没有下降。[能力刷新][M17]

Yuki 若照搬，会改变已冻结的声明、manifest、私有恢复合同及模型前缀。因此此项仅作差异说明，不列为本次建议实施能力。

#### 4.1.1 多平台实际经过同一条装配链

路径是：平台入站身份与 Route → 该 Route 的 Session/Thread → 本轮 CurrentPlatform、ReplyTarget、渠道身份、模型和工作区能力 → 统一 Provider 列表 → 各 Provider.Tools(SessionContext) → 去重与子 Agent 过滤 → 模型函数定义 → 执行处的真实授权和渠道 Adapter。

默认 Route 由 Bot、平台、外部会话及 thread 决定，同一个人关联多平台账号不会自动把它们合成一个 Session。Web 可以**显式接续已经授权的 IM Session**；这时历史 Thread 不变，本轮 CurrentChannel 仍是 web，工具参数和 usage 按本轮重新装配。这项产品行为与 Yuki 的原 Binding 会话隔离、原 Work 工具合同不同，不直接移植。[入站 Route][M30]、[Route 查询键][M42]、[Route 的 Thread][M43]、[Web 本轮平台][M31]、[显式 Session 授权][M40]

以下矩阵统一假设同 Bot、同模型、同 chat 模式、相同全部可选能力及交互通路，无动态外源。它只隔离平台维度，**不是四个平台的默认实装测量**：

| 项目 | QQ | Telegram | Discord | Web |
| --- | --- | --- | --- | --- |
| 注册的 Provider 列表 | 同一组 | 同一组 | 同一组 | 同一组 |
| 上述共同假设下的内置名称数 | 50 | 50 | 50 | 50 |
| 仅把共同模式改为 discuss | 49 | 49 | 49 | 49 |
| send／react 声明 | 有 | 有 | 有 | 有 |
| Adapter NativeUserInput | [false][M44] | [true][M45] | [false][M46] | [true][M47] |
| ask_user 呈现 | 纯文本降级 | 原生交互 | 纯文本降级 | Web 交互 |
| 直接渠道 Reactor | 未实现 | 实现 | 实现 | 未实现；当前 live stream event 另有呈现路径 |

`ask_user` 的声明看交互模式和输入交付通路，不直接看 Adapter.NativeUserInput；后者决定按钮还是纯文本。`react` 的声明只看注入的 Executor 能力，当前 QQ direct Adapter 不支持时仍可能在执行时报不支持。**因此“没原生按钮／reaction”不能换算成“少加载一个函数”。** 同样，web_search 未配搜索 Provider 也可能仍声明，声明与实际可执行能力没有完全一一对应。[AskUser 条件][M32]、[输入交付通路][M33]、[文本降级][M34]、[直接 reaction 边界][M35]

工具名称相同也不代表 schema 和提示完全相同：`send/react` 的 platform/target 是否必填取决于当前真实会话是否有可省略的目标；chat 与 discuss 的正文投递和描述不同；Telegram 增加数学 Markdown 用法；web/local 增加工作区预览链接说明。工作区位置、OS、模型 Image/FileInput 会改变路径、shell、读取说明或结果，通常不删除 read/exec 等基本名字。群／私聊本身没有独立的四套工具包，仍需看实际模式、目标和读取范围。[投递参数及模式][M36]、[本轮 SessionContext][M02]、[工作区提示][M37]

真正决定增减的条件集中在 §4.0：Bot 功能配置、Memory Provider、Skills、外部目录、Provider 依赖、交互模式、IsSubagent 和模型函数能力。不能把“模型 Provider/protocol”和“聊天平台”混成一个维度。

#### 4.1.2 native、ACP 与上游 Agent 自带工具分别计数

native 装配的函数经 `ToolDefinitionsFromTools` 进入 `sdk.Request.Tools`；审批只标注和处理执行，不因为待审批就先从模型清单隐藏。本文的 Browser 五项和 web_search 都是 Memoh 本地函数；本轮没有在 native SDK 构造链找到另加 OpenAI hosted web search／Gemini grounding 的配置，不能再给 50 加一遍“原生搜索”。Codex／Claude 独立 runtime 自带的工具属于外部 Agent，最终模型声明数不能由 Memoh 目录代算。[模型函数定义与实际请求][M03]、[M04]

ACP 则可注入一个 Memoh HTTP MCP server，其中聚合 Builtin NativeToolSource、Connector 和 Federation 三类来源；NativeToolSource 排除 FederationProvider，避免同一外部来源重复叠加。ListTools 是函数目录，一个 MCP server 里可以有多项工具；调用再按原 runtime guard、allow 和批准核验。外部 Agent 还能带它自己的工具，不能把“一个 server”记为“一项工具”。[Builtin NativeToolSource][M16]、[三类源装配][M41]、[ACP 的 MCP 注入][M38]

native 的能力变更可以在同 run 的 step 边界刷新工具、handler 和 system usage；ACP server 的 `ListChanged=false`，没有同等的自动列表变更通知保证。外部 Agent 会不会再取 tools/list、最终发给模型多少函数，要看该 Agent 行为，不能只凭 Memoh native 的热刷新代码推断。[native 刷新][M17]、[ACP 能力声明][M39]

### 4.2 各领域的实际设计

| 领域 | 已读实现说明 | 对 Yuki 有用的对照 |
| --- | --- | --- |
| 文件 | `read/write/list/edit/apply_patch` 共用同一 workspace target 与 workdir；图片读取可交回当前模型；位置不可达明确返回错误 | 小组语义清楚；但“终端也能改文件”不等于结构化文件工具重复。Yuki 的 expected_version、字节游标和不可变发布不能随合并丢掉。见 [M05]、[M25] |
| 终端与后台任务 | `exec` 可前台等待或转后台；`list_background/get_background_status/kill_background/wait/wait_until` 按 Bot＋Session 查原 task，区分完成、失败、未知、输出安静和超时 | 借鉴原任务 ID 与状态解释；不另建一套 Yuki 后台任务表。见 [M06] |
| 社交与投递 | `send` 同一函数支持文本、附件与目标；chat 允许普通 assistant 文本回复，discuss 仅显式 send 外发；`react` 单独操作；联系人返回平台／route／target | 多入口接入可共享领域投递实现，不能把 Memoh 的 chat/discuss 输出方式直接替换 Yuki 的统一出口与回执。见 [M08] |
| 历史 | `list_sessions/get_messages/search_messages` 分别做目录、窗口／精确消息和检索；可见范围按当前 session、原 route 或认证用户解析 | 借鉴发现与读取分开；本人跨入口也要保真实来源。Yuki 的群实时网关读取与内部账本读取不能不加区分地归为重复。见 [M07]、[M09] |
| Memory | 每 Bot 选择 Memory Provider；搜索来自 Provider，create/update/delete 由会话层调用，写入带原 actor metadata 和 hook；检索结果的 transcript source refs 再按可见会话过滤 | 可以参考“事实”和“证据原文”分权。Yuki 的 Person/Space/SELF 分域、独立 fact ID、真实证据与隐私代次仍沿原合同。见 [M10]、[M11] |
| 计划任务 | list/get/create/update/delete 五个命令；cron command 用自然语言；默认每次触发建新 session，也能显式固定已有 session／model／ACP／workdir；读取和变更核原 Bot | 可以比较 CRUD 颗粒度，不能因此删除 Yuki 的 Work 续跑、暂停、立即执行和历史回执。见 [M13] |
| 技能 | 有效技能按 Bot 加载；没有技能时这组不声明。列表只返回名、描述、路径；use 返回正文，未找到是明确业务失败 | 可参考说明按需读取；不能把 SKILL.md 内容直接当权限，也不必为精简工具先建设完整技能市场。见 [M12]、[D02] |
| 搜索与网页 | `web_search` 执行时取该 Bot 配置的 Search Provider；未配置／停用报错，不在这个分支暗换供应商；`web_fetch` 是独立正文提取，并可使用配置的 Fetch Provider | 两项不是简单重复。Yuki 应继续区分搜索、正文读取、主模型 native 与独立搜索连接；不能按模型品牌暗换路由。见 [M18]、[M19] |
| 浏览器与媒体 | browser/computer observe、action、remote session 是桌面能力；BrowserProvider 依据 DisplayEnabled 装配；图像、视频、ASR、TTS 是独立 Provider | 这是 Memoh 已有产品范围，不能当作 #299 必须新增的工具。Yuki 当前附件检查／原生图像读取不等于缺少一个必须补的 browser。见 [M20]、[M01] |
| 子 Agent | spawn/send_message/list_agents/list_models 配合后台任务；子 Agent 清单显式去掉直接用户消息、提问和再派生等能力 | 命名和查询方式可比较；这套 denylist 不符合 Yuki 现行“Main 完整能力＋真实来源／委托执行”合同，不建议照搬。见 [M21] |

上述是代码路径事实，不是“这些实现全部无缺陷”的安全审计或线上验收。官方说明中以概括的 `schedule` 称呼计划能力，实际源码是五个工具；同理，不能从 README 所列能力推算真实一次请求的函数数。

### 4.3 权限、失败与部分结果

Memoh 将定义交模型和执行分开；ExecuteTools 只匹配该次可执行清单，JSON 错误、未找到工具、拒绝审批和 handler error 返回对应 `IsError`。普通已批准调用可并行；审批 deferred 则本批次尚未执行，保留开放调用等待原决定。[执行器第 12–175 行][M22]

文件／命令可以接审批和 hook；扩展搜索与管理还在执行处重新核渠道主体与 Bot access；ACP 工具调用在执行前核原 runtime guard。**这不是给 Yuki 增加审批流程、动作黑名单或“一项拒绝全批停止”策略的依据。** Yuki 当前每项真实授权失败与正常生命周期尾部的合同继续成立。[M15]、[M16]、[官方审批说明][D03]

同样，Memoh 源码中前台 `sleep N` 的经验阻止、运行中能力刷新次数上限等，只是上游自己的产品策略，本报告不建议复制。工具结果截断也不能替代 Yuki 的可回读 artifact 和真实效果回执。这里读取的 executor 足以解释正常调用／拒绝／deferred，**不足以据此承诺所有外部效果在崩溃后恰好一次**。[终端执行][M24]、[结果边界][M23]

## 5. Yuki 可讨论的删除／合并／保留候选

这些候选尚未授权实施，不改变 #299 当前修复任务书。优先级按“能否删除真实重复，且保住原能力”排列，不按谁的工具数更少。

| 候选 | 当前实据与可讨论改法 | 代价与必须保留的语义 |
| --- | --- | --- |
| **优先压缩重复说明** | 六个最大声明占本次 JSON 字符约 43.5%；先检查反复出现的目标解释、流程教学与字段长描述，把确有重复的说明删掉 | 参数结构和作用不同不能因长就删；共同约束放固定权威合同，不在每工具复制。任何说明变更会产生新 manifest revision，不宣称旧请求前缀不变 |
| **优先讨论 `cancel_code_run` 的模型入口** | Manager 把它直接映射为对同一 run 的 `cancel`；`terminal_control(action=cancel)` 也到同一 `control`。可在保留原 ID／旧回执 reader 后，讨论删除重复的模型声明 | 这是模型接口候选，不是清空历史或立即删 Manager 原协议；SubagentExecution 等机器取消仍消费旧方法，不能随模型名字一起误删。须核软件安装等非终端 job 的授权与结果读取。见 [Manager 第 1133–1141 行](../../src/qq_ai_bot/sandbox/persistent.py#L1133-L1141) |
| **次级讨论 `get_code_run` 与 `terminal_read` 的收拢** | 两者查询同一 jobs，但前者 `wait_result` 有有限等待，后者增量字节输出并附终态 completion；不是现成等价别名 | 若统一，仍需状态、原 completion／artifact、输出游标和安装任务结果；不能让一次“空输出”冒充完成，也不因查询替代产生重复执行。见 [Manager 第 1126–1155 行](../../src/qq_ai_bot/sandbox/persistent.py#L1126-L1155)、[旧查询声明](../../src/qq_ai_bot/sandbox/client.py#L23-L52) |
| **讨论自动化历史入口是否收拢** | `automation_list_history` 读当前 actor 最近终态，`automation_list(status=terminal)` 是另一种目录语义；可研究在一个 list 查询中明确原范围，减少名字 | 目前不是同范围别名。不能删掉 actor 所有权或把较广目录原文并入较窄历史权限，也不能把 `automation_history(id)` 的真实执行记录当任务目录替代。见 [声明第 185–215 行](../../src/qq_ai_bot/automation/tools.py#L185-L215)、[执行第 317–357 行](../../src/qq_ai_bot/automation/tools.py#L317-L357) |
| **进一步讨论自动化按 action 合并** | 11 项合计 8,867 字符，list 的 `match_task`、create、update 重复携带 TaskSpec。可比较一个复合 automation 工具，或读／写两项的静态 schema，避免完整 TaskSpec 多次声明 | 这比取消别名更大，不能先声称一定省：需比较最终 schema。risk、side_effecting、owner／SELF、创建键、原版本／run／Work 回执必须继续按真实 action 分派；diagnose 是失败创建审计，不能被当作普通任务列表删除。见 [自动化定义与执行](../../src/qq_ai_bot/automation/tools.py#L177-L270) |
| **文件小动作可评估，暂不一刀并入 shell** | mkdir/move/delete 等能在终端完成；Memoh 也同时保留文件函数和 exec，并非只靠一个终端 | Yuki 文件修改核 expected_version、发布不可变 artifact。shell 无法自动继承这些结构化冲突和发布语义；若决定删模型入口，需接受这部分使用体验变化，不能声称纯等价。见 [工作区声明第 33–148 行](../../src/qq_ai_bot/workspace/tools.py#L33-L148) |
| **继续保留搜索与正文读取两项** | Memoh 也区分 web_search/web_fetch；Yuki web_search/read_webpage 工作不同 | 不按 Provider 名称隐式改变选择，也不把“搜索失败”变成购买另一服务的默认策略 |
| **继续保留事实、证据和聊天读取的区别** | search_memory/get_memory_fact/get_memory_evidence 与聊天原文、App 私聊账本读取各有不同范围 | 合并名字若让历史指令变新授权、本人证据变群可见、跨入口默认注入，代价远高于少几个声明。见 [Memory 工具第 518–571 行](../../src/qq_ai_bot/services/agent_tools.py#L518-L571) |
| **继续保留 Work、子 Agent、环境和计划的业务区别** | 名称都含任务，但分别是目标／预算／父子关系、执行资源、未来触发 | Memoh 的 background/schedule 也分开；不新造一个泛化 task 系统替换现有 Work。原 ID、累计预算、未知回执和正常工具尾部必须继续保留 |
| **App 是否移除 QQ 专用工具，单独决策** | 当前 App 继承平台能力，只有额外 read_personal_entry；不能因为处在 App 就认定 QQ 联系人、群成员、发信或撤回无用途 | 删除会影响用户从 App 请求操作其原 QQ 关系的能力。若只想收窄运维逃生口，可单独讨论 `call_onebot_api`，它当前仍核真实超级管理员；不能用“入口统一”悄悄扩大或移除授权。见 [声明第 801–816 行](../../src/qq_ai_bot/services/agent_tools.py#L801-L816) |
| **技能只作为说明组织参考** | 可先用现有文件读取承载确有复用需求的操作说明；不要为少工具而引入市场、安装、热注册和审批等整套机制 | 说明不能制造工具权限，动态技能内容不能改共同历史／固定声明；目前没有证据证明必须新增 list_skills/use_skill 才能解决本次需求 |

当前本地 recent／around／search 三项可讨论收为一个明确模式的账本读取工具。`get_recent_chat_history` 的实际 handler 只读 ledger；声明仍说消息入口可向网关补查，这句话已过时，不应据此设计回退。`read_conversation_history` 另外承担明确目标／实时 QQ、转发 raw、原发送 operation 的读取；App 本人跨入口原文又有自己的授权。先列清每个 mode 的来源和授权，再决定是否减少模型入口，不能“本地没找到就自动换网关或别的入口”。[本地近期读取实现](../../src/qq_ai_bot/services/agent_tools.py#L1169-L1185)、[当前聊天工具声明](../../src/qq_ai_bot/services/agent_tools.py#L469-L516)、[社交历史 schema](../../src/qq_ai_bot/social/tools.py#L54-L97)

`execute_code` 是无宿主文件系统／网络的 Monty 工具编排；`terminal_exec` 是持久 Linux 环境的 Bash，`get_code_run` 查的是 Manager 命令／安装 job。名字都像“代码”，但不是同一执行器，不能合并为任意宿主代码执行。

## 6. 后续决策需要的最小证据

用户若之后授权某一项工具精简，才为该项写实现和验证计划。所需证据限于受影响能力：新声明与旧合同的真实差量；两个入口 direct／Code 的匹配；实际 source／ToolActor 和私有结果授权；原 Work／调用键／unknown 与已确认回执的读取；旧已付请求和公共前缀不被重写；替代入口是否仍完成原工作。

费用与缓存另看同 Provider、同任务的真实 usage：prompt/cached 字段都齐全的样本可计算比例；缺样单列。未抓取真实 Provider 请求、没有有效使用样本时，只报告声明字节，不给收益百分比，也不以固定命中阈值新增完成门槛。

这份报告不把 Memoh 的 UI、审批、Browser、MCP 市场、Bot 级共享 Memory 或子 Agent denylist 列成 Yuki 的必备功能。#299 已知真实缺陷继续在原审查任务书处理，工具收敛讨论不替代那些修复。

## 7. 原始来源

所有 Memoh 代码链接固定本次核实 SHA；行号来自浅克隆同一文件。官方文档为本次实际读取的页面，未来可能变化。原始源码和文档缓存仅用于本地调研，没有写入产品或运行服务。

- 仓库与本轮快照：[felinics/Memoh](https://github.com/felinics/Memoh)、[固定树](https://github.com/felinics/Memoh/tree/7e40b52ec46d076c17d93e578f5fc18f88742f97)。
- [M01 注册 Provider][M01]；[M02 会话工具装配][M02]；[M03 模型定义转换][M03]；[M04 请求使用工具定义][M04]。
- [M05 文件与终端][M05]；[M06 后台任务][M06]；[M07 历史可见范围][M07]；[M08 消息][M08]；[M09 历史工具][M09]。
- [M10 Memory 装配／写入][M10]；[M11 Memory 证据过滤][M11]；[M12 Skills][M12]；[M13 Schedule][M13]。
- [M14 Federation][M14]；[M15 扩展管理授权][M15]；[M16 ACP 工具源][M16]；[M17 运行中能力刷新][M17]。
- [M18 搜索][M18]；[M19 网页读取][M19]；[M20 浏览器][M20]；[M21 子 Agent 工具过滤][M21]；[M22 执行与失败][M22]；[M23 结果边界][M23]。
- [M24 终端等待与执行][M24]；[M25 工作区目标解析][M25]。
- 官方文档：[Sessions][D01]、[Skills][D02]、[Approvals][D03]、[Memory][D04]、[Schedule][D05]、[Search Providers][D06]；[文档入口](https://docs.memoh.ai/)、[文档索引](https://docs.memoh.ai/llms.txt)。

[M01]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/cmd/internal/core/providers.go#L1099-L1171
[M02]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/runtime/native/agent.go#L542-L675
[M03]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/toolexec/tool_definition.go#L13-L43
[M04]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/runtime/native/loop_generate.go#L400-L445
[M05]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/container.go#L174-L337
[M06]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/background.go#L81-L131
[M07]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/history_visibility.go#L10-L60
[M08]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/message.go#L36-L123
[M09]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/history.go#L80-L119
[M10]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/memory.go#L105-L242
[M11]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/memory.go#L371-L443
[M12]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/skill.go#L32-L119
[M13]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/schedule.go#L88-L220
[M14]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/federation.go#L35-L78
[M15]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/capability.go#L117-L216
[M16]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/native_source.go#L118-L209
[M17]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/runtime/native/capability_refresh.go#L63-L139
[M18]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/web.go#L51-L132
[M19]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/webfetch.go#L52-L112
[M20]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/browser.go#L122-L165
[M21]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/subagent_policy.go#L7-L31
[M22]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/toolexec/tool_exec.go#L12-L175
[M23]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/output_limit.go#L13-L47
[M24]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/container.go#L1112-L1167
[M25]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/container.go#L587-L623
[D01]: https://docs.memoh.ai/guides/sessions.md
[D02]: https://docs.memoh.ai/guides/skills.md
[D03]: https://docs.memoh.ai/guides/approvals.md
[D04]: https://docs.memoh.ai/guides/memory.md
[D05]: https://docs.memoh.ai/guides/schedule.md
[D06]: https://docs.memoh.ai/integrations/providers/web-search.md
[M26]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/internal/toolname/toolname.go#L26-L101
[M27]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/schema_golden_test.go#L22-L91
[M28]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/container.go#L295-L329
[M29]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/sessionmode/sessionmode.go#L12-L23
[M30]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/inbound/channel.go#L653-L676
[M31]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/handlers/local_channel.go#L2420-L2440
[M32]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/ask_user.go#L41-L126
[M33]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/application/service.go#L1077-L1083
[M34]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/outbound.go#L797-L803
[M35]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/manager.go#L312-L320
[M36]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/message.go#L272-L329
[M37]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/tool/container.go#L145-L171
[M38]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/agent/runtime/acp/client/session.go#L309-L360
[M39]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/mcp/http_tools.go#L138-L151
[M40]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/handlers/local_channel.go#L2784-L2798
[M41]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/cmd/internal/core/providers.go#L1055-L1090
[M42]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/db/postgres/queries/channel_routes.sql#L37-L48
[M43]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/route/thread.go#L80-L92
[M44]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/adapters/qq/qq.go#L95-L105
[M45]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/adapters/telegram/telegram.go#L229-L245
[M46]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/adapters/discord/discord.go#L69-L84
[M47]: https://github.com/felinics/Memoh/blob/7e40b52ec46d076c17d93e578f5fc18f88742f97/internal/channel/adapters/local/web.go#L27-L40
