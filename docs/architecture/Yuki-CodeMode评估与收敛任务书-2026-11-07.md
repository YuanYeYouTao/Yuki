# Yuki Code Mode 评估与收敛任务书

日期：2026-11-07

状态：**调查与任务书，产品代码尚未修改。** 作者同意后，M01 已于 2026-11-07 20:42 UTC 在本机生产执行：`compose.local.json` 设 `CODE_MODE_ENABLED=false`（原件备份为 `compose.local.json.bak-codemode-20261107`），只重建 bot 容器；当时无进行中的 Work。重启后 `/healthz` 报告 `main_agent_manifest.mode=direct`、`model_tool_count=29`，OneBot 已重连。尚未用真实对话验证被隐藏工具可直接调用。M02 及之后的删除另行记录。

Issue：#573。关联：[普通执行连续性与插话接续任务书](Yuki-普通执行连续性与插话接续任务书-2026-11-07.md)、[服务器文件传到本人电脑任务书](Yuki-服务器文件传到本人电脑任务书-2026-11-07.md)、[Tool Kernel](tool-kernel.md)、[Code Mode 运维](../operations/pi-codemode-operations.md)。

## 1. 基线与方法

- 源码 main `5af28501`；生产镜像 `yuki:local-75e33e92`，`CODE_MODE_ENABLED=true`，worker 为 Monty。
- 生产库只读查询 `execution_trace_entries`（保留范围约自 2026-10-08 起）；未写库、未改配置。
- 对照：Pi `earendil-works/pi@200387122`（`pi-port-provenance.md` 固定提交）的 `packages/codemode`；SuInk/Diana `71db9cb0`（只读源码）。

## 2. 当前结构（已核实）

- Code Mode 开启时只直接声明 `DIRECT_TOOL_NAMES` 中的 26 个工具（`codemode/tool_visibility.py:14-43,68-72`）；生产中被隐藏的是 `automation_manage`、`automation_query`、`environment_service`、`recall_own_message`、`save_conversation_attachment_to_workspace`。
- `execute_code` 在没有已接纳 Work 时返回 `accept_work_before_execution`（`services/agent_runner.py:597-606`）；直接调用未声明工具返回 `tool_not_declared`。**普通聊天里被隐藏的工具完全不可达。**
- `lookup_tools` 是模型层工具，每查一次多一轮模型请求（`agent_runner.py:849-876`）；脚本内函数名为 `yuki_<name>`（`codemode/api_projection.py:28-33`），与直调名不同；`execute_code` 说明不列出可用函数（`codemode/contract.py:36-52`）。
- 段额度到限时抛 `CodeCompositionYield`，下一段从 Monty VM 快照续跑（`agent_runner.py:626-631`，`tool-kernel.md:114-126,166-171`）。
- 规模：`src/yuki/codemode/` 2633 行，另有快照与边界发布分散在 `work_repository.py`、`protocol_store.py`、`work_session.py`；Rust 构建 Monty、bwrap + seccomp/AppArmor 隔离、独立 codemode 镜像与 CI job；31 个测试文件约 9050 行。
- 与现行合同冲突：`development-contract.md:51` 规定“默认 direct 声明完整工具……Code Mode 为可选调用语法，复用同一清单”。现实现开启后实际收窄了直调能力。

## 3. 生产实际表现（已核实）

- 在 trace 保留范围内，`execute_code` 首次出现在 2026-11-07 18:57（Mac 迁移、PR #570 当天）；全部 28551 次请求中仅 186 次声明了它。**此前从未在生产运行。**
- 开启后约 183 次模型响应中，`execute_code` 被调用 2 次，`lookup_tools` 4 次。唯一一次真实使用（Work `8a57a50b`，迁移验收）：第一个脚本因使用原工具名而 `NameError`；随后连续 4 次 `lookup_tools`，每次约 3 万 token 提示；第二个脚本 5 个子调用中 1 个失败、1 个 pending，最后仍在脚本外用 `terminal_read` 补读。共 9 次模型请求，直调约 3–4 次可完成。
- 可观测性：`execute_code` 与 `lookup_tools` 不写 `tool_start/tool_end`；`turn_end.tool_calls_used` 记 1，实际执行 5 个子调用。
- 被隐藏工具的历史用量（10-24 至 11-07 18:50，共 6534 次工具调用）：`automation_query` 277（273 在 Work 内）、`automation_manage` 21、`save_conversation_attachment_to_workspace` 13、`recall_own_message` 1。其中 35 次普通聊天调用在现配置下已不可达。
- 节省的提示：工具 schema 由约 35.1k 字符降到 30.8k，另加约 0.7k 编排政策，净省约 3%。
- 此前基准均为 DeepSeek Flash 在合成工作区任务上的 n=1–2 样本；社交提醒、附件、跨机终端等真实负载和 gemini-3.8-flash 从未测试。

