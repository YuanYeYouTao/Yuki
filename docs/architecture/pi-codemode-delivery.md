# Pi 与 Code Mode 开发交付记录

始于 2026-10-04，最后更新 2026-10-05；目标与不变量见 [设计合同](pi-codemode-design.md)，接手说明见 [交接](pi-codemode-handoff.md)。

当前来源定位：Pi 仅为设计参考，Yuki Python 核心按自有合同实现；Monty 是实际依赖。
此前“Pi 移植”称谓和 Pi 许可随包检查属于历史记录，当前标注及包装已修正，详见文末
“Pi 参考关系澄清”。历史实测结果未改写成更新后制品的通过证据。

## 授权与 Git

- 本地实现和隔离测试已授权；本地依赖安装及指定 Monty 编译已单独授权。
- 远端创建 `codex/pi-codemode-experiment` 并拉取已单独授权且实际完成。
- 初次分支创建基线为 `8204b28ebc8939213dae60dbab94ab1c16d1263a`；本次接手前已 fetch
  远端测试分支并确认本地与远端均为 `226b5d6ac5e39021a5a4dca8eccc9f8e7401580f`。
- 开始时工作区干净；原主工作树 `main` 未切换、未修改。
- 已授权并执行：在测试分支提交与推送。2026-10-04 后续请求另外授权使用研究者指定的
  本地 DeepSeek 凭据完成真实 Provider 验收（三协议十二个配置场景实际通过）。PR、合并、真实消息、生产访问、
  镜像发布与部署仍待明确授权，均未执行。
- 共享 Git 存储存在既有 AppleDouble `._pack-…idx` 索引报错；fetch/push/分支追踪
  实际成功。没有删除或修复无关 Git 元数据。
- 2026-10-05 用户另外要求真实长任务完成度/成本/时间对照，并明确取消本次比较的
  费用上限。只在新建合成工作区和临时数据库中调用已指定的 DeepSeek；其他未授权范围
  不因这次费用授权而改变。

## P00：基线与清单，已完成

已核对现行 development-contract、Tool Kernel、主 Runtime、Provider、child 与持久环境边界；
按精确 SHA 获取 Pi/Monty。基线迁移 head 为 `0091_ordinary_turn_admissions`；生产版本未读取。

新增隔离声明导出器和 X02 回归：实际 manifest 每行映射 descriptor/binding，缺少映射失败，
没有输出 schema 的项明确为 null。部署相关插件/MCP 与生产清单不冒充已采集。
入口映射见设计合同 E01–E14。macOS 上 SQLAlchemy async 缺 greenlet，已改用现有
SQLAlchemy 的 `asyncio` extra（`pyproject.toml`/`uv.lock`）。

## P01：显式调用身份，已完成

- `MainAgentBackend.begin_batch` 与 `_batch` 按“名称＋参数”回找身份已删除；Backend 只暴露
  `execute_call(Invocation)`。Runner 仅为仍实现 `begin_batch` 的自定义/测试 backend 保留兼容
  调用，P10 删除。
- 身份为 `chain:request_sequence:provider_call_id`（过长时为全元组 SHA256），同参不同 call
  各自执行，跨响应重用 call_0 互不冲突，同响应重复 ID 在派发前拒绝。
- 所有来源的 Social 回执 `call_id` 现为 Host operation ID，不再是响应内 Provider call ID
  （此前仅 SELF 如此）。`test_gemini_web_search_main_turn` 的断言随合同更新。
- `_mutation_identity` 的单次写授权只限 `memory_change` 与管理写，不再合并合法同参发送。
- 未知 descriptor 的缓存分类改为保守副作用。

## P02：原 effect 内持久化，主体完成

已实现并有 fixture：

- T1 `publish_code_boundary`：私有快照、父 checkpoint CAS、唯一子 intent、对象引用同一
  writer 事务；过期 revision、伪造 dispatch/budget/父/owner、binding 不符均不发布任何行；
  同父 ordinal/engine call 重复映射为 `code_child_identity_conflict`。
  （`tests/integration/test_code_boundary_publication.py`）
- 快照读取复核 Work owner ref、来源 revision、隐私 generation 与 binding header；未发布
  到本 Work 的字节不能加载。
- T2/T3：预算与 `dispatch_started` 同事务；冲突结果拒绝；取消后原 operation 仍可结算迟到
  回执，但 T1 已登记未派发的 intent 不能再接纳。
- restore 分流：未结算且 `composition.version=1` 的父调用在一般 pending 配对前返回
  `PendingComposition`，不配 unknown、不退休链；已结算父只配一次原回执；未知版本走保守路径。
  （`tests/integration/test_code_composition_restore.py`）
- 真实子进程硬杀（`os._exit`）四个窗口：T1 后、T2 后、下游已写后、T3 后；下游有独立
  append-only 日志。重启后均不重发、不重扣，T2 后一律 unknown。
  （`tests/integration/test_invocation_process_crash.py`）
- `_unresolved_clause` 只将已识别的 `code_composition` 父从未知围栏排除。

快照发布失败、引用提交中断、writer 排队后过租约已由 `tests/integration/test_p02_failure_windows.py`
补测（`c0593229`）；composition 父结算与 `PendingComposition` 续跑由 P05 接入。

## P03：Pi 内核 Python 移植，已完成

- 新增 `src/qq_ai_bot/agent_core/{__init__,loop,types,state,events,model_boundary}.py`，
  均带 Pi MIT 署名与源符号/行范围；只依赖 `qq_ai_bot.domain.messages`（有测试强制）。
- `AgentRunner._run` 的 `for request_index` 循环删除，改由 `run_agent_loop` 驱动；
  原循环体按职责拆为 `Callbacks` 的 9 个边界闭包。截断响应的非执行回执改由核心
  `fail_truncated_calls` 生成（字节不变）。Work 会话下重复批次计数只读持久 `repeats`，
  不再先算一遍内存指纹再覆盖。保留职责清单见 [来源记录](pi-port-provenance.md)。
- 差分：同一组 fake 模型序列（8 个场景，含 Work accept→执行→complete）在移植前
  `b4fdef7d` 采集黄金样本（模型请求、工具顺序、持久 effects、Work 预算、可见输出），
  移植后逐项一致。刻意差异各有 fixture。

```sh
uv run --frozen pytest -q tests/unit/test_agent_core_loop.py \
  tests/unit/test_agent_core_differences.py tests/unit/test_agent_core_differential.py
# 26 passed
uv run --frozen mypy            # 716 文件无错误
uv run --frozen pytest -q -p no:warnings tests
# 2817 passed, 28 skipped（27 项为 Monty worker 未设 YUKI_MONTY_BINARY，1 项需生产备份）
```

未完成：真实供应商 streaming（当前仅完整响应＋合成 frame）；Pi 上游测试未运行；
跨步变量仍经 `nonlocal` 共享，P05/P08 收敛；`PendingComposition` 尚未由内核驱动（P05）。

## P04：Monty 隔离驱动，已完成（本机 aarch64-apple-darwin）

**驱动方式。**使用固定源码构建的官方绑定 `pydantic-monty-client` 1.0.1，而不是自写 protobuf
帧协议：绑定经 monty-pool 启动本地 `monty subprocess`，`worker.rs:185-194` 已做
`env_clear`、piped stdio、`kill_on_drop`，并提供手动 `feed_start`、`resume`、
`FutureSnapshot.resume({call_id: …})`、`dump`/`load_snapshot` 与类型化限额错误。只用手动接口：
不用 `resume_auto`、`feed_run`、`external_lookup`、mount、`os=` 回调、WebSocket/remote 或宿主 exec。

**新增模块 `src/qq_ai_bot/codemode/`：**

- `engine_monty.py`：`PinnedWorker`（绝对路径＋sha256＋平台核验，不经 PATH/wheel 发现）；
  `MontyEngine`（每个 worker 只服务一次 checkout）；`MontyRun` 的 start/answer/settle/dump/
  restore/terminate。OS 调用一律 `resume_not_handled`（沙箱内 PermissionError），未定义名称
  NameError，非 manifest 函数与带 object_id 的调用回 NameError，`__import__/open/exec/eval/
  compile/input` 不能进 manifest。参数、输入、答复和结果都过严格 JSON 闸门。任何失败丢弃 worker。
- `limits.py`：引擎限额（feed 时间、内存、递归、`max_suspensions`）与独立宿主限额（代码/输入/
  输出/结果/快照字节、pending future、跨恢复累计挂起数、父侧 watchdog）。`max_suspensions` 只是
  引擎资源上限，不是工具预算。
- `snapshot_binding.py`：dump 只经 `ProtocolStore.put_code_snapshot/get_code_snapshot` 与
  `CodeSnapshotBinding`；边界记录保存 engine call、参数摘要与宿主累计计数，供 T1
  `publish_code_boundary` 发布。恢复时重新宣告的调用必须与保存边界一致，否则
  `snapshot_binding_conflict` 并丢弃 worker。
- `driver_types.py`：宿主侧 DTO；答复异常类型为封闭集合。
- 配置：`Settings.code_mode_worker_path/_sha256` 与限额字段（env，进程级）；未配置即不可用。
  镜像/服务接线留给 P09。

**观察到的引擎行为（真实 worker）：**`while True` 在 feed 限额处终止；输出洪泛 1s 内产生约
125MB print 回调，宿主只保留上限内文本并标记截断；`MemoryError` 限额脚本不能捕获；
20 万项加法表达式使原生编译器栈溢出（SIGABRT），只丢该 worker；`max_suspensions` 超限为
RuntimeError；旧 dump-format（版本 1 < 13）与截断 dump 被拒并丢弃 worker；同一挂起二次
resume 由引擎拒绝。

**构建：**`scripts/build_monty_worker.sh` 固定 SHA、套补丁、`cargo +1.96.0 build --release
--locked -p monty-runtime --no-default-features`（仅 worker）并用 maturin 1.9.6 构建 wheel。

