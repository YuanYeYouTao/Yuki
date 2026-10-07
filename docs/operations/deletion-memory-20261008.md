# 删除重构补充：内存检查与 direct 模式（2026-10-08）

## 已确认并修复的增长

`memory/rebuild/service.py` 原 `_cancelled_runs` 每次成功取消追加 run ID，无删除路径；即使 run 没有执行中的提取任务，ID 也永久驻留。它只用于区分管理员取消提取与外部任务取消，无须保留历史 run 集合。

现以弱引用集合记录本服务实际取消的提取 task；没有活任务时不建立标记，任务释放时自动移除。持久 run 状态、取消授权、回执和恢复逻辑保持原 owner；未把外部关闭取消吞成业务取消。

`test_memory_rebuild_lifecycle.py` 覆盖一万次无活任务取消不积累状态，以及真实 SQLite 提取中的显式取消与外部取消。联合原 `test_memory_rebuild.py`：26 passed；源文件 ruff / mypy 通过。

## 测量口径

本地 Windows 两个独立 Python 进程分别加载修复前 Git HEAD 源码与修复后源码，共用同一测量程序。单个真实 `MemoryRebuildService` 连续执行 20,000 次取消；数据库边界替换为无持久列表的固定异步返回，隔离被测进程状态，不声称测了数据库吞吐。每 5,000 次后执行 GC，再读 tracemalloc 当前分配与 Win32 `GetProcessMemoryInfo.WorkingSetSize`。RSS 和 Python 分配分开报告；没有将 allocator 驻留误作 Python 活对象。

| 完成取消数 | 修复前保留 ID | 修复前 Python 当前 bytes | 修复前 RSS bytes | 修复后保留任务 | 修复后 Python 当前 bytes | 修复后 RSS bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 5,000 | 5,000 | 813,274 | 94,691,328 | 0 | 96 | 93,233,152 |
| 10,000 | 10,000 | 1,102,524 | 95,477,760 | 0 | 400 | 93,233,152 |
| 15,000 | 15,000 | 1,391,742 | 96,153,600 | 0 | 700 | 93,233,152 |
| 20,000 | 20,000 | 3,253,824 | 99,262,464 | 0 | 1,000 | 93,233,152 |

修复后少量 Python 增长包含测量样本列表本身。私有可复现文件：`.cache/rebuild-memory-probe.py`、`rebuild-memory-baseline.json`、`rebuild-memory-fixed.json`；没有业务正文或凭据。

## 其他生命周期检查

以下为源码 owner 检查，除明确列出的回归外不冒充长期压力验收：

- Conversation locks 为 `WeakValueDictionary`；provider active task 在 finally 移除。真实 direct 主入口连续 8 轮后，locks / active provider / turn task registrations / holders 均空。
- Rate limiter 和 vision limiter 定期清理过期 bucket；BrowserSessions 有会话/peer 上限及过期删除。
- QueryEmbeddingCache 有条目上限与 TTL，inflight 在 finally 删除；ProtocolStore 元数据/原记录缓存分别有条目/字节上限，发布事务收尾释放 prepared sources。
- Memory attribution 虽用无 maxsize Queue，enqueue 已按运行时 queue_limit 在无 await 的区段检查，重复 turn 集合在消费 finally/close 释放。DiagnosticWriter 与插件事件队列有容量上限。
- ActiveWorkBindings 在 finally 解除当前绑定；subagent scheduler 按可用容量选择，结束 finally 删除 running，关闭取消并 join。Work scheduler 关闭 join 子循环；Rollup active map 在 finally 删除；vision singleflight 最终清理。
- Model pool timeout key 的真实调用为默认、compaction、self-reflection 三种策略；配置池替换经 lease 归零关闭，关闭任务完成后移除。没有把固定 provider pool 当作每次请求泄漏。数据库读写使用 session 上下文，Database.close dispose engine；诊断 live spans 在 finally 删除。
- Code native worker 的进程终止由 engine session owner 负责并等待回收；direct 真实回归将 `_code_host` 设为失败陷阱，连续回合未触发，worker direct 没有 script_api 或 execute_code 声明。本轮未进行服务器操作或另建生产 Bot。

`ConversationTurnCoordinator._states` 按见过的 conversation 保留小型版本/来源围栏，清理 task/registration/holder 后仍保留状态。它供迟到 token、延后 observation 和版本匹配使用，直接 LRU/删除会使旧 token 失效或版本复用，不能按通用缓存处理。本轮没有改变该语义；高 conversation 基数下仍需持续观察。这不同于同一会话逐回合保留请求正文。

## 静态模式合同

主 Agent 的 CORE 由 Settings.code_mode_enabled 选择，CLI 默认 CORE 为 direct；worker 按冻结 MainAgentContract.mode 选择。仅替换原模式段，无重复整块提示词。Code 模式主/worker 最终文本与修改前原文逐字节相同；direct 只指导本轮真实声明工具。

Worker 的基础 required 名单不再无条件要求 execute_code / lookup_tools；Code 模式额外要求两者，direct 不生成 ScriptApi。`test_static_mode_prompts.py` 使用真实主/worker入口检查两种模式送达 provider 的提示和工具，联合既有 policy 文件 13 passed。四个生产源码 ruff / mypy 通过。

这些短时本地测量能证明具体增长已消除，不能证明整个进程长期没有内存泄漏。生产稳定态 RSS/Swap、业务量和运行时长应另记，不用旧热进程与新冷启动镜像比较百分比收益。提交、CI、默认镜像构建、部署和自然流量观察由主会话分别记录。
