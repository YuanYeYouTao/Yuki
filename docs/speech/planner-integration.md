# 语音发送与人物偏好

语音复用主 Agent 的 `send_message`，通过 `text` 和 `voice` 参数表达。没有独立
`send_voice` 工具、关键词路由或语音会话。`voice.request_basis` 为 `user_requested`
或 `agent_initiated`；可以提供公开 `style_hint` 和 `auto/zh/jp` 语言，不能指定模型、
profile、参考音频或路径。详细 schema 以 `social/tools.py` 为准。

主 Agent 自行决定是否发送语音，后端核验目标权限、入口传入的语音许可、声线和
传输能力。语音会立即发送；需要再发文字时由 Agent 明确调用另一条 `send_message`。
不维护概率门禁或独立回复效果队列，不把内部最终正文自动送出。

持久偏好由 `set_voice_preference` 写入，归属于 canonical Person，平台账号只用于
解析身份；来源、当前执行主体及持久修改权限由现行 preference service 核验。
当前普通聊天在构造 ToolRuntime 时读取发起人的偏好，`text_only` 会关闭该轮的
语音许可；它不是发送时重新读取目标收件人偏好的保证。自动化等直接构造 ToolRuntime
的入口仍默认允许语音，尚未统一接入这项持久偏好检查。这是现存入口差异，不能据此
宣称所有入口已统一尊重 `text_only`。

`SPEECH_DEFAULT_MODE` 仍有配置声明和管理项，但当前显式发送链没有运行时消费；
未保存偏好时不会据它自动选择 text_only、auto 或 prefer_voice。模式名称也不代表
每次语音自动附送文字。

TTS、媒体准备和发送返回真实回执，失败由 Agent 决定后续行动；unknown 不盲目重发。
语音合成前继续检查正文脚本与语言，避免把中文交给日语 G2P。

参见 [主 Agent 合同](../architecture/main-agent-runtime.md)。文件名仅保留现有文档链接。
