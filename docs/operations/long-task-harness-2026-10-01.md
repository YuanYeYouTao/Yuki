# 长任务 Harness 交付记录

本轮基线 `ffca08a1255ea8b4fff8d403c7467e7c43cc51ef`，开发分支
`codex/long-task-harness-compaction`，PR [#216](https://github.com/YuanYeYouTao/Yuki/pull/216)。
本文记录实施和验收边界，不替代现行合同。

## 任务书核对

| 项目 | 本轮实现 | 对应验证或证据 |
| --- | --- | --- |
| F1–F4 工具事实与原结果 | 类型化 outcome 先于正文裁剪；未决效果精确查询；UTF-8 字节预算；原 Work/effect 拥有结果引用与活跃留存 | 结果超过展示页、中文字节超限、保存失败、超过 64 次后重启、生命周期与分页回归 |
| F5–F8 请求容量与 Work 压缩 | 下一完整请求计量；显式 Profile 容量；独立无工具摘要；严格来源引用与资料 schema；近期完整配对回合；同源候选最后提交 | 多次压缩/重启/steer、巨大源、无改善/容量暂停、非法候选、最终保存失败、分页与公平让出恢复 |
| F9 元数据增长 | 计量有界聚合；chain/展示历史有独立窗口；协议原对象外置不因语义压缩失去执行身份 | protocol continuity、GC、容量与长期输入回归 |
| R1–R6 Rollup | 按完整请求余额规划连续后缀；原身份/回复/提及投影；完整超大事件分片；显式 emergency；结构化连续叙述/开放事项/更正；有界读取与原 CAS | 来源尾部、当前事件一次、仅提及、跨批回复、generation/hold/晚派生来源、结构引用与真实协议格式回归 |
| T1 不可变产物 | artifact offset/limit 真实分页，原文件不重写 | 多页正文按字节还原 |
| 默认累计限制 | 新 root/child/automation 累计 limit 为 NULL；旧有限预算与原计数保留；quantum 只让出 | 0087 迁移、超过旧限继续、有限预算停止、父子共用根计量 |
| 子任务并行与完成 | 真实并发 claim/run；活动/排队容量代替累计四个限制；目录分页；精确未结束查询；停止/cancel join | 两 child 同时运行、累计超过四个、跨页、取消与关闭、前台预留 |
| 进度和 steer | 原 send_message 报告合同与模型委派指引；同源唯一 queued Work 追加；waiting_external 仍按原条件 | 入站/分段间隙/source 权限/多候选/等待回归；不添加阶段状态机或定时报告器 |
| 动态资料减肥 | 有界 SQL 最近/可续接 Work 摘录；完整 goal/wait 按 get 查询；关系风格只常驻一份 | 16 个大目标目录、完整目标/等待回查、原活动目标与来源守卫 |
| Gemini continuation | 私有 native tail 留在原 Work；已提交获准公开历史前缀不整体重建 | 实际主入口→HTTP serializer 七请求、SQLite 重开、同 actor 下一轮与换 actor；静态字段和 parts 前缀检查 |
| 代理服务器核查 | 有效 tunnel/路由、三请求 UUID 和最终上游序列化关联、usage 分层核对、countTokens/显式缓存限制 | [代理逐跳审计](provider-compaction-audit-2026-10-01.md)；无必要 AGM 补丁或保温调用 |
| 成本与热配置 | 独立聊天/Work 水位、完整输入窗口政策与模型硬容量；协议存储全局热政策；现有 WebUI 目录 | [成本模拟](context-cost-simulation-2026-10-01.md)、可复现脚本、跨 scope 校验和真实管理权限测试 |

参数初值为聊天 96,000 / 0.90 / 0.60，Work 128,000 / 0.90 / 0.50；它们是热配置政策。
三条匹配真实请求的估算/usage 比例约 1.78，不是通用 tokenizer。
模拟同时限制必要来源/近期原文/压缩频率，所选政策不宣称在所有价格、缓存与增长条件下费用最低。

## 验证与交付状态

开发中采用相关定向测试。终局首次全量暴露的旧测试合同和恢复例外已修正并定向回归；
新增结构化摘要、分页恢复、配置和 Gemini 前缀另有针对性验证。
最终以最新 head 的 Quality CI 为合并门禁，不放宽 Memory 性能冻结基准。

完成提交另外核验已接纳但尚未准备好的媒体输入：原 Work 转为输入准备等待，
准备后沿原 ID 唤醒，保留原等待条件与计数；实际终态提交才取消等待，不能误报 completed。
定向 SQLite 回归覆盖 ready/notready 两种检查后入库竞态。

`dfb368b` 的 CI 中 Memory 功能指标通过，检索 p95 为 145.025ms；仅固定两个样本为
145.025/153.096ms，其余 21 个均不超过 27.186ms。Windows/Linux 定向两例未重现。
质量 Harness 增加显式 opt-in 的内容无关阶段诊断，以同一次检索拆分编译、连接池、
线程队列/SQL/唤醒、GC 和 loop lag；默认关闭，不预热、不剔除样本、不改原 wall gate。
当前证据不能将尖峰归因为 SQLite、GC 或本轮业务修改。

后续 CI 阶段样本中一次检索 wall 118.154ms、GC 103.950ms、实际 worker SQL 1.093ms，
说明该样本的主要停顿在事件循环而非查询；不能反推此前所有样本的根因。
探针按 case 结束释放 engine 引用，避免整套持有导致生存期偏差。
独立 Memory CI 固定 uv managed Python 3.12.13；已在 Linux 核验其 SQLite 为 3.53.1，
与冻结基准一致。默认不启用探针执行原 gate；仅失败时另存诊断报告，原报告不覆盖。
主 Python 回归仍保留系统 Python 的兼容性验证，不修改基准或放宽性能阈值。

最终开发提交 `d83fd992e8219bbaf2333030f114cfdd9877975c` 的
[Quality CI](https://github.com/YuanYeYouTao/Yuki/actions/runs/36893361046) 六项全部通过。
Python 全量为 2204 passed / 1 skipped；未开启诊断探针的 Memory 19 案例通过，
p50 16.797ms、p95 29.304ms，冻结基准无回归。
PR #216 已合并为 `ea446d64edf8a428df5a760d6dcedb49ba13a086`；合并树与已验证开发提交一致。
项目版本仍为 3.9.0 开发基线，未创建正式 Release。

## 线上验收边界

2026-10-02 01:07:37（Asia/Taipei）启动固定镜像
`ghcr.io/yuanyeyoutao/yuki-qqbot:ops-ea446d6`，服务器 image ID
`sha256:1c4ebb760b7862fb08ef9018d1f573c039d7f355100be64c5bd534964163a2c6`；
架构 amd64、OCI revision 与合并提交一致。上传 tar 的本地/远端 SHA256 一致：
`f1d8b1ebc24f9493e82132407dcd868b63fb62d6cd99ac50c570cd3ddaa46283`。

从实际 Bot 标签读取 33 个 Compose 叠加文件，追加最终参数覆盖，只停止并替换 Bot。
停写备份目录 `/opt/yuki-qqbot/backups/pre-harness-20261001T165936Z`，包括原配置、
完整 data、工作区、环境工作区和 social-transfer；独立 SQLite backup SHA256 为
`aa672667eec8264d327109020ea4c2b54e3e5a29f855a9d79c121b4e7785273e`。
数据库完整性/外键和已有证据引用检查通过。先在无网络副本演练，再离线迁移生产库
`0085 → 0088`，两次检查均通过。

迁移前、迁移后、启动后按原 ID 对账：6 个活动 Work、60 条效果回执、20 条输入、
6 份 journal、6 份预算、6 份恢复记录、15 条投递事实均保留，原回执/结果哈希一致，
没有状态变化、缺失锚点或计数重置。新存储引用为 6 条。3 条历史 send_message 的证明
不足，明确保留 `delivery_verification_required` 和 uncertain 围栏；没有重发或自动解除。
此对账证明事实保留，不证明原 suspended/waiting Work 已实际继续推进。

新 Bot ID `3e8150ec0ddd408f6912870167939c6c23ad7b680b6bd5acff47f40743cfceda`，
SnowLuma ID `9e7a1a89696eae3922e4b40daee295edebc0fb60e18c7c9bf5a15efe7a1c8b85`
保持原值。新 Bot healthy、零重启；livez/healthz、OneBot、Work/等待、子代理、
自动化和插件 worker 正常。Manager 与 AGM tunnel 服务 active，持久环境容器 running，
原挂载保留，WebUI `/ui/` HTTP 200。

实际配置读取确认聊天 96000/.90/.60、Work 128000/.90/.50、摘要输出 8192、
Rollup 字符 16384，协议存储 2 GiB/单对象 64 MiB/磁盘预留 64 MiB；现有配置目录
254 项。累计无限与公平 quantum 分离，当前 quantum 32 tools/24 requests。
活动模型 Profile 仍为 connection_2d4ba9aad301 / Gemini gemini-3.8-flash，显式 input/context
硬容量为 NULL；政策窗口不是上游认证。Memory contested 12/active contested 25、
dream failed 65 及 MCP 0 connected 是升级前已有状态，没有将其报告为全部健康。

启动后首次只读自然流量观察没有新模型请求，因而不能报告新的缓存率或新请求路由验收。

本地结构校验与模拟不能证明模型永远不遗漏语义、一定主动分工或阶段报告。
单条小型无工具 JSON 格式探针只证明当前代理能接受摘要 schema，不是长期任务验收。
真实 QQ 长任务、多次压缩后的模型表现与跨轮缓存改善需自然流量观察；不发送测试群消息、
不靠额外模型请求刷缓存，也不把部署健康冒充真人验收。
