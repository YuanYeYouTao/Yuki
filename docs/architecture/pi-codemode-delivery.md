# Pi 与 Code Mode 开发交付记录

2026-10-04；目标与不变量见 [设计合同](pi-codemode-design.md)。

## 授权与 Git

- 本地实现和隔离测试已授权；本地依赖安装及指定 Monty 编译已单独授权。
- 远端创建 `codex/pi-codemode-experiment` 并拉取已单独授权且实际完成。
- HEAD/远端测试分支/远端 main 均为 `8204b28ebc8939213dae60dbab94ab1c16d1263a`。
- 开始时工作区干净；原主工作树 `main` 未切换、未修改。
- 当前代码提交、代码推送、PR、合并、付费 API、真实消息、生产访问及部署均未执行。
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

尚未覆盖：artifact 发布失败与引用提交中断的独立 fixture（现有 `result_unavailable` 路径
沿用旧测试）、writer 排队后过租约、composition 父的最终结算接口（由 P05 控制门提供）。
`PendingComposition` 目前只被识别，原 owner 驱动在 P03/P04 接入。

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

## P05：完整 Code Mode 能力，已完成（本机，未提交）

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

下一步 P06（发送、记忆与来源权限闭合）与 P08（Provider 与上下文），P07 入口接线依赖 P06。
P09–P10 未开始；P11 真实外部与生产验收待单独授权。
Monty Python binding 为本地构建 wheel，未进 `uv.lock`，`uv sync` 后需重跑
`scripts/build_monty_worker.sh`，P09 落地可复现分发。
