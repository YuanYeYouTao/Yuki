# 手动 Gemini / DeepSeek 缓存核查

入口是 `python -m tools.cache_probe`。只有明确传入 `--live` 才运行真实 API；
不接入 CI、定时、启动检查或默认测试发现。普通 pytest 仅使用 MockTransport。

```powershell
uv run python -m tools.cache_probe --live --profiles private-model-profiles.toml `
  --profile selected-gemini --output .cache/cache-probe.json `
  --scenarios C01,C02,C03,C04,C05,C06 --warmups 1 --samples 3
```

默认 `--provider gemini`；DeepSeek 对比显式传 `--provider deepseek` 并选对应 Profile。
使用所选 Profile 的真实 Gemini、Chat Completions 或 DeepSeek Responses 适配器，
不按内容更换路由。`--protocol responses` 可显式覆盖实验 Profile 的协议，报告同时保留原协议；
它不修改生产 catalog，也不把 Chat Completions 结果标成 Responses。不同 Provider 的窗口、输出与结构化模式会记录在报告中。

也可把 `{ "profile": <ModelProfile JSON>, "api_key": <key> }` 经私有管道传入
`--route-stdin`，用 `--base-url` 指定 SSH 隧道。凭证不传命令行、不写报告。
每轮自动生成新的命名空间。`--prefix-characters` 调整合成长历史长度，
`--delay-seconds` 是请求间隔；补测应保留前一次完整报告。

脚本创建临时 SQLite、原件目录和合成身份，使用真实 TaskModelExecutor、所选协议
序列化器与持久仓库。它不启动 Application 生命周期、网关、调度器或插件，也不连接
生产数据库。业务工具仅读取合成数据；发消息、联网或其他副作用不执行。

默认 tools 来自隔离应用的完整 MainAgentContract。生产扩展声明可能更多，报告给出
实际数量与 hash。`--manifest ignored-export.json` 可加载只读导出的固定
`{ "tools": [{ "name", "description", "parameters" }] }`，不运行插件。
每个主请求保持相同完整声明与 Profile 设置；摘要请求按正式合同独立无工具。

| 场景 | 实际路径与范围 |
| --- | --- |
| C01 | 同一公共前缀更换末尾问题；另发独立命名空间对照，不称缓存关闭 |
| C02 | 原生模型生成的读取调用、opaque 签名和配对结果继续进入真实 serializer |
| C03 | 原 WorkRepository/WorkSession 做 W1→W2→W1；账本新聊天、原 context_note 发布与读取 |
| C04 | 原 ToolArtifactRepository 外存、模型按 handle 回读；显式新请求退出正文与旧签名尾部 |
| C05 | 第一段后关闭 SQLite pool，重建 repository/session；按原 effect ID 只读对账、预算保留 |
| C06 | 原 compact_ordinary 的付费分页摘要、新模型链与后续请求；无新增 Work，辅助调用单列 |
| C08 | 同一个 transcript 保留每次完整 assistant/opaque 输出并追加下一条 user；不混摘要或对照 |
| C07 | Profile 声明的真实 inline 图片；MCP 已退役；该场景只核查声明的原生图片输入 |

C03 使用合成公共前缀直接驱动仓库和 session，不能代替完整 Main 编排、隐私或真实群聊
验收。C05 是 SQLite/repository/session 重开，不冒称完整进程重启。C06 默认是普通
整理；`--compaction-mode work` 可另查原 Work 摘要路径，不改变默认场景。普通两次发送后
整理另有完整离线 Processor/Runner 回归。模型不选择预期工具、非法摘要
或请求失败会保留原结果，不能换样本只选最好一次。

报告不含请求正文、模型正文、签名、headers、凭证。它逐次记录 HTTP 物理请求，
Work 辅助页另存合成的 section/允许引用与来源类别；非标准引用仅存散列。
包括配置重试、错误响应、无响应与辅助摘要；与 model_invocations 的计数交叉核对。
Gemini input 已含 cached，已知输入加权比例是 `sum(cached)/sum(input)`。
显式零、缺字段和不合法计数分开；缺 cache write 不造零。冷、热、对照和摘要各自汇总，
总数包含全部调用。`unknown_usage` 指总 token 是否可计量，cache 未知另行统计。
每个原计量字段分别记录已知与未知覆盖；没有已知数字时小计为 `null`，只有实际返回
的零才计为零。部分已知数字的小计不能冒充全部请求总数。
`latency_seconds` 保留原响应头到达耗时；新行另写同值的 `header_latency_seconds`
及读完响应体后的 `complete_response_latency_seconds`，两者起点均为物理 request hook。
缺完整响应体计时的旧样本不能用响应头 P95 代称完整响应或用户收到答复的延迟。

