# 表情发送

表情复用主 Agent 的 `send_message.emoji`，参数是必填 `goal` 和可选 `emotion`。
可以附带 `text` 一同发送；资产由后端从已采用表情中选择，模型不指定 emoji_id、
路径或 URL。没有独立 `send_emoji` 工具，也没有 before_text/after_text 回复效果队列。

主 Agent 根据上下文决定是否发送。后端只执行目标权限、素材与传输检查，不增加随机
频率门禁，不把最终正文自动发送，也不要求每轮至少产生一条消息。

准备或投递失败返回工具结果，由 Agent 决定是否改发文字；后端不自动补失败说明、
换图或重发。结果 uncertain 时保留原操作 ID 和回执，不猜测失败后再次投递。

参见 [主 Agent 合同](../architecture/main-agent-runtime.md)。文件名仅保留现有文档链接。