| 产物 | sha256 |
| --- | --- |
| `monty`（worker-only，`--no-default-features`） | `c77fd658c0af299a687aae4949826a5ee902b7fdd5d2db1db34142b3e9dee41a` |
| `monty`（默认 features，含 CLI） | `2db64324459259fa291fe24d03518677b5f44f5eabcdb2a9435b00482d2d9430` |
| `pydantic_monty_client-1.0.1-cp312-cp312-macosx_11_0_arm64.whl` | `752fa7c616e49364b6eb2748ce67bd63e2874551239f596d5a3b17e4d789fa53` |

绑定 wheel 用 `uv pip install --no-deps` 装入 `.venv`，未写入 `pyproject.toml`/`uv.lock`：
它不在任何索引上，只能由构建脚本从固定源码产出。`uv sync` 会移除它，需重跑脚本。P09 决定
镜像内打包方式。

**测试：**

```sh
uv run --frozen pytest -q tests/unit/test_codemode_contracts.py        # 14 通过
YUKI_MONTY_BINARY=<worker> uv run --frozen pytest -q tests/integration/test_codemode_worker.py
# 两种构建产物各 27 通过；无 worker 时 27 跳过并给出原因
```

覆盖：manifest 挂起与完成、死循环、输出洪泛、内存耗尽、超大代码、长编译崩溃后宿主可继续、
名称/OS/文件/环境/网络/模块探测（9 项）、任意对象双向拒绝、挂起上限非业务预算、保留名称、
答复只能对准当前挂起、FutureSnapshot 跨“重启”（新 engine 从 ProtocolStore 恢复）手动 settle、
恢复不产生新 engine id、累计计数不清零、边界不符冲突、篡改/截断/旧格式不恢复、绕过私有存储
无效、同一快照不能在一个 run 上二次恢复或二次 settle。

全量（含真实 worker，`YUKI_MONTY_BINARY` 指向 worker-only 构建）：2818 通过、1 跳过；
mypy 710 文件无错误。

**未完成：**Linux 构建与 digest 未验证（只核对了本机）；P05 尚未把答复接到 InvocationService，
`publish_code_boundary` 在测试中由 fixture 调用；转依赖 notice 汇总待 P09。

## P05：完整 Code Mode 能力，已完成（`90b6d46a`）

单元 A `feat(codemode): 固定API与全工具受控调用`：

- 新增 `codemode/{contract,api_projection,driver}.py`；主合同 version 11 加入固定
  `execute_code`，`MainAgentContract.script_api` 从冻结声明投影（原 schema、可逆名称）。
- 外层调用为 `code_composition` 父 effect；子调用为显式 child Invocation
  （`<父>/c<序号>`），T1 `publish_code_boundary` 后经同一 InvocationService → WorkSession
  T2/T3 → `MainAgentBackend.execute_call`；VM 只收原回执视图。InvocationService 去掉
  `composition_invocation_not_supported`，WorkSession/领域回执键改为原 operation
  （`receipt_key`），子调用不能借 `allow_pending` 绕过新输入围栏。
- 计量：外层不计业务；子调用 T2 计一次；T2 前拒绝/预算拒绝为零；复用回执另列。
- 并发：只读段受 `max_parallel_calls`，发送/修改/记忆/控制为屏障；同响应多个外层顺序执行，
  不能与直接调用同批。程序结果超限时存授权 artifact，返回预览与 `complete=false`。
- `publish_code_boundary` 允许无子意图的程序 checkpoint；新增 `composition_children`、
  `undispatched_intent`；未 T2 的版本化 intent 不再计入未知效果围栏（旧行保守）。

单元 B `feat(codemode): 控制门及原Work让出`：

- 生命周期控制在同伴结算后独占执行，复用直接调用的同一检查（抽出
  `AgentRunner._execute_control_call`），`admit_dispatch(charge=False)` 不扣业务额度。
  wait/need_input/complete/fail 等、`memory_change`、未知副作用、新输入、权限或接纳关闭由
  宿主停止脚本并配对外层结果；get/list 保持原分页且不停止脚本。
- 记忆写与其他副作用不同步；先前已有副作用时记忆写被拒；同步的发送不派发。
- 段额度用尽：外层 call 不配对，下一段 restore 的 `PendingComposition` 在任何模型请求前
  续跑同一程序；已配对 partial 永不恢复 VM。

更新的既有测试：`tests/integration/test_code_composition_restore.py`
（`PendingComposition` 现携带原外层 call 的 name/arguments）。清单 JSON 重新导出为 76 项。

未覆盖／待后续：worker 合同的子集投影与插件主调用入口（P07）；发送分片/附件的
领域闭合（P06）；真实 Provider wire 下的 execute_code 前缀与缓存对照（P08）；
`execute_code` 在模型侧的使用质量未评估（需付费模型，未授权）。

## P06：发送、记忆和来源权限闭合，离线验证完成

- Social prepare 与原 Work effect 的 `invocation.original_domain_ref` 在同一交易提交，
  保存原父操作；分片、文件附言不替换该关联。Work 取消/generation/lease 在实际 Social
  claim 处再次检查，晚到的已完成事实仍只结算原回执。
- 保留计划数量与逐片向量；同参新调用各有原 ID，原调用重入不发送；长 operation ID
  仅在 Social 存储键表示中完整 SHA256，Work 保留原身份。恢复查询严格按原关联与
  子片身份读回执，不按正文或“最近消息”匹配，不清除原 Work unknown 围栏。
- Memory 的 Agent 写入在原来源持久回执与实际提交交易中核对已消费授权，并将
  `memory:<mutation_id>` 与 Work effect 原子关联；新激活/新服务不重发一次写权。
- MCP 在连接等待后核对原配置与 metadata，已派发写入的超时/断连/服务器失败为 unknown，
  不以 reconnect 重放。读取与确定的 HTTP 拒绝仍保留各自原错误语义。
- Plugin 在 scope 等待后重核当前安装状态与原注册项；真实 manifest revision 撤销
  批准时不能进入旧 handler。通用 binding 抛异常也按是否已派发及 effect 分类保留未知。

新增证据：`test_codemode_social_receipts.py`（真实 Monty＋独立外部发送日志，8 项）、
`test_codemode_memory_authority.py`（真实领域写入＋恢复）、`test_codemode_mcp_authority.py`
（9 项）与 `test_codemode_plugin_revocation.py`（3 项）。定向 Social/Memory/MCP/插件首轮
14 项通过；补上 HTTP 408/500、连接等待撤权和真实安装批准变更后，MCP/插件 12 项通过。
记忆领域与原 effect 审计集合 64 项通过；主入口/MCP 集合最后一轮 52 通过、1 个新增夹具
配置错误（已将模型设为 `extra='forbid'`，随后新增集合 12 项全通过）。

既有测试调整：`test_social_source_keys.py` 的长 call ID 从拒绝改为完整身份存储表示、
重入与参数冲突断言（合约变化有注释）；`test_codemode_authority_parity.py` 纳入
`update_short_state`，移除 send_message 对照跳过；`test_automation_timeout_certainty.py`
用真实 asyncio Timeout 在派发边界触发，消除 0.2 秒期限在 SQLite 准备阶段被机器负载
耗尽的问题，原未知/不重发断言保留。没有删除或放宽验收断言。

环境：重跑 `scripts/build_monty_worker.sh` 成功，本机 worker SHA256
`af76448a14fe980c823f1c6092692f90a8ddc6341f236f17d6d47d72f49548e8`；
wheel SHA256 `e3c99d8890b265babb90a50be336e4e3b3f25619c26ff607265578580c547c75`。
`frontend/` 中 `npm ci && npm run build` 通过；Node 22.17.1 有依赖 engine 提示，未升级运行时。

阶段结束命令：ruff check、ruff format check 通过；mypy 719 文件通过。第一次全量为更新
既有长 call ID 合约测试而中止，不计为通过；最新全量使用真实 worker：
**3127 通过、1 跳过，679.37 秒**。唯一跳过为未提供私有生产备份路径的 replay；
不计为迁移/生产备份验收通过。worker 测试无跳过。
阶段已提交并推送：`5414065f670230008aa796289a3ec49b1fd1d37b`，远端精确 SHA 已核验。

## P07：全部入口接线，离线验证完成

- `InvocationContextFactory` 只复制入口已有的可信 `ToolRuntime`，收敛 Chat、SDK 主调用、
  独立插件计算和 child 的重复装配；保留原 actor、内部 event、conversation 和 execution。
- 持久 worker 冻结自己的原工具子集与 API revision，增加 `execute_code`；仍不能发送、
  写记忆/短状态、管理或再派生 child，业务计量沿原 root。
- 插件自主唤醒将原 job/event/plugin 身份和 intent 交原 Work，恢复仍由原后台 job owner
  驱动。claim 和实际 Social 派发重核当前安装及目标批准；撤销不进模型、不发送。
- durable SDK 返回前读原 Work 终态，修正激活中取消仍返回 running 的过时状态；取消/失败
  的后台 job 不再回到 pending 重试。
- 独立插件计算保留独立会话、无业务 backend、`tools=None/max_tool_calls=0`；CLI 保持原
  管理/诊断命令，未知 Agent 命令在解析处拒绝，诊断不读取模型配置或调用 Provider。

新增实际入口测试：普通聊天 5 项（含短聊、未接纳拒绝）、SELF 4 项、Person/SELF 自动化
8 项、SDK 主调用 4 项、自主唤醒 4 项、独立插件计算 4 项、持久 worker 4 项；分别核对
正常、拒绝、取消和原身份跨段恢复，以及独立下游日志、预算和原来源。CLI 合同 5 项。
定向测试均通过；原 worker/SELF 集合 50 项、后台/插件原合同集合 25 项通过。

