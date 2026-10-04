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

脚本创建临时 SQLite、原件目录和合成身份，使用真实 TaskModelExecutor、Gemini
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
| C07 | Profile 声明的真实 inline 图片；未连接外部 MCP，MCP 部分如实标不适用 |

C03 使用合成公共前缀直接驱动仓库和 session，不能代替完整 Main 编排、隐私或真实群聊
验收。C05 是 SQLite/repository/session 重开，不冒称完整进程重启。C06 默认是普通
整理；`--compaction-mode work` 可另查原 Work 摘要路径，不改变默认场景。普通两次发送后
整理另有完整离线 Processor/Runner 回归。模型不选择预期工具、非法摘要
或请求失败会保留原结果，不能换样本只选最好一次。

报告不含请求正文、模型正文、签名、headers、凭证。它逐次记录 HTTP 物理请求，
Work 辅助页另存合成的 section/允许引用与来源类别；非标准引用仅存散列，分类原因只记是否非空。
包括配置重试、错误响应、无响应与辅助摘要；与 model_invocations 的计数交叉核对。
Gemini input 已含 cached，已知输入加权比例是 `sum(cached)/sum(input)`。
显式零、缺字段和不合法计数分开；缺 cache write 不造零。冷、热、对照和摘要各自汇总，
总数包含全部调用。`unknown_usage` 指总 token 是否可计量，cache 未知另行统计。

`first_difference` 只报位置；比较实际发往配置 endpoint 的 body，固定部分含完整工具
与 generation 设置，contents 按实际 role/part 顺序展开，避免连续 user 合并造成假差异。
记录内部 request_chain_id；它不等于 Provider session header。C08 不注入会话 header。
相同输入片段和规范化 JSON 的最长公共字节只报长度，不保存正文。
相邻物理请求与同场景主/摘要各自序列分别比较，避免把无工具摘要链的设置当成主链变化。
经上游代理时，这只证明 Yuki 发往配置 endpoint 的 wire，不证明最终上游完全相同，也不证明账单。
本实验是受控合成流量，不能当自然群聊平均值或设置固定命中率门槛。
