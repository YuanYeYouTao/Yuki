# 设计参考与 Monty 依赖来源

取得日期：2026-10-04；参考关系于 2026-10-05 按用户要求澄清。Pi 只作为设计思路和行为
对照的参考，不作为安装、运行或构建依赖。Yuki 的 Python 核心使用自有消息模型、固定
执行边界、持久回执及预算机制；Monty 则是实际构建、加载和执行的第三方组件。

| 项目 | 固定 SHA | 关系 |
| --- | --- | --- |
| Yuki | `8204b28ebc8939213dae60dbab94ab1c16d1263a` | 本项目基线；保留原项目许可 |
| Pi | `200387122ca450d6387f033949423114a270b96c` | 设计参考与行为对照；不加入依赖清单或制品 |
| Monty | `3f9d6ef413fb951e5b80113b7088d535bd028fcb` | 实际依赖，MIT，Copyright Pydantic Services Inc.；598 个锁定包（580 份 registry 归档逐 checksum 校验、18 个本地 workspace 包）；typeshed 原 Apache 2.0 许可已保留，全文清单已补齐，上游两份 MIT 署名缺失单独记录 |

早期记录中的“语义移植”统一改为“参考行为、在 Yuki 合同下实现”。当前没有 Pi SDK、
TypeScript 源文件或 Pi 的依赖链进入运行环境。下表保留查阅对象以便复核思路，不将
行为相似或名称对应当成包依赖。历史构建曾包含 Pi 许可文本，原证据仍保留；当前
wheel 和 Dockerfile 已取消这项打包配置。本次改动不改变模型循环的可执行逻辑。

旧参照 `755f7250e0ac465e57e748ea2e6583d1a76353b0` 是当前基线祖先，实际相差 65 提交。
旧文件较短不表示较新，也不能据旧包缺失删除当前 `DurableInvocations` 所有者。

## 参考行为与现行实现

| 固定参考 | 参考行为 / 实际接口 | 目标 | 2026-10-05 实施证据 |
| --- | --- | --- | --- |
| Pi `packages/agent/src/agent-loop.ts` L102-L327 | 响应、工具结果和下一请求的轮次顺序；Yuki 用三个固定执行边界与请求预算实现 | `agent_core/loop.py` | 已实现；`test_agent_core_loop.py`、`test_agent_core_differential.py` |
| 同上 L381-L470 | 完整 / 不完整响应的区分；Yuki `collect_response` 仅组装合成 frame，真实请求仍经 TaskModelExecutor | `agent_core/model_boundary.py` | 已实现（仅合成 frame）；`test_partial_frames_are_frozen_and_only_done_completes` |
| 同上 L478-L503、L689-L691 | 截断调用不执行、批次结算后决定继续；实际回执和派发仍由 Coordinator＋InvocationService 持有 | `agent_core/loop.py`、`InvocationBoundary` | 已实现；`test_truncated_response_fails_every_call_in_band_without_dispatch` |
| Pi `packages/agent/src/agent.ts` L565-L611 | 事件投影思路；Yuki 使用不可变状态和有界同步队列，不采用 Pi 的可变 Agent 实例与异步流 | `agent_core/state.py`、`events.py` | 已实现；`test_event_order_and_state_reduction`、`test_event_backpressure_drops_diagnostics_never_execution` |
| Pi `packages/agent/src/types.ts` L147、L514-L529 | 继续 / 结束和轮次事件的思路；Yuki 使用自有 dataclass、ChatResponse 和 ToolCall | `agent_core/types.py` | 已实现 |
| Monty `crates/monty-python`（`pydantic-monty-client` 1.0.1） | AsyncMonty、Function/NameLookup/FutureSnapshot、手动 resume、dump/load_snapshot | `codemode/engine_monty.py` | 已接入，真实 worker 验证 |
| Monty `crates/monty-pool/src/worker.rs:185-194` | 原生 subprocess：`env_clear`、piped stdio、`kill_on_drop` | 经绑定使用，不另实现传输 | 已验证（worker-only 构建） |

Pi SDK、pi-ai Provider、durable/chord、CLI/TUI/RPC 均不进入本交付。
实际 Monty 依赖及其传递依赖的来源、许可和上游 notice 状态由当前审计记录。

## 刻意差异

