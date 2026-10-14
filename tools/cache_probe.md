# 手动模型缓存核查

旧 `tools.cache_probe` 与 Work/ordinary 专用摘要探针已随 #417/#418 退役。
实际已导出的 Main 请求仍可用 `python -m tools.cache_probe_replay --live` 重放，
不执行返回的业务工具，也不发送 QQ 消息；六种协议与供应商的离线回归使用 MockTransport。

```sh
uv run python -m tools.cache_probe_replay --live \
  --replay-root <私有导出目录> --variant current \
  --sequence 0,1,2,0,1,2 --name <实验名称> --output <私有结果目录>
```

从标准输入的私有管道读取 `{ "profile": <ModelProfile JSON>, "api_key": <key> }`，
凭证不传命令行、不写报告。模型池按原 Profile 的 Provider、协议、header 与超时派发，
结束后关闭全部客户端。Chat 使用当前供应商 serializer；Gemini 保留导出的 prepared body，
不把重新生成的配置冒充原采样请求。

输入目录包含 `chatrequests.jsonl`、`cases.json`；Gemini 另需 `gemini.bodies.jsonl`。
输出记录逐次 HTTP 状态、usage、耗时、来源哈希与会话元数据，完整请求和响应只放私有目录。
缓存字段缺失保持未知；一次客户端派发不证明代理最终上游派发或供应商账单。
不接入 CI、定时、启动检查或默认付费测试；受控重放不能代替自然 QQ 流量验收。

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