既有夹具调整：SELF worker 声明由仅含 name 的模拟对象改为真实 `ChatTool`（投影需要
完整 schema）；五项 actorless 插件发言/媒体测试增加真实当前安装及目标批准；直接工具
trace 夹具明确 `script_api=None`。原行为断言保留，修改处写明现行授权合同。静态提醒
新增正常及重入都没有 Provider 请求的断言。没有删除测试或放宽预期。

阶段静态检查：ruff check、format check（1113 文件）和 mypy（720 源文件）通过。
首轮真实 worker 全量为 3162 通过、3 个旧夹具失败、1 跳过（695.63 秒）；不计为通过。
修正上述夹具后，失败项及静态提醒 5 项通过；第二轮全量 **3165 通过、1 跳过，713.81 秒**。
唯一跳过仍为未提供私有生产备份；真实 worker 无跳过。ruff check/format、mypy 720 文件
均通过，最终 diff check 通过。阶段已提交并推送 `b49e4da6b2e8c13bda5dfb0576e4edfdf4bad666`，
远端精确 SHA 已核验；没有执行 PR、合并或生产动作。
下一依赖：P08 外层 Code Mode wire、上下文与唯读父子轨迹；随后 P09。

## P08：Provider wire、上下文与只读轨迹，离线验证完成

- 新增 19 项 HTTP 实际请求对照：Chat 15 个 vendor、两种 Responses、Anthropic 与 Gemini，
  使用真实 Runner、Monty 和隔离 Work。外层 execute_code 只配一个工具结果，三个业务子调用
  不伪造成 Provider function_call；SQLite 私有 checkpoint 经新 Journal 读取、解码后，
  生成的请求与原请求逐字节一致，opaque 签名保持，效果和预算不重复。
- 另有 20 项 Chat HTTP 参数测试，逐 vendor 核对端点、header、固定完整工具/schema、
  thinking/token 字段、冷/热缓存用量和受支持 override；超出 Groq 声明的档位在请求前拒绝。
- Code Mode 父子轨迹只记录原 operation 身份、工具、序号、状态和私有结果引用/摘要哈希，
  不镜像程序或领域正文。三项真实 worker 测试覆盖正常、unknown 和诊断写入故障，证明
  trace 丢失不改变业务效果/预算；沿现有 control 读取授权与分页，查询不触发执行。
- WebUI 在原轨迹页面显示父子身份与状态，保留原隐藏正文行为；DOM 测试核对只读和缺样。
  现有两项图片预览夹具改用同正文与 MIME 的 Node Response，避开 jsdom Blob 缺失 stream()，
  原预览/延迟加载断言未变，修改附注释。

定向 CodeMode/轨迹/wire 49 项通过；原 Provider、搜索桥、协议续接、compaction/artifact/GC
集合 270 项通过。前端全套 89 项通过，npm build 通过；npm lint 退出 0，现有 7 个 warning
未改动，三个改动文件的 Prettier check 通过。没有进行上游 streaming 或真实 Provider 调用。

阶段命令：ruff check、format check（1116 文件）通过；mypy 720 源文件通过；真实 worker
全量 pytest **3207 通过、1 跳过，707.09 秒**。唯一跳过是私有生产备份路径未提供；worker
无跳过。阶段已提交并推送 `bab3448a414f07e758b00b437e09bae7c49b39b9`，远端精确 SHA 已核验。下一依赖：P09 正常迁移链、隔离池、备份/回退和许可汇总。

## P09：迁移、资源池、完整备份和 Linux 隔离验证

P09 授权范围内的离线实现、阶段全量和镜像装配验证已通过；提交/推送状态见本阶段回报。许可文本缺口及生产边界仍单独保留。

- 精确归档的 755/0081 和 820/0091 历史 producer 用原 API 生成合法数据，再执行全部
  正常 Alembic 迁移至 0092。原 Work/journal、有限 7/9 根预算、Social unknown、Manager
  原 run/continuation、插件批准及 automation run/cursor 保持。新 reader 接纳真实 Monty
  子效果后，实际旧 CLI 拒绝未知 0092，downgrade guard 拒绝丢失新 invocation 事实。
- runtime loop 共用 worker admission（总数 2、背景上限 1）。实际背景 worker 占用一个
  名额时，第二个背景等待，前台 native PID 仍可启动；排队取消/超时无新 PID。并发
  terminate 和二次取消等待同一次清理，容量只释放一次。这是准入验证，不是生产容量测量。
- T0 保存原 VM policy；四种上限下调或缺失、binary/API/dump 合同变化、实际 worker
  篡改均结算原 partial，保留 child、计数和快照，不加载旧堆或重跑业务。Host JSON 在
  超限前拒绝整树复制，UTF-8 字节边界与 tracemalloc 通过；stdout 跨 feed 按 UTF-8 累计。
- SQLite backup API 加原私有对象、artifact、workspace、Manager DB/回执及配置形成
  一致副本。新 reader 读取原 owned refs；活跃对象不被 GC，隐私删除中断后复制旧字节
  不能恢复读取权。实际 PersistentManager 仓库恢复原 run，对账产生唯一 completion，
  新 Bot repository 重复消费仍是同一结果，无重执行。execd 和容器句柄是明确合成下游，
  不声称真实 gVisor 或生产环境通过。缺失、篡改及 deleting 对象精确拒绝。
- 共用 builder 固定 SHA/补丁、Rust 1.96.0、maturin 1.9.6 和 CPython 3.12；最后一次
  uv sync 后重跑 worker wrapper 并安装本地 wheel。编译与 notices 分成两个可缓存步骤；
  许可下载仅对同一固定公开 URL 有界重试，断连失败与 404 缺失严格区分。
- 专用 Debian 12/aarch64 VM 没有用户目录挂载，没有模型凭据、生产数据或 Bot。
  实际非 root Host 启动 root-owned launcher/native；观察六个独立 namespace、UID/GID
  65534 映射、只读 worker/运行库、零能力、仅 PWD=/tmp 环境、仅 lo、无应用/工作区/socket
  以及 AS/CPU/FSIZE/NOFILE/CORE 限制。真实无限循环、所有后代退出、绑定 owner SIGKILL、
  前台竞争和 SIGSTOP→Host 0.5 秒 watchdog 均通过，证据见
  `pi-codemode-evidence/p09-linux-isolation.json`。两个挂起脚本 HWM 为 4040/4088 KiB，
  watchdog 0.5024 秒；仅代表这个合成 fixture。
- 默认 Docker seccomp 实测拒绝 user namespace（EPERM）。默认镜像内 Code Mode 保持
  未配置，原生 Linux Host 是本阶段验证过的隔离方式。未用 privileged/unconfined/
  额外 capabilities 绕过限制，容器部署须有自己的获准隔离配置与证据。
- 原 Pi/Monty/typeshed 许可独立保留；Yuki wheel 内 Pi LICENSE 逐字节一致。598 个锁定
  Cargo 包逐归档核验，Darwin target 352、Linux target 353，共用 321 份全文。
  Linux target/实际 artifact 见 `pi-codemode-evidence/p09-linux-distribution.json`。
  8 个文本缺口保留，其中 quote-use/quote-use-macros 是建置依赖；完整精确 upstream tree
  查验也未找到这两项及 r-efi 的许可文件，其余四项没有 packaged VCS revision。
  未合成版权，X11 仍为 partial，镜像发布未授权且未执行。
- 运维文档给出 reader/producer 矩阵、停止接纳和原操作对账、一致备份、正常迁移、
  单实例切换及失败处置。没有生产访问、Bot 启动、真实消息、PR、合并或部署。

实际命令与结果：

```sh
bash scripts/build_monty_distribution.sh <owned-output> <venv-python>
# Darwin 和 Linux 均成功；Linux wrapper 在 uv sync 后重建并安装成功
uv build --python .venv/bin/python --wheel --out-dir <owned-output>
# 成功，原 Pi LICENSE 完全一致
bash -n scripts/build_monty_distribution.sh scripts/build_monty_worker.sh
# 通过；精确自有补丁复用及后续修改/暂存/未跟踪保护另用隔离 Git fixture 验证
uv run --frozen ruff check src tests scripts migrations
uv run --frozen ruff format --check src tests scripts migrations
uv run --frozen mypy
# 通过，format 1126 文件，mypy 721 源文件
YUKI_MONTY_BINARY=<actual-worker> uv run --frozen pytest -q -p no:warnings tests
# 最终全量 3246 通过 / 1 跳过，896.09 秒；真实 worker 无跳过
YUKI_MONTY_BINARY=/opt/yuki-monty/monty YUKI_MONTY_LAUNCHER=/opt/yuki-monty/monty-isolated \
  .venv/bin/python -m pytest -q -p no:warnings \
  tests/integration/test_codemode_worker.py tests/integration/test_codemode_resource_policy.py \
  tests/integration/test_codemode_manager_backup.py tests/unit/test_monty_notice_download.py
# Linux：45 通过，无跳过，26.99 秒；Mac 相应 worker/resource/Manager 41 通过，14.40 秒
.venv/bin/python scripts/verify_monty_isolation.py --output <owned-report>
# Linux：passed，所有原后代 PID 均退出
sudo docker build -f deploy/codemode/Dockerfile.validation -t yuki-monty-validation:p09 .
# 成功；应用 Dockerfile 同样构建成功。两个镜像的离线包装探针通过，无 Bot/模型调用
```

全量唯一跳过是未提供私有生产备份路径，真实 worker 无跳过。现有 worker 测试保留行为
断言，改用明确的测试 worker helper，在 Linux 必须同时提供 launcher；没有自动发现或
不受限 fallback。新增 Manager 完整备份夹具最初缺少 home/workspace，补齐合法初始目录
后通过。早期 migration fixture 的历史 API/配置、表名及数据形状错误均已修正并重验。

