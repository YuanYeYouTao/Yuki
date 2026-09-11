# Yuki Main Agent 请求对照报告

日期：2026-09-11。此报告记录已实现入口的定向验收，不代表任务书全部完成或生产已上线。

## 捕获方法

场景代码：`tests/support/main_agent_wire_cases.py`，由现有自动化集成测试调用，不增加 collected test 数量。

使用真实数据库、ContextAssembler、PromptComposer、MainAgentTurnService、AgentRunner、TaskModelExecutor、ModelRouter 和两个 Provider。仅在 HTTP 边界使用 MockTransport，测试不自行构造 ChatRequest。清单由核心能力与标准自动化 registry 冻结生成，包含真实的非空函数 Schema。

普通消息通过 MessageProcessor；自主回复从已入账消息和真实协调 token 进入 ChatService.respond；插件事件先通过 scoped ledger 入账，再进入 generate_main_agent_wakeup；自动化分别进入 generate/agent。QQ 投递使用测试回执，不向外部发送消息。

## 已覆盖矩阵

每行分别运行 Responses、Chat Completions，两种协议共截获 40 次 HTTP 请求。

| 入口 | 读取/执行条件 | 每协议请求数 | 断言 |
|---|---|---:|---|
| 普通私聊 | 普通用户，真实入站消息 | 2 | 全局状态写入成功，正常投递 |
| 管理员群聊 | 管理员，真实群入站消息 | 2 | 管理员动态资料不改变固定字段 |
| 自动化 generate | creator_private 历史，外部执行权限为空 | 3 | 状态写入成功；可见发送工具执行被拒绝，处理器未调用 |
| 自动化 agent | creator_private 历史，既定委托边界 | 2 | 状态写入成功，沿自动化后端处理 |
| 插件事件唤醒 | 已入账 external_event，无当前用户角色 | 2 | 正常 Assembler/Composer，无伪造入站用户 |
| 自主群回复 | 已入账群消息，自主协调 token | 2 | 使用正常聊天生成和测试投递 |
| SDK generate | 真实 Host 入站绑定，纯生成 | 3 | 共享声明与状态；发送拒绝，未调用发送处理器 |
| SDK generate_with_context | current_user 有限资料 | 2 | 不扩大历史读取，资料位于动态区 |
| SDK agent.run | 真实 Host 入站绑定，能力交集为空 | 2 | 使用共享编译与执行入口 |

所有场景显式使用函数联网模式 Tavily；不以 Provider 不支持原生工具而静默忽略的配置模拟原生策略已经解决。

## 硬断言与结果

- 同协议内，各入口的固定指令、完整工具清单、模型和其他固定 HTTP 字段相同；比较保留 JSON 字段插入顺序，不仅使用忽略键顺序的 dict 相等。
- 每一对相邻请求的历史/input 前缀完全相等；模型输出和工具结果只追加到尾部。
- 成功写入有 `ok=true` 的真实 ShortState 回执。纯生成尝试发送返回 `capability_not_allowed`，绑定的发送处理器调用次数为零。
- 续轮及拒绝后的最终请求保留相同声明；`tool_choice` 是执行控制字段，不纳入固定字段相等断言。
- 验证命令：`.venv/Scripts/python.exe -m pytest tests/unit/test_automation_runtime.py tests/unit/test_deepseek_responses.py -q --tb=short`。48 项定向测试通过；本批修改源模块类型检查与 Ruff 通过。未运行全量。
- SDK 扩展验证：`.venv/Scripts/python.exe -m pytest tests/unit/test_automation_runtime.py tests/unit/test_plugin_facades.py -q`，44 项通过，4 个源模块类型与 Ruff 检查通过。缺失/错误来源、私聊读取群资料、generation 失效和递归生成均在 HTTP 请求前拒绝；不将这些拒绝用例算作额外 HTTP 请求。

## 尚未证明的范围

这不是生产安装插件/MCP 清单的启动验收；插件调度器和自主准入策略本身不在本矩阵内。SDK 生成接口已覆盖上述直接入站场景；缺少真实绑定的后台旧调用明确报迁移错误。D1/D2/D3 已确认，原生联网合同与有界持久投影仍待实现，不能视为已验证。

本矩阵没有代替所有媒体/结构化 @/语音的最终 HTTP 对照，也没有证明 Rollup、改名、重启、删除后的跨轮投影稳定，或全部恢复/取消路径。相关已有定向测试只证明各自覆盖的行为，不能合并解释为这些维度全部通过。最终上线仍需完成任务书其余工作和最终集成检查。
