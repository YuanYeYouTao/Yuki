# MCP 架构

MCP 是 Tool Kernel 的一个 Provider，不是第二套 Agent。配置、连接、缓存、目录、Binding、结果
归一化和运维命令分别由 `mcp/config.py`、`connection.py`、`manager.py`、`provider.py`、
`binding.py`、`result_normalizer.py` 和 `admin.py` 负责。

配置启用的 Server 被视为可信工具来源，不需要 MCP Tool 逐项审批；远程返回内容仍是外部资料，
不会授予权限。MCP Tool 与 Plugin Tool 使用同一个 Capability Runtime、能力策略、AgentRunner、调用协调器和
结果预算器。

显式自动化 DSL 的 MCP 步骤通过 `mcp/automation.py` 的通用桥接层接入。只有 Server 配置中
`yuki.automation.includeTools` 明确列出的远端工具才会注册为
`mcp.<server_id>.<remote_tool_name>`；权限、风险、重试、JSON Schema 和输出 Artifact 随
注册定义进入 `AutomationCapabilityRegistry`。这些是持久 DSL 调用名，不会作为另一套
模型工具别名追加到主 Agent。DSL 和普通工具底层均可使用 `MCPToolBinding`，不按品牌分流。

自然语言创建的 Agentic 任务由 `AutomationCompiler` 编译为 `yuki.agent`，执行时使用
主 Agent 的固定工具合同和创建者当前权限。TaskSpec 不再选择或冻结一份 Agent 工具
白名单；MCP 的 `includeTools` 是显式 DSL 注册配置，不是这条主 Agent 路径的工具集合。
底层 DSL 仍供插件 SDK 与内部调用使用。

显式 DSL 的受限委托快照保存远端工具的完整元数据哈希。`tools/list_changed`、手工 refresh 或重连刷新
目录后，桥接层会原子替换该 Server 的动态定义；Schema 改变时旧快照不再匹配，禁用 Server 或
删除允许项时定义会消失。两种情况都会由既有执行器阻止旧任务，而不是把新能力自动补授给它。

启动时准备已启用 MCP 工具，再冻结主 Agent 的完整工具声明。普通聊天即使未调用 MCP，
声明也保持一致；没有运行时目录查询或 Schema 注入。元数据刷新与自动化定义
更新不直接改写已冻结的主 Agent 清单；工具合同变化需要重启形成新合同。
参见 [共同架构约束](../architecture/development-contract.md)。

Gateway 不拥有目标工具的风险。`call` 必须经过 `resolve_tool` 取得当前已启用、已发现并通过
include/exclude 的元数据，再用目标 Descriptor 进入现有 `CapabilityPolicyEngine` 和
`MCPToolBinding`；只读模式、图片/联网限制、scope 与委托权限都在执行处按目标工具检查，
不能通过动态裁剪固定声明代替授权。search 不执行，
describe 只返回定义。

`yuki.toolBundles` 可把一个 Server 的多项工具声明为不可拆分的 semantic namespace，一个工具也可
加入多个 Bundle。Bundle 只解决目录选择完整性，不定义步骤顺序或条件，因此没有额外 Workflow
DSL。远端 annotations 只保留作描述元数据；风险、只读与幂等性由运维配置的
`toolAnnotations` 决定。未配置可信覆盖时默认按修改状态处理，不能用远端
`readOnlyHint` 或 `destructiveHint` 自报来取得权限或安全重试资格。

MCP 工具成功结果的提交状态默认未知，由 Tool Kernel 按目标 effect 解析；只读成功为 false，
写入成功为 true；上游 `isError=true` 时，`mutation_committed` 为 false。普通工具/业务错误、4xx 和 429 不会销毁连接；
只有会话失效、网络断开、协议或初始化失败才断开。协程取消原样传播，不记为失败或触发重连。

## 工具结果、来源证据与诊断记账

主 Agent 的 Work 调用先按原 journal call key 提交 accepted 结果，再执行该调用栈中的
次级记账；提交结果未知时不运行次级记账。记账失败只报告 coverage_incomplete，不能把
已确认的 Social 投递或 Work effect 改成 unknown，也不授权再次执行工具。取消仍传播。
无 Work 的调用及独立 SDK 路径保留同步来源校验，不能借诊断降级绕过真实来源合同。

MemoryToolReceipt 是有来源的证据，不进入可丢弃的诊断队列。结果先在锁外脱敏，短
INSERT 再核验原内部事件或 initiative run、canonical owner、generation 和隐私删除代次。
无效来源拒绝补写。事件回执固定到原内部事件所属 Conversation 的 kind/person/space，
QQ Binding 迁移不能把旧事件重新归属到新 owner；锁外准备及短写入都核对该原分区。
原 SELF call key 保持不变；有原 execution/call identity 的事件回执也用既有
source_call_key 去重，可只重做原 effect 的证据记账而不重做工具。失败没有
独立永久重试队列，进程终止时的缺口仍须按原持久 effect 及可信来源确认后补建。

内容无关的 ToolInvocation 单独复用 [执行过程查看](../architecture/execution-trace.md)
的有界诊断队列，Work 在派发前冻结原 Conversation、generation 和删除代次，审计登记
保留此 token，不能在 accepted 后重新读取代次作为旧调用的授权；证据与诊断写入再次核验
原 token。聚合仅含已落盘
样本，队列拒绝或异步失败报告覆盖缺失；统计失败不会回滚已经提交的来源证据。