Linux 编译的两次 OOM 已记录：初次 jobs=2，以及错误地并行原生与镜像 LTO。之后加入
VM 内 3 GiB 临时 swap，固定 jobs=1 并串行重型构建，原生重建完成。验证镜像首次编译
通过后 notices 下载遇到 TLS 断连，构建失败；已加有界重试并拆开缓存层，最终构建通过，应用构建复用了原生编译层。
验证镜像探针发现缺少 typing_extensions，现按 uv.lock 固定 4.16.0 及 wheel hash 安装；
修复后导入和探针通过。探针初版依赖旧 bwrap 错误字符串，实际新版本使用
“No permissions to create a new namespace”；改为同时查实际 unshare errno=EPERM 与
该拒绝诊断，未改变隔离策略或拒绝断言。
这些失败没有被当作通过，未调低测试或隔离要求。

实际 native SHA256：Darwin `af76448a14fe980c823f1c6092692f90a8ddc6341f236f17d6d47d72f49548e8`；
Linux `fc4ee999669c5f520f6d37333b13e341fc4a00493785e0a6e6b6133dfe435241`；
Linux launcher `bc3dd9cd7d1d0f0c11fe294c5e586ca30a28f4281271ee9cda3db800abd07c63`。
Linux 最终本地安装 wheel `c7351f8da95cf3c317200863cef8aa5c2bb2102a452c0f20727e3279fd15bba3`；
Darwin 已安装/测试 wheel 为审计内 `5f77cfbcf15ca0e0bb405d6d586aeb98aacbbcfc10555ba0b882c1b73868aa6f`。
重复构建的 wheel ZIP 时间会改变 hash，payload 已核对一致，不声称归档字节可复现。

P09 记录时 VM `yuki-p09-20261005` 及临时构建/下载缓存尚未删除；P11 收工清理已全部
删除，当前状态见 `cleanup.json`。应用镜像以 UID 10001、只读 root、cap-drop=ALL、no-new-privileges、network=none 和
私有 tmpfs 运行包装探针：真实绑定导入、固定文件/hash、598/353/321/8 notices、四份
原始许可证、wheel 内 Pi 许可、构建后的 WebUI 和全正常迁移链至 0092 均通过。
具体命令用 `scripts/verify_monty_packaging.py application|validation`；证据在
`pi-codemode-evidence/p09-container-packaging.json`。两个镜像默认 namespace 创建仍被
拒绝，未启用容器 Code Mode，没有启动 Bot。

下一依赖：P10 显式回合状态、兼容删除、
四组计量与全矩阵；真实 DeepSeek 已授权，按 P10 前置条件尚未运行。生产验证待授权。

## P10：完整实现与离线验证通过，已提交并推送

P09 已提交并推送 `85a35aa92ace8b4b54dcfb95ad35b5014e2b5cb3`，远端精确 SHA 已核验。
生产 `AgentRunner._run`、Callbacks bag/union、`begin_batch` 调用、工具
`execute(name,args,runtime)` 临时 adapter、Runner/Turn/Coordinator 动态 hook/model fallback、
Worker `__getattr__` 已删除。原测试 Provider 在 Runner 外显式规范化。默认 backend 生命周期
权限拒绝，Main/Worker 显式核验；测试夹具显式声明假权限，coordinator 只接受原 Invocation。

每次激活的 38 项状态归 `TurnState`；`TurnExecution` 实现模型、调用、结算三个固定边界，
调用唯一 `agent_core.loop.run_agent_loop`。请求拆为准备、持久派发、响应观察；主候选、普通
摘要、Work 摘要各有 admission 对象。预算/CAS 只在派发提交，HTTP 重试共享原 reservation，
辅助页不覆盖主 journal。摘要分页和工具批次不是第二主循环。
结构证据：`pi-codemode-evidence/p10-retirement.json`。

验证：最初状态拆分 39 通过/10.19 秒；Callbacks/begin_batch 删除后 61 通过/24.14 秒；准备/
观察拆分 126 通过/47.89 秒；typed fixture 重验 116 通过/46.12 秒；wire/诊断重验 100 通过/
5.99 秒；异常所有权 fixture 5 通过/1.65 秒。原生 Linux root-owned launcher 的核心、差异、
composition/control/resource/process-crash **61 通过，22.97 秒，零跳过**。ruff/mypy 通过，
722 个源文件。首轮全量 3228 通过、18 失败、1 跳过/755.46 秒：17 项是旧 fixture 接口/补丁
目标未迁移，1 项是迁移时误改诊断字段（已恢复，无最终 diff）。原断言保留，失败项定向
重验通过，最终完整复验 **3246 通过、1 跳过，737.40 秒**；唯一跳过仍是未提供私有生产备份。真实 Monty 没有跳过。

四组固定任务比较 **12/12 通过，5.70 秒**：旧循环按 b4fdef7d 固定源码临时加载，主迭代
不改；共享 Invocation/Code kernel，分离循环差异与组合效应。历史循环不进入生产，也不是
永久 CI 依赖。fanout：direct 4 次 HTTP、code 3 次；incomplete：direct 5 次、code 4 次；
拒绝任务各 4 次、零业务派发，原 Work 提议 failed，未宣称任务成功。每个成功任务真实
业务计数均 3，paired checkpoint/counter 可读，无越权或重复。原始次数、首个有用产物、
总时间在 `p10-comparison.json`；fake planning 无 token/cache/billing，记 null/unknown。
样本未显示新循环自身节省请求；本地 code 时间更长，不能宣称线上加速。建议保留直接工具，
不把 Code Mode 设为默认，不推进生产切换。

fixture 合同更新：`StubAgentBackend` 是固定测试接口；原 execute 辅助迁为
`execute_call(Invocation)`，读取原 context.runtime；计数/结果/拒绝断言不变。compaction 容量
补丁覆盖新模块；异常所有权在 `TurnExecution.activate` 注入同一 ExceptionGroup，原来源/
恢复断言不变；Core Callbacks 移至 tests/support。完整 JUnit 保存为 `p10-full-results.xml.gz`，
验收矩阵附有实际通过节点及 JUnit hash：89 项离线通过，X11 许可项 partial。
`p10-tool-coverage.json` 给出 76 个固定声明的 schema、binding、wrapper/拒绝路径映射；
Linux 61 项证据在 `p10-linux.json`。P00 清单保留历史 not_run，不改历史快照。
最终 ruff check/format 通过（1133 文件），mypy 722 源文件通过；git diff --check 通过。

P10 提交并推送 `9edc5f1e624fae3e3abb63d66bf3db53065d9686`，远端精确 SHA 已核验。
真实 DeepSeek 与清理状态见下节。

## P11：已授权 Provider 场景实际通过，生产与真实发送待授权

只使用研究者指定的 DeepSeek 凭据；没有复制或输出 key。配置的
Chat Completions、Responses、Anthropic Messages 三种协议均实际收到 HTTP 200，
使用完整固定 76 声明、真实 Runner/SQLite/Monty；业务仅连接隔离 fake workspace。
这是一次调用完整 response 的验收，不是流式 token delta 或生产验证。

首轮 12 项：pytest **5 通过、7 失败，27.63 秒**，实际 24 次 HTTP，触及声明的请求上限。
分别为 Responses code、三协议截断、Chat 受控断连通过；三协议 direct 与
Chat/Anthropic code 失败；Responses/Anthropic disconnect 在付费派发前被请求上限
挡住，没有实际运行。截断/断连均无业务派发。思考 content、Responses opaque 及
Anthropic thinking/signature 在有后续请求时逐字段原样回传（保存 hash/布尔，不保存正文）。
这证明观测到的材料没有改变，不宣称本地验证供应商签名或永久缓存保证。

首轮暴露验收夹具错误：写入后读取 result.txt 仍返回 numbers.json 的数据，且提示要求
“核验写入”却又只接受两次业务调用，模型因此反复读取。已修正为保存隔离文件实际状态、
返回真实写入参数，并要求根据写入回执核验后返回；没有改变生产 Runner、固定声明或断言。
未删除首轮失败，证据为 `p11-deepseek-initial.json`。修正后独立 MockTransport 装配
**12 通过，4.27 秒**；新永久夹具只用 synthetic key，普通 pytest 不调用真实 Provider。

实际命令：

```sh
PYTHONPATH=. YUKI_MONTY_BINARY=<actual-Darwin-worker> uv run --frozen python \
  scripts/verify_deepseek_codemode.py --credentials <user-credential-file> \
  --output docs/architecture/pi-codemode-evidence/p11-deepseek-initial.json --authorize-paid
YUKI_MONTY_BINARY=<actual-Darwin-worker> uv run --frozen pytest -q -p no:warnings \
  tests/unit/test_deepseek_acceptance_harness.py
```

首轮 conservative reservation 为 $0.5190585（按请求 byte 上界及最大输出预留，非账单）。
实际返回 usage 合计 input 382827、cache hit 345856、output 1961；按官方 Flash peak 费率
估算 $0.015519636，off-peak 约一半，实际账单未核验。受控断连的应用 usage 为 unknown，
观察器已读到 wire usage；两者分开记录。一次预算重建辅助命令误用系统 Python 3.9，导入 datetime.UTC 失败、无网络调用；
改用 uv 管理的 CPython 3.12 后实际预算重建及篡改拒绝均通过。金额依据
[DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/)，没有用离线次数声称真实费用。

