# 手动模型缓存核查

入口是 `python -m tools.cache_probe`。只有明确传入 `--live` 才运行真实 API；
不接入 CI、定时、启动检查或默认测试发现。普通 pytest 仅使用 MockTransport。

```powershell
uv run python -m tools.cache_probe --live --profiles private-model-profiles.toml `
  --profile selected-gemini --output .cache/cache-probe.json `
  --scenarios C01,C02,C04,C05,C06 --warmups 1 --samples 3
```

直接读取所选 Profile 的供应商名称和显式协议，由正式模型池选择真实适配器；
自定义连接名称沿原协议使用，不再需要 `--provider`。连接的 headers、协议参数、
超时与重试次数沿原 Profile，实验结束关闭模型池及客户端。
`--protocol responses` 可显式覆盖实验 Profile 的协议，报告同时保留原协议；
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
| C04 | 原 ToolArtifactRepository 外存、模型按 handle 回读；显式新请求退出正文与旧签名尾部 |
| C05 | 第一段后关闭 SQLite pool，重建 repository/session；按原 effect ID 只读对账、预算保留 |
| C06 | 原 compact_ordinary 的付费分页摘要、新模型链与后续请求；无新增 Work，辅助调用单列 |
| C08 | 同一个 transcript 保留每次完整 assistant/opaque 输出并追加下一条 user；不混摘要或对照 |
| C07 | Profile 声明的真实 inline 图片；MCP 已退役；该场景只核查声明的原生图片输入 |

C03 按人选择公共笔记与手写公共前缀的场景已退役，旧报告只作历史记录。统一公共历史
由实际 Main 装配回归和真实 Provider 边界验收。C05 是 SQLite/repository/session 重开，
不冒称完整进程重启。C06 默认是普通
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

`tools/cache_probe_materials.py` 提供当前资料仅当前注入的严格离线重放。
旧端固定为 `ff0dbdf`，用 Git archive 导入；当前端冻结工作区源码。两端在独立的
生产 SQLite 快照副本上执行对应迁移和实际 Main 装配，原快照以只读方式打开。
没有 live、collect、凭据管道或真实请求入口。

```powershell
uv run --offline --no-sync --extra dev python tools/cache_probe_materials.py `
  --contexts .cache/runtime-material-audit-20261012/cache-late-20261012-contexts.jsonl `
  --proxy-contexts .cache/runtime-material-audit-20261012/cache-late-20261012-proxy-contexts.jsonl `
  --snapshot .cache/runtime-material-audit-20261012/production-0107.db `
  --snapshot-contexts .cache/runtime-material-audit-20261012/production-context-samples-03/yuki.jsonl `
  --output D:/Temp/yuki-current-material-main-replay
```

只传 `--contexts` 可单独整理实采原件。原上下文、prepared 报文、实际响应回执分别记录；
selection 库存不冒充当时实际采用的公共视图，prepared 不冒充已成功派发。
保留原数字 usage 和缺字段覆盖，同执行私有前缀、工具/图片/签名载体顺序仅作事实比较；
原采集已经脱敏的签名不还原，也不发送。

快照装配使用实际 Conversation 的统一公共聊天历史和已有 SELF/定时来源，调用真实
ContextAssembler/MainAgentTurnService，再调用 Gemini、Claude、Chat Completions、Responses
的实际 serializer。固定 system/tools/生成参数取自原采集请求，作为两端共同的实验控制；
各端本身的部署合同另存 `deployment-contract.json`。重复当前输入和空资料是明确标记的
受控离线场景，已有完成的 SELF/定时来源只装配，不重新接纳或执行。
原 neutral 与 prepared 中的实际生成参数分别保留；`effective_request` 只是以关联 prepared
生成字段重建的 serializer 输入，不代表恢复了原 Profile、endpoint 或模型别名的真实路由。
Gemini固定字段与原prepared逐项核对；trace关联、原generationConfig与导出body/file哈希
另存 `gemini_export_provenance`。neutral温度不代表prepared存在对应HTTP字段。
其他协议使用通用serializer配置，不声称复原原收费路由。两端共用当前Python环境，
SP安装pin与旧锁定pin分别记录；此流程不执行语义参与包。

输出分别保存原件覆盖、每端 ChatRequest/body、当前资料字符数、原消息与历史相等性、
实际迁移清退行数、有用 work-note 与 Work/journal/Protocol/发送回执的原始行摘要。
旧私有执行实际依赖的自动来源可能仍保留；公共视图退出资料与私有原证据存续分别报告。
原生产快照和后来采集的短状态有各自时间范围，不能把重新装配结果声称为当时原 wire。

所有生成请求的 token/cache/费用都未测量；字节差和估算不能代替实际路由收费。
原窗口 usage 保持原实验范围，不回填为新方案收益。Gemini 四组及 DeepSeek 付费对照
另行执行与记账，QQ自然流量验收也另列。私有原文与完整 body 只保存在受控证据目录。

`tools/rehearse_current_material_migration.py` 可在已有镜像 Python 环境做 0109/0110
纯迁移演练。传入只读快照、显式副本、当前源码目录和报告路径；脚本对源做只读 backup，
把 Alembic 的 DATABASE_URL 指向副本，分别执行真实迁移。它逐行散列原 Work、journal、
effect、input、预算、Protocol 元数据及原调度回执，比较有用 note 和原版本记录，核查公共
资料清退、旧定义发布、integrity/FK 与源文件不变。只输出计数和散列，不解密协议正文。
该过程不启动 Application、Worker 或联网；镜像依赖版本单列，不能声称是完整 Main 或
语义参与包兼容测试。可在隔离、无网络容器内执行：

```sh
python /replay/source/tools/rehearse_current_material_migration.py \
  --snapshot /snapshot/replay-snapshot.db \
  --copy /results/rehearsal.sqlite3 \
  --source-root /replay/source \
  --report /results/rehearsal.json
```
