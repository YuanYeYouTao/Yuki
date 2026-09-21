# 表情与回复序列

表情是 Main Agent 的回复效果，由 `send_emoji` 请求。工具参数只有语义意图：

- `intent`：当前消息是否明确索要表情
- `mode`：`none/optional/preferred/emoji_only`
- `placement`：`before_text/after_text/only`
- `goal`：期望的社交作用
- `emotion`：目标情绪

Schema 禁止 `emoji_id`、路径和 URL。最终资产始终由后端选择。`emoji_only` 也必须走 Main
Agent，不再跳过上下文装配或 Agent。回复完成条件是至少存在一个可见输出；纯文字和需要正文
合成的语音仍不会把空响应当作成功。每个发送工具独立返回真实投递回执；选择失败时由 Main
Agent 决定是否改发文字。

日常是否发送表情由 Main Agent 自行决定。后端不再维护随机频率或近期投递比例门禁；复杂请求
仍由 Main Agent 正常生成正文并调用 `send_emoji`。

表情准备或发送失败由后端确定性恢复：optional 只跳过媒体并继续正文，preferred 保留正文并补一
条短说明，emoji-only 只发送失败说明。恢复不会重试原图、自动换图或重新进入 Agent。