重验仅运行失败/未运行的七项，继承首轮已用 HTTP 和费用 reservation，
累计费用 ceiling $1 不变，追加最多 24 次请求（累计最多 48）；从原 wire 元数据重建
已用 counter/reservation，零化或不一致数据拒绝，nested resume 拒绝，不能把已通过项夹带重跑。此前将自设的 24 次请求上限误当成新的授权需求，发出了重复确认；
随后依据用户已有的真实 API 验收授权撤回该确认，原 $1 费用上限没有增加。
**重验 7/7 通过，18.72 秒**，三协议 direct/code/truncation/disconnect 共 **12/12** 场景
均有实际通过记录，首轮失败/未运行证据不删除。直接工具各 3 次 HTTP、code 各 2 次；
成功场景各执行两次真实 Host→fake 下游调用，原预算准确保留，零重复 operation。
新重验逐请求比较完整 tools payload（schema、顺序、说明），全部一致；思考/opaque
材料在实际后续请求逐字段原样回传。
ruff/format（1134 文件）和 mypy（722）通过；
最后全量 **3258 通过、1 跳过，731.20 秒**，原生 worker 无跳过。脚本随后收紧假文件路径
及完整工具 payload 对照，使用实际安装 worker 的最终离线 12 项重验通过（3.95 秒）；
生产 Runner 在 P10 后未改变。原始全量 JUnit 与最后定向证据在
`p11-full-results.xml.gz`、`p11-local-validation.json`，首轮真实失败 log 在
`p11-real-initial.log.gz`，精确 paid/failed/not-run/费用界限汇总在 `p11-deepseek-summary.json`。
实际重验命令（其余已通过场景没有重跑）：

```sh
PYTHONPATH=. YUKI_MONTY_BINARY=.venv/bin/yuki-monty-worker uv run --frozen python \
  scripts/verify_deepseek_codemode.py --credentials <user-credential-file> \
  --resume-report docs/architecture/pi-codemode-evidence/p11-deepseek-initial.json \
  --maximum-total-physical-calls 48 --authorize-paid \
  --case chat_completions/direct --case responses/direct --case anthropic_messages/direct \
  --case chat_completions/code --case anthropic_messages/code \
  --case responses/disconnect --case anthropic_messages/disconnect \
  --output docs/architecture/pi-codemode-evidence/p11-deepseek-retest.json
```

累计两轮 **39 次 HTTP**，保守 reservation **$0.8518629**；实际 wire usage 合计
input 621782、cache hit 549632、output 3169，按两档公开费率估算约 **$0.01437–$0.02875**，
实际账单未核验。三项受控断连的应用 usage 均 unknown，观察器各收到 wire usage。
重验原始证据 `p11-deepseek-retest.json`、`p11-real-retest.log.gz`；当前总裁决见
`p11-deepseek-summary.json`。重验只创建一个临时输出 log，保存证据后已删除。
真实消息、生产访问、发布、部署仍待授权；X11 的八份许可全文缺口仍为 partial。

最终 `uv build --python .venv/bin/python --wheel --out-dir <owned-output>` 成功；
从实际 wheel 解包并隔离导入新 `TurnExecution/TurnState`（38 字段），确认无旧 `_run`/
Callbacks，源文件和 Pi LICENSE 完全一致，WebUI assets 存在；
证据 `p10-final-wheel.json`。这次是最终 Darwin wheel 包装检查，P09 两镜像构建状态独立保留，
未声称 P10 后重新构建 Linux 镜像。

Linux 证据已保存，VM `yuki-p09-20261005`、其镜像下载缓存、Monty/Pi 临时源码构建目录、
本任务临时日志目录，以及本次新安装的 Lima 已删除，自有目录 du 分配量合计 20.8 GiB；
最后观察可用空间约 52.5 GiB。
实际 Mac worker 安装为 `.venv/bin/yuki-monty-worker`，22.9 MB，SHA 与验证过的原生文件
完全一致，属于保留的本地开发依赖。项目、.venv、设计包、其他缓存与全局工具保留。
清理结果及空间观察见 `pi-codemode-evidence/cleanup.json`。

## 验证记录（2026-10-04）

```sh
uv run --frozen ruff check src tests scripts migrations   # 通过
uv run --frozen ruff format --check src tests scripts migrations   # 通过
uv run --frozen mypy   # 705 文件无错误（修复 _unresolved_clause 的 bool/ColumnElement 混用后）
uv run --frozen pytest -q tests   # P01 后：2758 通过、1 跳过（需生产备份路径）
# P02 后：2777 通过、1 跳过；mypy 705 文件无错误
# P03+P04 合并后（YUKI_MONTY_BINARY=worker-only 构建）：2844 通过、1 跳过；mypy 716 文件无错误
# P05+P02 补测后：3102 通过、2 跳过、1 失败；mypy 719 文件无错误。失败为既有计时测试
#   test_automation_timeout_certainty::test_transport_deadline_preserves_uncertain_receipt_and_original_dispatch
#   （剩余 0.2s 截止；该轮因机器负载耗时 25 分钟，截止在发送意图前触发）。单独重跑 5/5 通过，
#   P05 未改 automation 执行器；记为计时敏感，未修改测试。
# P05 后（同上）：3100 通过、2 跳过（生产备份路径；send_message 在已提交修改后仍可调用的对照项）；mypy 719 文件无错误
```

第一轮全量：2727 通过、7 失败、24 错误。失败为本次删除旧 `execute/begin_batch` 后未迁移的
4 个测试辅助、Social call_id 合同变化 1 项、迁移 head 推进后 3.9.0 基线文档 2 项；错误为
WebUI 前端资源未构建（`npm ci && npm run build`，产物已 gitignore）。均已修复并定向重验。

## 后续依赖

P06–P10 的完整实现、旧循环退休、迁移与离线验收已完成并推送。真实 DeepSeek 三协议
十二场景有实际通过记录，首轮失败与修正后七项重验保留；真实业务发送、生产访问、
镜像发布与部署仍待单独授权。Monty notice 补充和当前制品复验见文末。

Monty Python binding 是本地构建 wheel，未进 `uv.lock`；`uv sync` 后需重跑
`scripts/build_monty_worker.sh`。原生 Linux 隔离有独立实测证据；默认容器 Code Mode
仍不启用，不能拿装配探针冒充容器 sandbox 通过。

## Pi 参考关系澄清（2026-10-05）

按用户明确的“只参考思路”要求，将 Pi 从源码移植 / 第三方制品标注改为设计参考。
核对固定参考源码后，当前 Python 核心使用 Yuki 自有 ChatResponse/ToolCall、三个固定
执行边界、不可变事件状态及有界同步队列；没有安装或加载 Pi SDK、TypeScript 源码或其
依赖链。来源页保留参考行为及差异，第三方 notices 仅记录实际使用的组件。

- 去除 `agent_core/` 和测试边界夹具的 Pi 移植署名与误导性说明；实际模型循环、协议、
  授权和回执逻辑未修改。七个模块去除 docstring 后的可执行 AST hash 与改前逐项相同。
- 删除未使用的 `vendor/pi/LICENSE`，取消 wheel force-include 及两份 Dockerfile 的
  Pi 许可 COPY；Yuki、Monty、typeshed 许可及 Monty 完整审计保持原记录。
- 包装核验按当前合同更新：原“Pi 许可证必须存在 / hash 一致”改为验证实际三份许可
  存在、Pi 许可不随包，并保留原 worker、binding、隔离拒绝、迁移和 WebUI 检查。
  这是参考关系澄清带来的包装合同变更；未删除或放宽业务行为测试。
- 更新现行设计、交接、运维和来源记录。P09/P10 历史 wheel / 镜像报告保留原实测内容；
  不用它们证明本轮 Dockerfile 的新版本已经构建。

本轮实际验证：

```sh
uv run --frozen ruff check src tests scripts migrations          # 通过
uv run --frozen ruff format --check src tests scripts migrations # 1134 文件通过
uv run --frozen mypy                                            # 722 文件通过
YUKI_MONTY_BINARY=.venv/bin/yuki-monty-worker uv run --frozen pytest -q -p no:warnings \
  tests/unit/test_agent_core_loop.py tests/unit/test_agent_core_differences.py \
  tests/unit/test_agent_core_differential.py                     # 26 通过，0 跳过，2.64 秒
uv build --offline --python .venv/bin/python --wheel --out-dir <临时目录> # 构建及归档核验通过
```

新 wheel 不含 Pi 许可文件，核心源码逐字节与当前文件一致，WebUI assets 存在，依赖
metadata 不含 Pi。wheel SHA、AST 对照和命令结果见
`pi-codemode-evidence/pi-design-reference.json`。构建核验首次误用不支持的 `uv build --frozen`，
第二次默认解释器选择了不可用的 Python 3.7.17；改用现有 3.12 后成功，失败记录未省略。
临时输出随核验删除，Monty binding 和 native worker 保留，未重建 VM，未调用付费 API。

本轮全量测试：未运行（本轮只改说明、来源标注和包装元数据，核心可执行逻辑未变）；
更新后 Linux 镜像构建与容器探针：未运行。原全量 3258 通过 / 1 跳过为此前实测结果。
Monty 八份依赖许可文本缺口仍为 partial，本次不将它改成通过。下一依赖：若要交付更新后
镜像，需按当前 Dockerfile 重建并运行原包装探针；发布、部署仍待授权。

## Monty notice 补充与最终制品复验（2026-10-05）

本轮闭合 X11 的当前文本清单和更新后镜像证据。Pi 仅为设计参考，实际许可保留
Yuki、Monty 与 typeshed；未修改模型循环、业务权限、回执、恢复或预算执行逻辑。

- 对 580 份 registry `.crate` 逐 Cargo checksum 核验，另 18 个为本地 workspace 包。
  之前“598 份归档”的说法应按此纠正；598 是锁定包总数。
- r-efi 两包的 AUTHORS 实际含完整 MIT 与原版权。四个缺 packaged VCS 的包通过
  固定发布提交与归档源码 Git blob 对应，取得原 license；生成文件及 winapi 发布时
  的唯一版本编辑分别记录。原 Darwin worker / wheel SHA 未变。
- quote-use 两包原始 manifest 明确声明 MIT。附固定 SPDX 标准全文并保留两项
  `upstream_notice_omissions`；模板 year/holder 不是包版权署名，未声称找到原许可文件。
  文本清单缺口为 0，Darwin 去重文本 326 份。来源证据是
  `pi-codemode-evidence/monty-notice-supplement.json`。