## 4. 与 Pi、Diana 对照

| 方面 | Pi `packages/codemode` | Diana | Yuki |
| --- | --- | --- | --- |
| 直调 | 默认 `mode=on`，原工具保持直调；`only` 才隐藏 | 原生 function calling + 目录 + `tools_execute` 信封，一步可调 | 开启即隐藏部分工具 |
| 发现 | 脚本内 `searchTools/describeTool`，不增加模型轮次 | 目录行直接写参数签名，参数错时回完整契约 | `lookup_tools` 每次一轮模型请求 |
| 命名 | `tools.<原名>` | `tools.<原名>` | `yuki_<名>` |
| 前置条件 | 无 | 无 | 必须已接纳 Work |
| 恢复 | 每次新起 QuickJS worker，无快照 | 同左，单次运行限时 | Monty 原生隔离 + 跨段 VM 快照 |
| 实际使用 | — | 两天 809 次可选请求中选用 7 次 | 生产 1 例 |

照搬得对的：子调用不进模型上下文、失败作为回执返回、子调用走同一执行器和授权。被 Yuki 合同改坏的：Work 门槛、精简直调集合、模型层发现、跨段 VM 快照续跑。

## 5. 结论

对当前主力模型与 Yuki 的社交加终端负载，Code Mode **净收益为负**。主因是设计（精简直调 + Work 门槛 + 模型层发现 + 名称不一致），不是 Monty 引擎。沙箱终端本身已能运行 Python 与 Shell，覆盖了绝大部分批量计算需求。

## 6. 修改方向

| 方案 | 内容 | 评价 |
| --- | --- | --- |
| A | 生产设 `CODE_MODE_ENABLED=false` | 零代码、立即恢复 29 个工具直调；是其他方案的前置止损 |
| B | 改为 Pi 的 on 模式：删除 `DIRECT_TOOL_NAMES` 收窄与 `lookup_tools`，开启时声明全部工具再加 `execute_code`；脚本名与工具名一致；删除 Work 门槛 | 保留批处理能力，消除 P0/P1 设计问题 |
| C | 在 B 上删除跨段 VM 快照续跑，段末结算交还模型重新规划 | 与 Pi 对齐，复杂度大降 |
| D | 整体删除 Code Mode：`codemode/`、快照存储、Monty 构建、seccomp/AppArmor、codemode 镜像、CI job、文档与测试 | 最符合删除优先；旧回执按原 journal 只读保留 |
| E | 只往 `DIRECT_TOOL_NAMES` 补工具 | 打补丁，不采用 |

**推荐顺序：先 A 止损；随后在 D 与 B+C 之间由作者决定。** 依据现有数据（生产 1 例、终端已承担计算、Diana 与 Pi 的实际使用率都很低），本文倾向 D。若保留，按 B+C 以删除为主实施，不新增机制。

## 7. 实施项

| 编号 | 内容 | 依赖 |
| --- | --- | --- |
| M01 | 生产关闭 Code Mode（运维动作，需作者确认） | — |
| M02 | 作者在 D 与 B+C 之间决定 | M01 |
| M03-D | 删除前核对：已配对 composition 的回执只读路径、`work_journal` 中 code 相关恢复分支、`main_agent_contract.py` 合同字段、Desktop/Client 对 Code 字段的消费、发布资产与 `deploy/security`、`scripts/build_monty_*`、`vendor/monty`、CI native job | M02 |
| M03-BC | 删除收窄与 `lookup_tools`、改名、删 Work 门槛、删快照续跑；补 `execute_code` 的工具轨迹与子调用计数 | M02 |
| M04 | 同步 `development-contract.md:51`、`tool-kernel.md`、`pi-codemode-operations.md`、README 与 `.env.example` | M03 |

## 8. 验证

- A：关闭后普通私聊能直接调用提醒、附件保存与撤回；产出一次真实请求的工具声明清单作为证据。
- D：删除后全量 `ruff`、`mypy`、单元与集成套件通过；旧库中已有 code composition 回执的 Work 可正常查询与结算（用生产库副本离线验证，不写生产库）。
- B+C：用 gemini-3.8-flash 在真实形态任务（批量读取多个附件、多个终端命令汇总）上与 direct 对比请求数、token 与成功率，n 不少于 10，结果作为 artifact。
