# Pi 与 Code Mode 交接说明

2026-10-05。接手后续阶段的开发者先读本页，再读下列依据。本页只写接手需要的状态、
环境和约束；逐阶段证据见 [交付记录](pi-codemode-delivery.md)。

## 依据与阅读顺序

1. 仓库 `AGENTS.md` 与 [共同架构约束](development-contract.md)。
2. 外部设计包四份（不在仓库内，向研究者索取）：`01_架构决策与完整设计.md`、
   `02_分阶段开发任务书.md`（P06–P11 在第 8–13 节）、`03_验收矩阵与故障恢复规范.md`、
   `04_给Codex的执行提示词.md`。前三份是完整范围与验收依据，04 是执行约束。
3. 本仓库：[设计合同](pi-codemode-design.md)、[来源与刻意差异](pi-port-provenance.md)、
   [交付记录](pi-codemode-delivery.md)、[Tool Kernel](tool-kernel.md) 的 `execute_code` 节。
4. 覆盖清单：`pi-codemode-capability-inventory.json`（76 个工具）、
   `pi-codemode-acceptance.json`（验收项映射）。

## 当前状态

分支 `codex/pi-codemode-experiment`，基线 main `8204b28e`，已推送到远端。PR、合并、部署均未做。

| 提交 | 内容 |
| --- | --- |
| `22b323c6` | P00 合同、来源、清单 |
| `06b5cd48` | P01 显式调用身份；P02 T2/T3 与迁移 `0092` |
| `b4fdef7d` | P02 T1 发布、restore 分流、硬杀窗口 |
| `10f07855` | P03 Pi 循环移植（`agent_core/`）；P04 Monty 驱动（`codemode/`） |
| `c0593229` | P02 补测：快照发布失败、引用提交中断、writer 排队过租约 |
| `90b6d46a` | P05 `execute_code`、wrapper 投影、子调用接 InvocationService、控制门 |
| `5414065f` | P06 领域回执与来源权限闭合，已推送 |
| `b49e4da6` | P07 全部入口接线，已推送 |
| `bab3448a` | P08 wire、私有续接与父子轨迹，已推送 |
| `85a35aa9` | P09 迁移/资源/完整备份、Linux 原生隔离及两个镜像装配，已推送 |
| `9edc5f1e` | P10 显式 TurnState/三边界、单循环退休与完整离线证据，已推送 |
| `49b2bee1` | P11 首轮原始记录、离线验证和临时资源清理，已推送；后续七项重验实际通过见当前证据 |

当前最后一次全量：3258 通过、1 跳过（未提供私有生产备份路径），731.20 秒；真实 worker
测试无跳过，ruff check/format（1134 文件）与 mypy 722 文件通过。此前 0.2 秒计时失败已改成派发边界触发，
断言保留。P07 旧夹具缺字段/批准的三项失败已修正，全量重验通过。

| 阶段 | 状态 |
| --- | --- |
| P00–P05 | 完成，离线验收 |
| P06 发送/记忆/来源权限闭合 | 离线实现及全量验证完成 |
| P07 全部入口接线 | 离线实现及全量验证完成 |
| P08 Provider/上下文/轨迹 | 离线实现及全量验证完成；前端 89 项通过 |
| P09 迁移、worker 运维、回退演练 | 本地迁移/完整 Manager 备份、原生 Linux 隔离与 45 项定向测试通过；最终全量 3246 通过/1 跳过；应用与验证镜像装配通过（默认容器 Code Mode 不启用） |
| P10 整体验收与旧循环删除 | 完整实现及离线复验通过，已推送：旧 _run/Callbacks/execute/动态 fallback 已退休；四组 12/12、Linux 61 项、全量 3246/1 跳过；89 项离线通过、X11 许可项部分通过 |
| P11 真实外部验收 | DeepSeek 三种配置协议的 12 场景实际通过，累计 39 次 HTTP、无真实业务效果；首轮失败保留；其余外部范围待授权 |

## 已存在、后续阶段直接复用的部件