- 新增归档/署名/声明/固定源码/损坏标准全文的回归覆盖；notice 测试 20 项通过。
  镜像探针保留 worker/hash、UID、默认 namespace 拒绝、迁移及 WebUI 断言，并增加
  580 份归档计数、全 notice 引用/hash、零文本缺口及两项上游事实核验。
- 验收导出器按当前审计和实际镜像源码/probe SHA 判定 X11；不再固定写旧八项缺口，
  缺少或陈旧镜像报告不能判为完整通过。

实际本地验证：

```sh
uv run --frozen ruff check src tests scripts migrations          # 通过
uv run --frozen ruff format --check src tests scripts migrations # 1134 文件通过
uv run --frozen mypy                                            # 722 源文件通过
uv run --frozen pytest -q -p no:warnings tests/unit/test_monty_notice_download.py # 20 通过
YUKI_MONTY_BINARY=<保留的 Mac worker 绝对路径> uv run --frozen pytest -q -p no:warnings --junitxml=<临时目录>/final-results.xml tests
# 3274 通过 / 1 跳过，689.19 秒
```

唯一跳过为未提供私有生产备份；真实 worker 测试无跳过。全量 XML/日志及逐项复核在
`final-full-results.xml.gz`、`final-full-tests.log.gz`、`final-local-validation.json`。
原 89 项测试场景再次实际通过，其中一个参数在采集时使用 `uuid4()`，新旧 UUID
节点分别记录，未把随机参数误当漏测，也未修改该测试。

当前 Docker 构建/探针：两镜像构建与严格探针全部通过。初次 4 GiB VM 无 swap，内核
OOM 杀死 Rust 编译；初次 exit 101 与内核日志保留。加 4 GiB task swap 后按同一
Dockerfile/命令重试成功，不改编译政策或检查。应用镜像复用了同一已完成的 Monty 层。

- 应用镜像 `c3b31ab58a4eee21c8e30ac17efe44f883977f5cbc2f0178957a03637d6a6906`；
  验证镜像 `4e53351b5c5ac0fd124db2afd26d5e66fb083e5f1fa663869973a58feaf3bb61`。
- 均使用 UID 10001、network none、只读 root、drop ALL capabilities、no-new-privileges
  与独立 /tmp tmpfs；没有 privileged/unconfined。真实绑定导入、worker/launcher/wheel
  hash、598/580/353/326 清单、零文本缺口与两项上游 notice 事实均通过。
- 应用新建临时 DB 正常迁移至 `0092`，SQLite integrity/foreign-key 检查通过，已构建
  WebUI 存在。额外核对镜像实际安装的 697 个 Python 源文件，逐字节 hash 与当前
  worktree 相符；Pi 依赖 metadata 与随包许可均不存在。
- 真实 full XML 加镜像报告执行验收导出器；实际报告判 X11 通过，缺报告/陈旧 probe
  SHA 的两种情形均判 partial，不能拿历史包装报告满足当前源码。
- Linux 目标审计与 Darwin 的 326 个文本 hash 全部相同，完整实际 Linux JSON 保存在
  `final-linux-notices.json.gz`。制品 hashes、构建/探针记录与源文件证明见
  `final-container-packaging.json`、`final-image-sources.json.gz`；三份构建日志和 kernel
  原文另有 gzip，原 P09/P10 报告保持历史实测内容。

**默认容器隔离仍不可用**：实际 unshare 返回 EPERM，launcher 硬拒绝；Code Mode
保持未配置。此处通过的是当前制品装配，不能宣称新容器 worker 字节的 sandbox 已通过。
原生 Linux Host 的独立隔离证据仍对应此前原生制品，当前镜像有自己的真实 SHA。

临时 VM `yuki-final-20261005`（含镜像、swap、Rust/Cargo 构建层）、本次下载镜像缓存、
源码 staging、原始临时日志与本轮 full pytest 的 5.493 GiB 临时目录已在证据保存后删除；
此前两轮测试目录保留，pytest-current 自身本轮改动已回到原目录。新装 Lima 2.2.0 已卸载，禁用 autoremove
以保留其他依赖。项目、前端产物、已安装 Mac worker/binding、全局 Lima keys 及其他用户
内容保留。删除核验与实际剩余空间见 `final-cleanup.json`；未把路径 allocation 总和当成
精确释放量。两个验证镜像随 VM 删除，后续获准部署需按当前配方重建。

当前 90 项离线义务已有实际证据；文本清单通过保留上游两项缺文件/署名事实。未通过项：
本轮最终测试无失败；首次编译 OOM 已修复并保留原记录。下一依赖：P11 真实发送、生产
访问与切换的独立授权和实际实例资料。

本轮未增加付费调用；原真实 API 十二场景结果和累计 39 次请求不变。PR、合并、
真实发送、生产访问、镜像发布与部署：待授权，未执行。

## 长任务真实 Provider 对照（2026-10-05）

本地新增 opt-in 付费测量脚本 `scripts/benchmark_long_tasks.py`、不发请求的汇总脚本
`scripts/summarize_long_tasks.py` 及两份单元/原生 worker 装配回归测试。没有修改生产
运行时代码、旧测试或依赖锁；没有创建新 VM。实测解读见
[长任务对照报告](pi-codemode-long-task-benchmark.md)。

三种任务（480 条跨文件去重汇总、18 层条件依赖链、12 层带一次性审计写入的分段续跑）
各使用两个输入，在历史/新循环 × direct/Code 可用四组运行，共 24 次测量。模型自行
规划，使用真实 DeepSeek、完整固定 76 个工具声明、原生 Monty、FileWorkspace、SQLite、
InvocationService 和 Work supervisor。历史循环按固定 SHA 隔离加载，共用当前调用内核；
不是旧应用原封不动的部署。新循环/Code 可用组实际都用了代码，旧循环有一组自主选择
直接调用。每个输入在四组中的 hash、工具声明 hash 均一致。

原始证据：`long-tasks-unlimited.json`；统计：`long-tasks-summary.json`；局部离线故障
重放：`long-tasks-unlimited-diagnostics.json`。探索期 `long-tasks-initial.json` 与
`long-tasks-comparison.json` 保留，完成条件/配额/中断问题明确列出，不并入正式完成率。
这些原始报告包括 HTTP、程序、答案、业务调用、预算和 Work 状态；没有密钥、请求头
或原始 reasoning。已核对本次输出没有命中实际 API key，脚本 hash 与正式报告相同。

正式执行命令完整保存在对照报告；关键参数为 `--authorize-paid --unlimited-cost
--max-output-tokens 32768 --repeats 2`、真实 `YUKI_MONTY_BINARY` 及四份 prior-report。
没有费用/累计请求/累计工具上限；单请求 600 秒超时、引擎隔离与每段配额保持。连续
十段没有新增成功读写路径或便签、真实暂停/错误时停止样本并记录未完成。恢复是同一
进程的新激活，未模拟 OS 崩溃。没有接触生产数据或发送真实消息。

实际结果：付费装配执行器 `24 passed in 807.92s`，独立任务验收 **16 完成 / 8 未完成**。
分组为历史/direct 3/6、历史/Code 4/6、新/direct 4/6、新/Code 5/6。新 Code 批量计算
两次用 13.47/14.59 秒，较同输入新 direct 约快 4.61/3.08 倍；依赖链四组都通过，
新 Code 比新 direct 略慢。恢复仅新 Code 成功一次；另一次文件全对但 Work 未结束，
仍算未完成。两个旧 Code 恢复样本的局部引擎重放均触发 `code_wait_queue_full`，
默认并发 future 上限为 16，是共同内核限制，不能归为旧循环独有缺陷。

正式轮 388 HTTP，所有用量已返回，按相同公开峰值费率估算 $0.407706。含失败的新
Code 组总计 $0.058602、历史/direct $0.153945。此前 P11/探索期峰值费用或未知保守
留额累计 $0.546281，合计暴露估算 $0.953987；实际账单未核验。用户不设费用上限，
本轮没有因预算或请求数被截断的样本。

本地验证：

```sh
uv run --frozen ruff check src tests scripts migrations
uv run --frozen ruff format --check src tests scripts migrations
uv run --frozen mypy
# 通过；format 1138 文件，mypy 722 源文件
YUKI_MONTY_BINARY="$PWD/.venv/bin/yuki-monty-worker" \
  uv run --frozen pytest -q -p no:warnings \
  tests/unit/test_long_task_benchmark.py tests/unit/test_deepseek_acceptance_harness.py
# 26 通过，7.31 秒，无付费请求
uv run --frozen pytest -q -p no:warnings tests/unit/test_long_task_summary.py
# 10 通过，0.05 秒，无付费请求
YUKI_MONTY_BINARY="$PWD/.venv/bin/yuki-monty-worker" \
  uv run --frozen pytest -q -p no:warnings tests --basetemp <本轮自有临时目录>
# 3298 通过 / 1 跳过，660.00 秒；跳过项需要未授权的私有生产备份
```

全量退出码 0，真实 worker 没有跳过；日志和 worker hash 保存在
`long-tasks-regression.log.gz`、`long-tasks-regression.json`。汇总脚本最后一次元数据/文案
调整后另外重跑 10 项汇总测试和 ruff check/format，全部通过。正式报告、派生汇总与
报告表格逐行核对，24 次记录、388 HTTP 和费用总和一致。正式轮与回归的自有临时
工作区/数据库目录均已删除，原项目和已安装 worker/binding 保留。

未通过的任务样本保留，没有改断言、删样本或放宽完成条件。本次 Git 目标仍是已授权
测试分支 `codex/pi-codemode-experiment`，最终提交/推送状态由回报核验。下一依赖：
若继续改善任务完成度，需专门处理并发程序超限后的模型可恢复错误、便签使用和完成
动作；本轮未把这些修复混入比较。小时/天尺度运行、真实进程故障与生产入口均未运行。

