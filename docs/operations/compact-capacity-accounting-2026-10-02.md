# 普通续接容量计量核查与交付记录

## 核查对象与当前状态

用户报告 2026-10-02 13:31:39（Asia/Taipei）开始搜索，13:32:03 收到上下文容量停止提示，
并指出该问题频繁出现。该轮生产为 `ops-8ca2e17`，实际启动于
`2026-10-01T21:55:23.469019063Z`（台北 2026-10-02 05:55:23）。
前一轮历史与交互修复的上线事实见[原交付记录](history-interaction-harness-2026-10-02.md)。

本记录固定本轮只读诊断与自然流量计量，并记录容量计量修正的本地实现和定向验证。
代码修复已通过最新 head CI 并合并 PR #220；部署状态与后续观察以本文交付核验为准。
诊断不发送真实 QQ/模型请求，不调整路由、窗口或数据库，不重发已有结果。

## 13:32 普通续接停止的已知事实

内部 turn 为 `f1844bf1dbbb41f98a7d0005b43a81f0`，容量错误 trace 为 `52918`。
主 Agent 与运行时审阅者复核的调用顺序是：首请求成功、搜索工具成功，随后普通续接的本地
容量预检拒绝派发。原 turn 的 `work_id=NULL`，不是已接纳的持久 Work；该错误不是模型服务端
返回超窗，也不把搜索成功推断为整个用户目标完成。

本轮同时复核本地请求估算的 Host 元数据虚增：旧估算路径对完整 `asdict(ChatRequest)`
做 JSON 计量，包含诊断/关联字段、消息的空默认字段及函数声明中仅供 Host 使用的元数据。
主 Agent 与运行时审阅者已按真实 serializer 核对这些字段。修正只改变估算视图：
排除确定不发送给模型的 Host 元数据与默认空字段，不删除实际签名、媒体、工具回执，
不改写已提交的请求前缀。具体本地实现及验证边界见最后一节。

这里的 token 预算与估算是本地容量政策，不是 Provider 返回的真实 input token 计量。
缓存命中的 input 仍属于模型上下文，命中率不能用来扣除容量预算内的历史。
即使估算中的 Host 元数据被排除，也不承诺所有真实长上下文都能够继续；真实容量不足应
保留原结果并准确停止。

## 缓存统计窗口与口径

缓存统计采样时刻为台北 **2026-10-02 13:38:33.147329**（UTC 05:38:33.147329）。
使用 `model_invocations` 的 `task/created_at` 索引，每窗口最多读取 5001 行检测截断，
统计最多 5000 行；以下三个窗口均未截断。读取仅含调用与用量元数据，不读取提示词、
聊天正文或 Provider 签名。

三个窗口仅有同一 Profile `connection_2d4ba9aad301`、Provider `gemini`、
model `gemini-3.8-flash`。等长前后窗口各为 7 小时 43 分 10.147329 秒：

| 窗口 | Asia/Taipei 起止 | UTC 起止 |
| --- | --- | --- |
| 部署前等长 | 10-01 22:07:42.852671–10-02 05:50:53 | 10-01 14:07:42.852671–21:50:53 |
| `ops-8ca2e17` 部署后 | 10-02 05:55:23–13:38:33.147329 | 10-01 21:55:23–10-02 05:38:33.147329 |
| 最近一小时 | 10-02 12:38:33.147329–13:38:33.147329 | 10-02 04:38:33.147329–05:38:33.147329 |

部署前窗口截止停写备份时刻，不把停止/替换容器的间隔混入比较。
统计对象是成功的逻辑 `chat_agent` 调用，并分别保留 input 缺失和 cache 缺失。
模型调用失败数不代替容量停止次数；派发前拒绝可能根本不进入模型 invocation。

| 指标 | 部署前等长 | 部署后 | 最近一小时 |
| --- | ---: | ---: | ---: |
| 成功 chat 请求数 | 209 | 79 | 18 |
| input 缺失的成功请求 | 0 | 0 | 0 |
| cache 已知 / 未知请求 | 184 / 25 | 62 / 17 | 13 / 5 |
| 全部已知 input token | 7,601,685 | 2,980,917 | 780,974 |
| cache 已知样本的 input token | 6,747,293 | 2,309,968 | 550,729 |
| cache 已知样本的 cached token | 5,830,107 | 2,008,027 | 488,925 |
| cache 已知样本的未缓存 input token | 917,186 | 301,941 | 61,804 |
| cache 未知请求的 input token | 854,392 | 670,949 | 230,245 |
| 输入加权的已知命中率 | 86.4066% | 86.9288% | 88.7778% |
| 未知全按 miss 的保守下界 | 76.6949% | 67.3627% | 62.6045% |