- **调用身份**：`capabilities/invocation.py` 的 `Invocation/InvocationIdentity/direct_invocations`。
  顶层 ID 为 `chain:request_sequence:provider_call_id`，子调用为 `<父>/c<序号>`。
  领域回执键一律取 `WorkSession.receipt_key`，不要再用响应内 Provider call ID。
- **持久化**：`WorkRepository.prepare_effect`（T0/T1 意图）、`publish_code_boundary`（T1）、
  `admit_dispatch`（T2，预算与 `dispatch_started` 同事务；`charge=False` 用于生命周期控制）、
  `record_effect`（T3，冲突拒绝、保留 invocation/composition 元数据）、
  `composition_children`、`undispatched_intent`。迁移 head `0092`。
- **恢复**：`WorkSession.restore` 在一般 pending 配对前识别未结算 composition，产出
  `PendingComposition`；已配对的 partial 不恢复 VM。
- **主循环**：`agent_core.loop.run_agent_loop`，`AgentRunner` 通过模型/调用/结算三个固定边界驱动。
- **Code Mode**：`codemode/driver.py`（编排）、`api_projection.py`（wrapper）、`contract.py`
  （`execute_code` 声明，主合同 revision 11）、`engine_monty.py`（固定 worker）。
- **测试夹具**：`tests/unit/test_tool_effect_audit.py::active_work`、
  `tests/unit/test_work_effect_results.py::owned_session`、
  `tests/integration/test_invocation_crash_windows.py::{prepared,facts}`、
  `tests/support/codemode_cases.py`、`tests/support/codemode_crash_child.py`
  （真实子进程 `os._exit` + 独立下游日志的模式，P06 的“远端成功但响应丢失”可沿用）。

## 环境准备

```sh
uv sync --frozen --extra dev --python 3.12
scripts/build_monty_worker.sh            # 固定 SHA、套补丁、Rust 1.96.0、maturin 1.9.6
(cd frontend && npm ci && npm run build) # WebUI 测试需要，产物已 gitignore
```

- Monty Python 绑定 `pydantic-monty-client` 是从固定源码构建的本地 wheel，**没有写进
  `uv.lock`**。任何 `uv sync` 都会把它移除，之后必须重跑构建脚本。P09 已增加共同固定源码分发；镜像在最后一次 uv sync 后安装本地 wheel。
- Rust 工具链在 `~/.cargo/bin`，默认不在 PATH，脚本已自行加入。固定 Monty `3f9d6ef` 不打
  `vendor/patches/monty-3f9d6ef-string-cache-iterator.patch` 在 Rust 1.96 上编译不过。
- 本机验证过的 Mac worker 已安装为 `.venv/bin/yuki-monty-worker`（hash 见 cleanup.json）；
  本任务临时源码构建目录已删除，Linux VM 已删除。它是开发依赖，不是生产部署配置。
- 运行配置：`CODE_MODE_WORKER_PATH`、`CODE_MODE_WORKER_SHA256`（为空即禁用 Code Mode）以及
  `CODE_MODE_MAX_*` 限额。真实 worker 测试读 `YUKI_MONTY_BINARY`，未设置时那 27 项跳过。
- aarch64-apple-darwin 和 Debian 12/aarch64 原生 worker 已验证。Linux 还须配置
  `CODE_MODE_LAUNCHER_PATH/SHA256`；测试同时设置 `YUKI_MONTY_LAUNCHER`。实际 hash、
  namespace/UID/环境/rlimit/所有后代退出证据见 `pi-codemode-evidence/`。
- 默认 Docker seccomp 拒绝 user namespace，默认镜像内 Code Mode 保持未配置；不能
  把原生 Linux 通过写成容器隔离通过。专用 VM 验证已完成并删除，证据保存在 repo。
  本任务 Mac 构建/下载/日志缓存及新 Lima 也已删除；其他部署与生产操作仍未授权。

## 验证命令

```sh
uv run --frozen ruff check src tests scripts migrations
uv run --frozen ruff format --check src tests scripts migrations
uv run --frozen mypy
YUKI_MONTY_BINARY=<worker 路径> uv run --frozen pytest -q -p no:warnings tests
```

