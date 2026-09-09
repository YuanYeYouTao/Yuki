# 查证可靠性执行记录

当前合同：[收口任务书](Yuki-记忆与查证可靠性收口任务书.md)。实施中，不能作为上线验收报告。

## 线上只读核对（2026-09-09）

运行镜像为 memory-recovery-92483ab1，schema 0051，WEB_MODE 环境值为 native_with_tavily_fallback。
完整自然日窗口为北京时间 2026-09-07 00:00 至 2026-09-09 00:00，数据库 UTC 半开区间对应
2026-09-06 16:00 至 2026-09-08 16:00。未读取/输出密钥与消息正文。

- 函数调用表：Person 读取 3 次、SELF 2 次、历史搜索 4 次（2 成功/2 失败）；没有 web_search 函数记录。
- Web 运行表：**deepseek_native 11 次，其中 partial_failure 2 次**。不是零联网，不能只统计函数名。
- chat_agent 模型请求 257 次，不等于 257 个独立问题；不能直接用作应查询机会分母。
- attribution 请求 186 成功/40 失败；不能据请求成功率推算事实实际使用率。
- 当前容器日志不覆盖这两天（查询零行），不可推断历史工具未暴露。
- 函数调用 16 条均有 runtime_turn_id；关联运行观测后全部来自 user_message。
  这不代表插件/自动化未运行，只说明上述函数记录没有这些 origin。

## C1 观测语义

新增 agent_evidence 内容无关日志，以每次 AgentRunner 调用生成的 UUID 关联，包含 origin，
并携带已有 ambient runtime_turn_id（无真实关联时为 null），可与现有调用/回执表连接。
request_prepared 记录实际函数/native schema 指纹、已暴露的已知读取工具、Web route 与最终收尾状态。
tool_result_staged 区分执行/缓存复用，仅表示结果待交给模型；没有后续响应不能算确认暴露。
response_received 单独报告已接收后续响应的前批结果数，以及本次 native 完成/失败事件和 citation 数。
确认是保守口径：网络失败或取消后模型可能已接收的输入仍不标为已确认；不能当作模型未见。
prepared 不是网络发送计数，不能代替真实验证 transport 预算；native 事件数也不是去重后的调用次数。
不记录工具参数、查询词、账号、引用 URL、结果正文或 provider 原始错误。

已通过 49 项 Web/聊天目标测试；其余 C1 人工机会标注和后续 C2–C5 尚未完成。
