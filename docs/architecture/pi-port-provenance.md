# Pi 与 Monty 固定来源

取得日期：2026-10-04。以下源码已用 Git 按精确 SHA 获取；尚未完成的移植不标已实现。

| 项目 | 固定 SHA | 许可 |
| --- | --- | --- |
| Yuki | `8204b28ebc8939213dae60dbab94ab1c16d1263a` | 保留原项目许可 |
| Pi | `200387122ca450d6387f033949423114a270b96c` | MIT，Copyright 2025 Mario Zechner |
| Monty | `3f9d6ef413fb951e5b80113b7088d535bd028fcb` | MIT；构建与转依赖 notice 尚待核对 |

旧参照 `755f7250e0ac465e57e748ea2e6583d1a76353b0` 是当前基线祖先，实际相差 65 提交。
旧文件较短不表示较新，也不能据旧包缺失删除当前 `DurableInvocations` 所有者。

## 源符号与目标责任

| 固定源 | 采用符号/结构 | 目标 | 当前状态 |
| --- | --- | --- | --- |
| Pi `packages/agent/src/agent-loop.ts` | `runAgentLoop/runAgentLoopContinue/runLoop` | `agent_core/loop.py` | 未移植 |
| 同上 | `streamAssistantResponse` | model boundary + 原 TaskModelExecutor/TurnTranscript | 未移植 |
| 同上 | 工具 prepare/execute/finalize | InvocationService + 原 Backend/Coordinator | 未移植 |
| Pi `packages/agent/src/agent.ts` | run lifecycle、事件归约 | `agent_core/state.py/events.py` | 未移植 |
| Pi `packages/agent/src/types.ts` | 被采用的事件/结果类型 | `agent_core/types.py` | 未移植 |
| Monty `crates/monty-python/src/snapshot.rs` | Function/NameLookup/FutureSnapshot | `codemode/engine_monty.py` | 未接入 |
| Monty `crates/monty-pool/src/worker.rs` | 原生 subprocess、清空环境、受控 pipes | 固定 native worker | 编译中，未验收 |

Pi 完整 SDK、pi-ai Provider、durable/chord、CLI/TUI/RPC 不进入本交付。
实际移植文件必须带原 MIT 许可和署名，并在本页记录目标符号、源范围与语义测试。

## 刻意差异

- Yuki 固定声明，不采用 Pi 的动态 `declareToolChanges` 策略。
- 保留严格 JSON Schema，不移植 TypeBox 数值/布尔/null coercion。
- 有界读取并发；修改、发送和控制保留 Yuki 屏障，不能采用默认无限并发。
- 普通失败保存 receipt；取消、租约和存储故障继续交原 owner，不能统一扁平化错误。
- Work inputs 是持久输入真源，不复制 Pi 内存 queue 作为恢复账本。
- 当前 complete Provider 映射完整 frame；partial 只用于边界测试且不得执行工具。
- Provider 原私有签名/opaque 不经业务 JSON canonicalizer 重写。
- 原领域回执是效果证据；Pi 事件、nested calls 和 VM return 不承担持久真源。

各差异的运行 fixture 待对应阶段实际执行后补入，当前不宣称差分测试通过。
