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

P06–P08 的离线实现及全量验证完成；继续 P09 迁移、worker 运维和回退。
P09 迁移/资源/完整备份、原生 Linux 隔离及 45 项定向测试已验证，最终全量及两个镜像装配通过，P10 完整实现及离线复验通过，89/90 矩阵项通过、X11 许可项部分通过；真实 DeepSeek 三协议十二场景已有实际通过记录，首轮失败与修正后七项重验均保留；P11 的真实消息、生产访问、
镜像发布与部署待明确授权。
Monty Python binding 为本地构建 wheel，未进 `uv.lock`，`uv sync` 后需重跑
`scripts/build_monty_worker.sh`；P09 共用分发已在 Darwin/Linux 构建并安装；两个镜像装配与离线包装探针通过，容器 Code Mode 默认仍不启用。

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