加权已知命中率为 `sum(cached)/sum(input)`，分母只含 cache 已知的请求；
不是每请求百分比的平均值。cache NULL 不等于 0；保守下界是处理未知值的情形假设，
不是观测到的实际 miss。三个窗口均未出现 cached 大于 input 的异常。
成功请求的 physical request count 均有计量且为 1；指标仍按逻辑请求统计，不能将其
扩展为失败重试在内的全部物理请求用量或账单成本。

前后已知命中率提高 **0.5222 个百分点**，不据此宣称修复显著改善缓存。
部署前 5 个会话，最大会话占成功请求 85.17%；部署后 4 个会话，最大会话占 51.90%。
最近一小时为 2 个会话，最大会话占 83.33%。平均已知 input/请求分别为 36,371.70、
37,733.13、43,387.44 token；请求数、上下文长度和会话负载均不同。
同 Profile/model 不证明设置或任务分布完全一致，也不区分普通聊天与 Work 的全部组合。

采样脚本及机器结果保存在本地 ignored `.cache`，不进入生产或 PR：
`harness_cache_window_audit.py`、`harness-cache-windows-ops8ca2e17.json`。
原 `harness_cache_audit.py` 的缺 input 请求曾被排除，本次明确单列该项，未重复扫生产更新样本。

## 部署后容量停止频率

频率采样截止台北 **2026-10-02 13:44:28.245911**（UTC 05:44:28.245911），
起点仍为实际启动 UTC 21:55:23。只计 `turn_error`，不叠加外层
`chat_processing_error` 或模型 `model_error`；条目数与 distinct turn 数分开核对。

| `turn_error` 分类 | 条目 / distinct turn | `work_id=NULL` | 已接纳 Work |
| --- | ---: | ---: | ---: |
| `model_request_capacity` | 6 / 6 | 6 | 0 |
| `prompt_dynamic_capacity` | 0 / 0 | 0 | 0 |
| 其他 capacity | 0 / 0 | 0 | 0 |

6 次均为 `user_message`，台北时刻分别是 08:25:39、10:03:04、10:08:36、13:09:02、
13:09:55、13:32:03。另有 1 条非 capacity `turn_error`；payload 缺失与重复容量 turn 均为 0。

现行 trace 没有全局 `kind/created_at` 索引。最初按 kind 反向扫描被只读脚本的 15 秒
截止中断，没有把未得结果当作 0；最终先按 invocation 索引定位停写前原 turn，再按 turn
索引取得 trace `50669`（UTC 21:48:34，早于启动 cutoff），使用主键 ID 范围筛选
`turn_error`。512 条上限仅取得 7 条，未截断；原持久化 payload 的大小和摘要核验通过。
机器结果为 ignored `harness-capacity-frequency-ops8ca2e17.json`。

这只覆盖仍然可见的 Runner `turn_error` 证据，不是所有类型容量事件的总数。
`PromptCapacityError` 可发生在 compose、进入 Runner turn span 之前；已接纳 Work 的容量
退出也可能被内部恢复处理。因此本表的 0 不能推断整个窗口从未发生编译/Work 容量事件，
日志写入失败、隐私删除或过期记录也不能由现存数据推断为 0。

## 本地实现与定向验证

`model_runtime/capacity.py` 使用仅供估算的模型输入视图，排除函数工具的 Host 目录字段、
消息未发送的默认空字段和请求诊断哈希；`services/chat.py` 的历史工具成本同步使用
`estimate_tools_tokens`。实际 `ChatRequest`、Provider serializer、冻结工具声明和请求
payload/prefix 均不改变。窗口、输出预留、硬容量检查和原执行计量不重置，也不扩大。