`first_difference` 只报位置；比较实际发往配置 endpoint 的 body，固定部分含完整工具
与 generation 设置，contents 按实际 role/part 顺序展开，避免连续 user 合并造成假差异。
记录内部 request_chain_id；它不等于 Provider session header。C08 不注入会话 header。
相同输入片段和规范化 JSON 的最长公共字节只报长度，不保存正文。
相邻物理请求与同场景主/摘要各自序列分别比较，避免把无工具摘要链的设置当成主链变化。
经上游代理时，这只证明 Yuki 发往配置 endpoint 的 wire，不证明最终上游完全相同，也不证明账单。
本实验是受控合成流量，不能当自然群聊平均值或设置固定命中率门槛。

`tools/cache_probe_materials.py` 另提供首份/差量方案的实际 Main A/B/C 重放。
A 从显式提供并核验为 24da979 的源码导入；B/C 共用冻结的候选源码、0107
合成 seed、一致静态合同和工具清单。B/C 执行实际 0108 清退，B 只在隔离进程中
改为整份提供剩余有效资料。原工具 schema/静态规则的部署变化另列。

```powershell
.venv/Scripts/python.exe tools/cache_probe_materials.py `
  --baseline-root ignored-original-24da979 `
  --output .cache/materials-offline
```

默认使用固定回包和 MockTransport，实际经过 Processor/Main/serializer；每个物理
请求、最终 fragment/map、原 wire 字节及结构体积保存在输出目录。空值、稳定值、
变化、恢复 A、清空、过期、不同主体、cache 行回收及旧 snapshot/摘要/真实 note
各自留样。行回收不是进程重启，合成资料不代替生产样本；mock usage/cache/费用未知。
离线准备可以用 `--profile-json` 或私有管道 `--route-stdin` 选择实际 Profile，仍只走
MockTransport，不发真实 API。Gemini、DeepSeek Chat Completions 和 Responses 各用现行
适配器的原协议和实际模型；三组在同一个实验内共用同一 Profile，跨协议结果分别报告。
测量 Profile 若显式加 tools 或改协议，应单列与原 saved catalog 的差异，不写回生产路由。

真实输入重放显式加 `--live --route-stdin --replay .cache/materials-offline`，
并指定新的 `--output`。`--order ABC,CBA` 平衡组序，`--warmups` 为每组每块的
预热份数，`--limit` 限定每组每块正式请求数；全部尝试与预热均保留。
私有 pipe 使用上述 profile/key 格式；`wire_options` 只序列化显式字段，不能把
跨协议默认字段展开成实际配置。数字缺字段和 HTTP/non-JSON 失败保持未知。
固定输入按原适配器的 endpoint/auth 发送捕获的正文，实际 route 的 provider/protocol/model
必须与捕获 Profile 相同；失败保持原协议，不降级。响应的原数字 usage 及嵌套字段 presence
另外保留，缓存、思考和完整账单的缺值不补零。字节公共前缀不是 token 缓存命中率。
Profile 是路由及默认值，Main 的实际 ChatRequest 可以覆盖生成设置。新 hook 与 manifest
另存实际 wire 的 generation shape（不存 instructions 或完整 schema），包括实际输出上限、
thinking/reasoning 和格式；不得用 Profile 的默认值代称最终请求。该合成 Main fixture 的
Settings 会要求 8192 输出及 thinking/low，须与所选 Profile 的默认值分别报告。

加 `--closed-loop` 使用四步真实 Main 只读任务，调用 `get_my_capabilities(mode=summary)`
后在内部答复当前线索、变化和原 note 证据。实验 controller 只把本组模型实际内部答案
引用到下一条合成 user 入站；每次原生工具续轮和 opaque 保持在本组原 Main 链上。
它不制造 QQ 发送或公众回复，不启动真实 Bot、网关、自动化或插件；不同组不共享签名。
该模式没有额外预热，使用 `--warmups 0` 明确记录。离线固定工具回包只核本地链路，
真实 Provider 是否接受续轮、实际模型是否选择工具及保留事实，仍由付费实验核对；
失败和未选工具均保留，不能替换样本称通过。它不代替自然 QQ 行为验收。
付费闭环使用既有 trace codec 保存去 opaque 的诊断副本及原 body 字节数/hash，
完整签名不写实验报告；该副本不能作为签名重放输入。实际上游账号、最后一跳、
缓存热度、搜索/存储费和完整账单缺样时标未知，SSH relay 等实际网络路径单列。