- Yuki 固定声明，不采用 Pi 的动态 `declareToolChanges` 策略。
- 保留严格 JSON Schema，不移植 TypeBox 数值/布尔/null coercion。
- 有界读取并发；修改、发送和控制保留 Yuki 屏障，不能采用默认无限并发。
- 普通失败保存 receipt；取消、租约和存储故障继续交原 owner，不能统一扁平化错误。
- Work inputs 是持久输入真源，不复制 Pi 内存 queue 作为恢复账本。
- 当前 complete Provider 映射完整 frame；partial 只用于边界测试且不得执行工具。
- Provider 原私有签名/opaque 不经业务 JSON canonicalizer 重写。
- 原领域回执是效果证据；Pi 事件、nested calls 和 VM return 不承担持久真源。

唯一模型循环与执行责任见[主 Agent 合同](main-agent-runtime.md)，工具模式、
原调用与 Code VM 恢复见[Tool Kernel](tool-kernel.md)。历史差分样本和阶段导出保留在
`pi-codemode-evidence/`；它们记录当时实现，不构成源码形状或测试数量门槛。

## P09 固定分发与许可证据（2026-10-05）

共用 `scripts/build_monty_distribution.sh` 从精确 Monty SHA 和记录的唯一补丁构建
worker-only runtime 与 CPython 3.12 binding；固定 Rust 1.96.0 / maturin 1.9.6。
本机重复构建 native SHA256 相同：
`af76448a14fe980c823f1c6092692f90a8ddc6341f236f17d6d47d72f49548e8`。
本轮 wheel SHA256 为 `5f77cfbcf15ca0e0bb405d6d586aeb98aacbbcfc10555ba0b882c1b73868aa6f`；
wheel 包装时间可改变归档哈希，不能据 worker 相同宣称 wheel 逐字节可复现。

实际第三方组件完整许可在 `vendor/monty/LICENSE` 和 `vendor/monty/TYPESHED-LICENSE`。
P09 的历史构建也曾打包 Pi 许可文本并核对全文；这是该次构建的实测事实，当前参考关系
澄清后不再将这份文本作为 Yuki wheel 或镜像的必需内容，旧报告不覆盖更新后的制品。
`vendor/monty/THIRD_PARTY_NOTICES.json` 是 Darwin 实际 worker/wheel 对应的审计：
P09 当时的清单包含 598 个锁定包（580 份 registry 归档、18 个本地包），352 个当前 normal/build 目标依赖，
321 份去重 notice；不将锁内所有包冒充镜像实际依赖。原 SPDX 选择表达式保留，
不替许可人选择。typeshed 固定 `0e16ea31d2e188fdc126cb31e7c4fcc6b5a8da96`。

P09 原审计记有 8 个全文缺口，`license_text_audit_complete=false`。这是补充前的历史状态，
当前 JSON 已按下述来源查证更新；没有合成版权署名，外部发布未执行。
Linux 原生分发已构建/安装，353 个目标依赖及实际 hashes 见 `pi-codemode-evidence/p09-linux-distribution.json`；321 份全文逐字节与共享审计核对一致。
应用与验证镜像构建及包装探针通过，证据见 `pi-codemode-evidence/p09-container-packaging.json`。
两个镜像保留各自实际 worker/wheel hashes；不把 native Host 的隔离通过写成默认容器隔离通过。

## Notice 来源补充（2026-10-05）

当前 Darwin 审计仍对应原 worker / wheel 哈希，598 个锁定包、580 份 registry checksum、
352 个目标依赖未变；全文增至 326 份，文本清单缺口为 0。r-efi 两个归档的 AUTHORS
实际含完整 MIT 与版权；symbolic、rustls Android 和两份 winapi 原文来自固定发布提交，
已逐 Git blob 与归档源码对应，生成文件及发布时的版本改动单独记录。

quote-use 两包的原始 manifest 明确声明 MIT，上游没有附文件与版权署名。审计保留
原声明、附固定 SPDX 标准全文，模板的 `<year>` / `<copyright holders>` 不作包署名。
两项 `upstream_notice_omissions` 持续可见；不将“MIT 类型已知”写成“原版权文件已找到”。
证据见 `pi-codemode-evidence/monty-notice-supplement.json` 与根 `THIRD_PARTY_NOTICES.md`。
历史 P09 镜像证据不改写；更新后制品的实测结果见交付记录最后一节。