## 恢复故障修复与重新测量（2026-10-05）

用户要求分别修复测试装配和产品代码后重测，继续适用既有真实 API 与测试分支提交/推送
授权。起点为 `c932655c`，没有新增依赖、VM、生产访问或真实发送。

产品改动：Monty 第 17 个 pending future 不再抛出裸 RuntimeError，而返回
`limit_wait_queue` EngineFailure，由已有 driver 结算为 `code_limit_wait_queue` 配对回执。
上限仍是 16，失败丢弃该 VM，未派发 child 明确结算，已执行操作保留原回执；模型可以
根据错误另写分批程序。task_control 固定说明明确所有直接 action 都须独占工具批次，
不放宽执行检查；新的声明由现有内容 hash 自然产生新合同 revision。

测试改动：真实 Runner 装配夹具在接纳前绑定合成 canonical Person/ArtifactAccess；每段
新控制器复制并重绑同一可信来源。付费脚本从当前合成装配导出完整冻结声明，不再将
P00 历史 inventory 当作当前合同；每段记录便签是否保存/可读，HTTP 错误记录附调用 ID，
标明它们是该次请求可见的历史，不能跨请求累加为独立失败数。新增回归覆盖 16/17 个
future 边界、无副作用/已有写入后超限的纠正、原 Work/预算及便签跨两段可见。

报告纠正：旧 direct repeat=1 只有第一份便签因引用错误被拒，第二份合法便签已保存，
但测试缺读取身份导致不可见；原解释重复计数。旧原始 JSON/费用/结果保持，旧恢复率
标明受测试缺口影响；修复后的测量使用新文件，不与旧受影响样本合并。

验证：ruff check、format check（1138 文件）、mypy（722 源文件）通过；带真实 worker
的四份定向测试 65 通过，15.09 秒。全量 **3303 通过 / 1 跳过，670.48 秒**，跳过项
需要未提供的私有生产备份。之后产品代码未再变化；仅修测量停止观察器及新增回归，
受影响的五份测试最终 **77 通过，18.03 秒**，静态检查再通过。分别记录全量与后续
定向范围，不声称全量包含后来加入的两项。

重测发现路径观察器将仍在推进的程序误判停滞。新增回归在修复前被第 55 次调用截停；
保持成功/答案/正式结算断言，修复后同程序完成 97 次调用、2 HTTP。观察器读取原父
operation 的持久快照，只有同一父操作跨激活 revision 增长才算 VM 进展，新父操作或
静止快照不算；便签只比较语义 payload。没有放宽完成标准或提升引擎限额。

三轮 API 都使用用户已授权的无费用上限模式，重测时没有并跑重型验证。首次八项只有
1/8 完成，291 HTTP，$0.303000，仍受路径误判影响。中间版把新父操作误计进展，导致
历史循环不断新建而不是续原程序；已中止并保留 4 项返回及 1 项 in_progress，303 HTTP，
已知用量 $0.333164、未知保守留额 $0.062388，不当成完整对照。

最终报告 [恢复重测](pi-codemode-recovery-retest.md)：**1/8 完成**；新版 Code 为 1/2，
其余三组 0/2。新版两个输入文件均正确、零重复写入，失败输入没有调用 complete，Work
为 queued；仍属任务收尾问题，未修复。成功输入 14.15 秒、6 HTTP、42 业务调用、9 段。
实际队列超限已返回配对 `code_limit_wait_queue`，最终所有样本没有 activation 裸异常
或整 Work 暂停；任务失败样本保留。最终 295 HTTP，全部用量返回，费用估算 $0.286578。
付费执行器 `8 passed in 506.03s` 是八次测量正常返回，不是八项任务完成。

最终命令完整保存于报告，关键参数为 `--authorize-paid --unlimited-cost --repeats 2
--max-output-tokens 32768`、八个 `--case` 与七份 `--prior-report`；原预算未重置。
三轮本次已知费用 $0.922742，加未知留额暴露上界 $0.985131；含此前记录累计上界
$1.939117，实际账单未核验。原始诊断轮、最终 JSON、汇总及日志都保留，未隐藏中止。

最终所有组输入/冻结声明 hash 一致；新报告保留 source hash 和每段便签可见性/代码
检查点。八个付费样本都未保存便签，本轮付费未覆盖便签主动使用；可读性修复由回归
验证。历史主迭代源码没有新 VM 续跑接线，该组只是实验组合，不可冒充旧生产应用。
没有重跑先前批量/依赖链，也没有做 OS 崩溃、小时/天任务、真实生产入口验收。

下一依赖：改进必要工作进度的跨段交接及模型收尾，保留原执行 ID、回执与根预算；
不能从文件正确自动替模型完成任务。真实发送、生产访问、PR、合并与部署仍未执行。
本轮临时付费夹具已自动删除，全量自有夹具已清理，worker/binding 保留。

## 2026-10-05：分段结果交接、段末进度保存及测量装配修复

起点 `b50093c1`，在 `codex/pi-codemode-experiment` 修改与验证。原因和新原始报告见
[分段交接修复记录](pi-codemode-handoff-repair.md)。程序与测试缺陷分开处理：

- 程序：原 paired 回执尚未被下次模型观察时，新业务激活带出最后一轮原调用/结果的
  工作证据，保留原执行键；不恢复旧群史、opaque、签名或 reasoning。段额度耗尽后
  在原模型预算内有一次保存累计 note/complete 的机会，业务剩余调用仍为零。
- 声明：说明字典回执访问、asyncio 显式导入和 Monty 内置模块，累计发现及必要中间值
  需要进入有来源的 note。实际引擎能力没有通过放宽限额改变。
- 测试：旧 Code 隔离装配补两个明确的 VM yielded/resume 钩子，旧 direct 主迭代保持
  原样，两个源码 hash 均记录；记录器不再抢先解析无效参数而暂停 Work。统一实际
  推理强度/分段配置可显式记录，汇总拒绝不同任务配置，按实测额度说明。

新增真实入口 32 次边界与段末收尾；19 种 wire 方言检查原回执在新链可见；五/六次
边界、失败回执纠正、三段累计预算、段末 note 可读、超额写入不可执行、原生 math/
asyncio 和旧装配续原 VM 的回归。既有边界用例显式耗尽模型预算来测无收尾请求的
恢复兜底，行内解释；既有压缩用例改为确实不缩小的合法摘要，保留拒绝及原检查点
不变的断言。修改不是删掉或放宽成功标准。

最终命令：`uv run --frozen ruff check src tests scripts migrations`、
`uv run --frozen ruff format --check src tests scripts migrations`、`uv run --frozen mypy`、
`YUKI_MONTY_BINARY=$PWD/.venv/bin/yuki-monty-worker uv run --frozen pytest -q -p no:warnings tests`
（自有 basetemp 仅用于夹具清理）。最终 **3352 通过 / 1 跳过，708.35 秒**；1139 文件
format、722 源文件 mypy 通过，worker 无跳过。唯一跳过仍是未提供私有生产备份路径。
首次全量的一项旧夹具失败、主动中止的复验及最终完整复验分别保留原日志和 JSON。

全量之后源码未改，真实 API high reasoning 对照固定当前源码：正常 32 次额度下
24/24 完成（四组各 6/6）；澄清报告必须读回实际文本后，五次分段压力复验 7/8 完成，
其中新版 direct/Code 各 2/2、旧 Code 2/2、旧 direct 1/2。旧 direct 的失败为文件、
报告读取都正确但 Work 仍 queued，十段无新进展后停止；耗时和费用保留。程序、
装配、记录器和任务表述问题分别有复现与修复证据，没有把 completed 自动当作成功。

既有任务“验证文件”与必须 workspace_read 的验收条件曾不一致，现同时明确四组
任务要求，输入与答案不变；新增四组正确文件/completed 但缺回读仍失败的反例，已
包含在最终全量。较早受影响的压力诊断七项返回及一项 in_progress 保留，不作为
完整八项对照。正常额度成功轮全部 24 项实际回读，记录原样保持。应用默认模型配置
未改；本轮 high 结果不冒充较早 low 的完成率。

使用全部历史账本，不限制费用。最终压力轮 262 HTTP、已知峰值 $0.734534532、
无新增未知用量；六轮本次修复测量已知费用 $2.294465292，本次未知留额 $0.1263912
保留。含此前全部记录的累计暴露估算 $4.359973764，实际账单未核验。详表与原始
命令见修复报告及 `long-tasks-verification-final*.json`；失败和中止成本没有删除。

当前范围无未解释的程序/测量失败；私有备份用例因缺数据未运行，生产、OS 崩溃及
小时/天尺度任务仍未运行。下一依赖属于这些额外验收，或使用应用默认配置扩大样本。
源码、测试与本节证据作为同一测试分支提交交付，具体提交号及远端一致性核对见本轮
最终回报。未创建 PR、未合并、未部署。四个全量自有临时数据库根目录
已删除，删除目录的 du 计量合计约 17.56 GiB（不是瞬时磁盘净释放量），付费夹具已
自动清理，本轮未新建 VM，worker/binding 保留。


## 2026-10-05：与 main 的兼容整合

实验起点 `3d1d9838`，整合 main `de9d0e3a`；范围是测试分支兼容修复，main 不写入。
[完整修复记录](pi-codemode-main-compatibility.md)列出实际冲突、迁移和验证。

- 保留实验分支单一新循环、原 Monty VM、调用身份、已确认效果、根预算和未观察结果
  交接。接入 main 的只读来源/配置/历史优化、缓存和有界 GC、空记忆重建及 Gemini
  提取并行。原生工具保护与调用方收尾移到 TurnExecution，旧 `_run` 不恢复。
- main `0092`—`0095` 原文保留；实验调用索引接为 `0096`，补齐旧实验 `0092` 缺少的
  main 维护索引。真实两个旧分支 producer 的正常升级、启动、事实与回执完整性、
  重复升级和 downgrade 护栏通过；不 stamp、不改写原任务/预算/回执。