完整参数 schema、实际正文、非空 reasoning、调用与结果、native/结构化输出声明仍计量。
Responses 投影的 `content` 是原 opaque item 的预算镜像，真实 serializer 使用 tagged
原 item；因此只计完整原 payload 一份，不解析或裁剪其内部签名与字段。媒体继续使用
原有每项 4096 的本地预留，识别范围收窄为 typed `ChatImage` 和 Gemini 原生
`content.parts[].inlineData/inline_data` 媒体位置；未知 opaque 结构完整计量，不因任意
嵌套键名相同而替换成媒体成本。已知估算视图顶层 `tools` / `response_format` 的完整
schema 不进入媒体替换。schema、Gemini `functionCall.args` / `functionResponse.response`、
Claude `tool_use.input` 中名为 `data_url`、`inlineData`、`inline_data` 的业务字段都完整
计量，不改变原始 payload。

本轮是普通请求链，`current=None` 不等于已接纳 Work。修复不新增普通链的压缩 owner、
持久 Work 或摘要状态机，不通过借用 Work 摘要器来绕过原 ownership、前缀和预算边界。
初始历史与 Work 压缩继续遵守现有政策；本地输入估算仍是近似值，不能称为逐协议严格
上界或 Provider 实际 input token。真实内容过大时仍会准确停止。

只读容量构造结果保存在 ignored `.cache/incident-1331-replay.json`：

| 本地估算对象 | 旧完整 neutral 对象 | 新模型输入视图 |
| --- | ---: | ---: |
| 原首请求 | 88,753 | 76,717 |
| 搜索成功后续接构造 | 96,018 | 83,965 |

真实首请求的已记录 Gemini wire 本地估算为 76,227；续接构造 wire 为 83,479。
首请求的原始实际 wire 字段 `contents/systemInstruction/tools/toolConfig` 比较一致。
续接构造中的 138 字节签名资料使用 ASCII 等长替代，只用于容量几何核对，**不是无损签名
重放**；独立 serializer 回归以合成签名验证保留机制，不证明该生产签名已经无损重放。
以上数值均不是 Provider 用量。本次构造实际 HTTP 请求数为 0，也没有真实 QQ 发送；
它表明该构造在现有窗口内通过新估算，不是生产再次执行成功或真人验收证据。

最新 opaque 业务 JSON / 媒体位置补修后，必要组合 **36 passed，4 条既有 SQLAlchemy
警告，9.77 秒**：容量输入计量 28、普通搜索续接 2、普通轮容量停止 6。新增实际 serializer
对照覆盖 Gemini 调用参数/响应业务 JSON 和 Claude 工具输入中的三种媒体同名键；它们
增大时估算与实际 wire 都增长，原签名与 payload 不变。真实媒体位置仍保留预留，未知
opaque 形状不得享受媒体替换。

前一 schema 边界冻结版本主组合为 **52 passed，30 条既有警告，39.38 秒**（计量 18、
普通搜索续接 2、普通轮容量停止 6、Work 压缩 26），独立必要组合另有 **24 passed，
2 条既有警告，1.89 秒**。更早版本的关联检查为 119 passed。这些结果互有重叠且没有
纳入最后 opaque 补修，不能相加或冒充最新源码的一次完整验证。

回归覆盖 Host 元数据增大不再虚增容量，实际正文/schema/签名/结果增大仍计量，真实媒体
预留不丢失，Gemini 与 Responses 实际序列化不改变，原签名与工具回执保留，以及真实
容量不足时不会继续 HTTP 派发或新建 Work。最新补修的定向 Ruff、格式检查与 Linux
平台 Mypy 两个 source 通过。主 Agent 在 opaque 补修前还核验全仓 Ruff、1062 文件格式、
Linux 平台 Mypy 683 个 source、`v3.9.0` 发布门禁与 diff 检查通过；这些全仓结果需按
最终 head 继续核验，发布门禁通过不代表创建 Release 或部署。

最终实现由主 Agent 验证 **62 passed，30 条既有 SQLAlchemy 警告，37.46 秒**，包括计量
28、普通搜索 2、普通轮容量停止 6、Work 压缩 26。独立审阅无阻塞；最终实现全仓 Ruff、
1062 文件格式、Linux Mypy 683 source、`v3.9.0` 发布门禁及 diff 检查通过。

以上本地验证在提交 PR #220 前完成；最新 CI、合并和部署事实按下文分别核验。
本记录前面缓存及停止频率统计仍属于修正前 `ops-8ca2e17` 的自然流量证据。

## CI 与合并