全量约 10 分钟（机器负载高时 25 分钟），没有 pytest-xdist。阶段内先跑定向测试，阶段结束再跑全量。

## 已知问题与遗留

- `test_automation_timeout_certainty.py` 的 transport deadline 在 P06 改为由实际派发边界
  触发真实 asyncio Timeout，不依赖准备阶段的 0.2 秒剩余期限；原未知及不重发断言保留。
- P10 已将 38 项回合状态收敛到 `TurnState`，请求派发由独立 admission 拥有；
  生产旧循环、Callbacks bag、begin_batch 和 execute 兼容已删除。结构报告在 `p10-retirement.json`。
- 持久 worker 的 `execute_code` 已使用独立固定子集，P07 全量验证通过。
- `update_short_state` 已纳入 P06 直接/子调用授权一致性对照。
- Provider 仍采用完整 response 接口；合成 frame 已验证，未向 Yuki 暴露真实供应商 token delta。P11 核验实际配置的工具、续接、截断与断连，不把 complete 宣称为 streaming。
- 真实 DeepSeek 首轮已运行，验收夹具问题导致部分失败；已修正隔离文件状态及核验指引，
  修正后七项重验全部通过，十二个配置场景均有实际通过证据；首轮记录保留。
  这是隔离 Provider 验收，真实业务/生产验证仍未做。
- 598 个 Cargo 锁定包已校验归档并汇总 notices；8 个完整文本缺口保留在审计中。原生 Linux 隔离、两个镜像装配及包装探针通过；容器 Code Mode 默认不启用。
- 共享 Git 存储的 `._pack-…idx` AppleDouble 文件会让每条 git 命令打印 `non-monotonic index`
  报错，fetch/commit/push 实际成功。没有删除它，接手者也不要顺手清理无关 Git 元数据。

## 后续阶段要点

只列在现有代码上落地时最容易出错的地方；完整步骤以任务书为准。

**P06**：建立派发前 Work effect 与 Social 父操作的所有权关联，分片/附件/附言保留确定性子身份。
任一分片 unknown 阻断后续，成功不改失败，unknown 不被其他成功覆盖。MCP reconnect 与插件 transient
不重放未知写。脚本 `print`、最终返回值、`NO_REPLY` 永不外发。Social fake 下游日志必须独立于 Bot DB。

**P07**：主聊天、SELF、模型自动化、插件主调用、持久 worker 走同一个 `run_agent_loop`。
独立插件计算会话保持 `tools=None`、`max_tool_calls=0`；静态提醒保持确定性路径。child 只给原获准
子集加 `execute_code`，不能调 `memory_change`、短状态写、管理或递归派生。按四个调用者群分开提交。

**P08**：每个外层 `execute_code` 只配一个 Provider 工具结果，子调用只进轨迹，不伪造成
function_call。签名、思考块、原生工具 continuation 原样保留。compaction 不能在父 code 未结算时
切断协议。用 HTTP mock 采集真实序列化 payload 对照。

**P09**：迁移从 755 与 820 两条合法链升级；新子效果存在时旧 binary 不得接管。worker 独立资源池、
无 secrets/DB/Docker/主动网络。Monty wheel 进镜像的方式、Linux digest、许可 notices。

**P10**：四组对照（旧/新循环 × 直接工具/Code Mode），删除旧 `_run`、`begin_batch` 适配、临时
选择器，证明生产只有一个主循环。

## 授权边界

已授权：本地开发、依赖安装与 Monty 编译、在 `codex/pi-codemode-experiment` 上提交并推送。

另已授权：使用研究者指定的本地 DeepSeek 凭据进行真实 Provider 验收；两轮共 39 次已调用，配置的十二场景实际通过；原 $1 ceiling 不变。

未授权，需用户另行明确：PR、合并、真实消息发送、访问生产数据库/凭据/工作区、启动
第二个 Bot 写同一 SQLite、镜像发布与部署。授权以当次对话为准，文档中的步骤不构成授权。