- 新增真实 worker 的三激活发送/内部结果组合回归，确认同一 Work、累计预算及重复
  调用不重发。新增迁移核验的字符串归一缺陷由实际 SQLite 反例复现，已修正；错误
  标识符、Unicode 标识符、JSON 路径、唯一性、谓词及列形状漂移均在 DDL 前拒绝。
- 原迁移链 head 断言更新到 0096 并注释；main 的 0095 自有 DDL 验收固定到 0095，
  保留原全库事实、schema 差异、downgrade 不变断言，并另验当前 head 启动与事实。
  没有删掉或放宽成功、未知围栏、预算或不重发的检查。

定向检查分别 53/70/2 项通过；head 夹具修正后 78 项通过；空内部结果兜底条件与 main
精确对齐后 17 项通过；最终迁移联合检查 87 项通过（28.22 秒）。首次完整检查
3919 通过、1 失败、1 跳过，868.45 秒；唯一失败是旧夹具把当前 head 写死为 0095。
两个主动中止轮分别 361/2715 项通过、1 跳过，128.15/566.01 秒，不算完整通过。
新增核验程序缺陷的两个拒绝反例先实际失败，再修正生产核验；失败/中止原记录保留。

最终命令：

```sh
uv run --frozen ruff check src tests scripts migrations
uv run --frozen ruff format --check src tests scripts migrations
uv run --frozen mypy
YUKI_MONTY_BINARY=$PWD/.venv/bin/yuki-monty-worker uv run --frozen pytest -q -p no:warnings tests
```

pytest 自有 basetemp 仅用于夹具清理。最终 **3926 通过 / 1 跳过，813.13 秒**；
format 1188 文件及 mypy 724 源文件通过，真实 worker 无跳过。唯一跳过仍为私有生产
备份路径未提供。当前代码 hash 与验证源一致，日志及 SHA 见
[最终完整证据](pi-codemode-evidence/main-compatibility-accepted.json)。

四个自有全量临时根目录已删除，du 分配量合计约 19.287 GiB；不是瞬时磁盘净释放量。
worker/binding 保留，未动共享 pytest 临时根或用户数据。本轮未新建 VM、未跑付费
API 或真实 QQ；Docker 包装探针更新 head，但未重新装配/验收镜像，不冒充部署。
以前的长任务费用与完成率保持历史记录，不当作本次对最新 main 的性能比较。

源码和证据随本轮测试分支提交；实际提交号、推送及祖先/远端核对见最终回报。
下一依赖：后续合回 main、发布和部署须另授权；私有备份用例缺数据、本轮未运行。

## 2026-10-05—06：main 原生媒体与历史投影兼容

实验起点 `c4afe62d`，接入 main `3ca36510`（PR #247、#248）。上游媒体能力、附件
缓存冷哈希优化以及重建历史排除当前 trigger 均保留；迁移 head 仍为 `0096`。
[兼容记录](pi-codemode-main-compatibility.md)说明冲突和对应的当前合同。

- 原生媒体进入现有 TurnExecution 与 typed Invocation；主合同更新到 version `12`，
  不恢复旧循环。工具回执全部配对后加入 Host 图片观察，实际派发前再次检查全链
  图片预算、来源、文件版本与权限，包括已进入 opaque continuation 的图片。
- Code Mode 子调用图片保留为私有 Host 数据，父回执按原子序号聚合并持久化；
  公开 JSON 与 VM 不携带像素。组合起点的隐私版本固定并持久化，父回执发布与
  来源检查及原效果 CAS 在同一短事务，迟到结果不能重新发布已删除图片。
- 只读复用先持久化原 receipt key 再派发；恢复复核只读属性、参数签名与原链，
  未派发/未知不认定成功。WorkerBackend 显式委派媒体校验并保留 MediaResultText，
  无校验器时拒绝派发，预算拒绝仍保留已执行的组合操作与原副作用事实。

main 的三个新增测试文件按 typed Invocation 更新夹具，并添加解释；原配对、权限、
预算、原回执与不重发断言全部保留。新增四个真实 worker 回归覆盖直接及分段图片、
父回执重读、删除围栏和主循环观察；固定组合隐私版本的拒绝回归也通过。
最新历史投影九项原测试通过。首轮完整检查 **4007 通过、2 失败、1 跳过，950.15 秒**；
两项失败为旧 Code 对照装配缺少新的媒体预算方法，仅补明确共享方法，不改生产判断
或验收断言，24 项相关回归通过。较早的主动中止轮保留原状态，不算全量通过。

最终命令沿用四项共同检查，pytest 显式设
`YUKI_MONTY_BINARY=$PWD/.venv/bin/yuki-monty-worker`，使用自有 basetemp。
**4022 通过、1 跳过，960.10 秒**；ruff check、format（1201 文件）和 mypy（726
源文件）通过。唯一跳过为未提供私有生产备份，真实 worker 无跳过。源码、worker、
退出码及日志 hash 见[最终联合证据](pi-codemode-evidence/native-media-main-compatibility.json)。

复验过程中只有 PowerShell 安装器参数传递及旧 WSL 版本查询兜底变更；原始源码
hash 保留，最终脚本的独立行为检查、14 项安装器回归及最终 hash 单列于证据。
其余 Python、运行时和测试源码均未改变。两个原生图片/工具/续接 API 探针实际
通过，分别两次请求；未做真实 QQ 发送或生产访问，不把连通性作为性能对照。

三轮全量自有临时根与便携 PowerShell 工具已删除，压缩日志及失败/中止证据保留；
清理量和范围见[清理记录](pi-codemode-evidence/native-media-windows-cleanup.json)。
开发 worker/binding、共享临时根和用户数据保留。本轮无 PR、main 写入、镜像发布
或部署。测试分支提交、推送与祖先关系由最终回报单列。

下一依赖：使用同一份已验证的源码提交生成私有 Windows 包；目标 Windows 的
原生运行和 QQ 扫码尚未运行，私有生产备份测试仍缺数据。

## 2026-10-06：Windows 私有一键部署包

用户在最新 main 兼容后继续要求 Windows 一键安装。当前兼容提交 `783d0873`
是 `c4afe62d` 与 `3ca36510` 的正常双父合并；安装器使用随后已验证的完整源码提交，
不使用旧发行镜像。公开仓库只保存通用脚本，真实密钥、完整角色提示词和机器人
账号仅在仓库外生成的私有包中。首次部署由用户输入独立管理员 QQ。

新增 `deploy/windows/` 与 `scripts/build_windows_private_bundle.py`：

- 全新 x64 Windows 10/11 启用 WSL2、缺新 WSL 时安装校验固定 SHA 的官方 MSI，
  导入独立 `Yuki-Bocchi` Ubuntu 24.04，重启续装且拒绝覆盖其他所有者的环境。
- Bot / Monty 以普通 `yuki` 用户运行；NapCat 通过同一 WSL 内的 Docker Engine
  启动，使用固定 amd64 manifest。编译固定 Monty 与 wheel、构建 WebUI，校验
  原生 namespace / UID / 无网络 / watchdog / 所有者死亡，不放宽隔离规则。
- 原子配置接入完整角色提示词、Anthropic 主/后台 profiles、全部 ModelTask 路由、
  Work / 自动化 / 子 Agent / Code Mode 与原生图片；不硬编码用户密钥或管理员。
  管理凭据随机生成，Linux 私有文件和 Windows 安装目录限制访问。
- 现场检查真实 API 的图片、工具与原回执续接，再正常迁移至 `0096` 并只读
  quick_check；服务与 Windows localhost 均通过后才标记安装完成。重试先停
  原 Bot，systemd 重启后再次停，避免第二个 SQLite 写者。QQ 登录仍由用户扫码。
- 源码 tar 固定 git 提交，私有 ZIP 校验每项 SHA、来源和完整性；Windows 脚本
  使用 CRLF。安装/启动/停止入口保存运行数据，成功后仅删除自有 Monty 构建缓存。

验证没有依赖目标 Windows：14 项真实 Settings / 路由 / 权限 / 私有文件 / 原子
配置 / 安全解包 / 固定 git archive / ZIP 回归通过（0.26 秒），已包含在最终
4022 项通过的联合检查中。PowerShell 7.6.6 的实际 Legacy 模式行为检查、Bash
语法及含临时合成 `.env` 的 Compose config 检查通过，具体输入、输出与 hash
保存在[安装器验证](pi-codemode-evidence/windows-deployment-verification.json)。
它们不冒充 Windows PowerShell 5.1、WSL 导入或目标 QQ 运行测试。

用户真实 API 已验证：原连通探针两次请求 8.009 秒，原生图片探针两次 18.499 秒，
最终安装器自身探针两次 5.845 秒，共六次实际请求；安装器主 profile 接收蓝色原图、
返回指定函数参数后，原 continuation 配对工具结果并回复 READY。对应原始
[安装器探针](pi-codemode-evidence/windows-deployment-provider-probe.json)保留 token
与时间；没有从时间不同推断性能提升，没有真实 QQ 消息外发。

目标 Windows 的导入、原生编译/隔离、迁移、systemd、网关、localhost 与扫码状态
均标记**未运行**，由安装器在用户部署电脑上逐项执行。额外搜索、语音及 gVisor /
Manager 终端服务尚无对应连接，默认不启用；不影响已配置的聊天、原生图片和受限
Python Code Mode。使用方式与这些边界见[安装说明](../../deploy/windows/README.md)。

这次交付范围为本地通用安装器、私有包生成、验证及测试分支提交/推送；没有替用户
在当前 Mac 或生产环境部署。私有包由上述 builder 在提交后生成并独立核对；实际
路径、固定源码提交、ZIP SHA 与推送结果随最终回报交付，不把密钥载荷加入 Git。