[PR #220](https://github.com/YuanYeYouTao/Yuki/pull/220) 测试 head 为
`661f0191a38899f470a9b833ea1e99c4f20483c8`。
[CI 36972974752](https://github.com/YuanYeYouTao/Yuki/actions/runs/36972974752) 六项全部成功；
完整 Python suite 为 **2312 passed、1 skipped、1700 条既有警告，1076.08 秒**。
前端 88、语音 worker 15、三个插件合同/行为检查 4/84/32 也分别通过，fresh Alembic 安装通过。
这些检查不等于实际 QQ 能力验收。

2026-10-02 台北 **14:40:40** 合并，merge commit
`d090a052516e496822be1a92abfbec06daa49613`。
正式构建使用干净的该 merge tree，与测试 head 的完整树比较无差异。
包仍为 `3.9.0`，数据库头仍为 `0088`；没有发布 tag 或 GitHub Release。

正式 image 为 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-d090a05`，linux/amd64，
OCI revision 为完整 merge SHA。安装后的包版本与 15 个关键源码文件 hash 均与测试源匹配，
包括本次 `model_runtime/capacity.py`、`services/chat.py` 及原历史/恢复/汇报链。
镜像 tar SHA256 为
`18dca747544d769d4e68b9425b2c10556977a68e8ed81edab1ac9e3d5cdcafb2`，服务器复核一致。

最终源码的只读容量构造重新核验，结果与上表一致；机器证据为
`incident-1331-replay-final.json`。仍为签名等长替代的容量核对，没有真实 HTTP 或 QQ 重放。

上线前的计量与请求证据只覆盖所列窗口；未知用量不能外推为零或完整上游账单。

## Bot 部署与留存核验

部署脚本从实际 Bot labels 取得 35 份 Compose 文件，核对原 `ops-8ca2e17` 完整 OCI revision、
归档 SHA、已安装版本、架构和证据 helper SHA，检查备份所需空间及余量。
只停止并替换 Bot；未重启 SnowLuma 或上游代理，也没有切换路由、扩大窗口或恢复旧数据库。
失败回退使用原 image 的固定 tag，仅回退代码，保留当前数据。

实际停写备份为
`/opt/yuki-qqbot/backups/pre-capacity-accounting-d090a05-20261002T064316Z`。
SQLite backup API 副本的 integrity/FK/head `0088` 通过，协议引用核验为
**993 protocol objects / 0 tool artifacts**。停写和启动后的留存对账
`comparison.ok=true`，`errors=[]`、`changed_counts={}`：原 **6 Work、60 effects、20 inputs、
6 journals、6 budgets、6 recoveries** 全部保留；预算和稳定回执没有重置或回退。

新 Bot 实际 StartedAt 为 `2026-10-02T06:48:08.462457111Z`，台北 **14:48:08**。
部署退出码 0；主 Agent 与独立审阅均核验 image/full OCI revision/amd64/3.9.0，
running、healthy、restart 0；线上 15 个源码文件 hash 与测试源一致。
`/healthz` status/database ok、OneBot 已连接、Work/child worker running，
`/livez` 与 `/ui/` HTTP 200。

SnowLuma 原 ID
`9e7a1a89696eae3922e4b40daee295edebc0fb60e18c7c9bf5a15efe7a1c8b85` 保持 running/0 restart；
原有 `memory_consistency_healthy=false` 仍存在，不把这项既有问题写成本轮已解决；
本轮没有扩展为 Memory 修复。

机器证据为 ignored `capacity-postflight-d090a05.json`、`capacity-live-installed.json` 与
`harness-capacity-independent-postflight.json`。独立审阅读取本次真实 backup 的已保存报告，
没有重复扫生产数据库，也未制造模型/QQ 请求。

实现、定向验证、最新 head CI、合并与 Bot 部署均完成；普通聊天语义、真实长任务行为及
新版本缓存改善仍需自然流量及用户能力验收，不能由健康检查或短样本宣称通过。

首次上线后计量采样截至台北 **14:52:46**，使用实际启动 cutoff `06:48:08 UTC` 和
`task/created_at` 索引、500 行上限：尚无成功/失败逻辑 chat 调用，缓存率为 UNKNOWN，
不是 0% 命中。没有发送测试或保温请求来补样；更多自然流量观察应另记具体 cutoff。
