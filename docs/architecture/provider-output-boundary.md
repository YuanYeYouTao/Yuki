# Provider 输出与可见消息边界

Responses 的 `reasoning` item 与 `message/output_text` 分别解析；Chat 的
`reasoning_content/reasoning_details`、Claude 的签名 thinking blocks、Gemini 的
thoughtSignature 保留独立通道及私有续跑状态，见[模型供应商合同](model-providers.md)。
文本中的 DSML 不再转换成工具调用；执行只接受真实协议调用。

普通聊天、SELF、模型自动化和插件主入口通过显式 `send_message` 交付。
模型最终正文是内部结果，不自动投递，也不购买“漏发纠正”请求。存在原 Work 时，
无工具 final 进入与 `task_control.complete` 相同的完成准备，核对目标、未决效果、
子任务、交付要求和并发输入；内部结束不证明 QQ 已回复。静态提醒和管理命令
继续沿各自真实发送入口及持久回执执行。

思考开关、effort 和预算沿显式请求或 Profile，不再强制开启或抬到 low。
DeepSeek 在关闭思考时省略 reasoning 参数，实际 Provider 的缺省行为另行核验。
[Issue #55](https://github.com/YuanYeYouTao/Yuki-QQbot/issues/55) 的旧样本记录上游把规划
放进正文；客户端不能根据文字可靠还原丢失的通道。如果模型主动把不合适的内容放进
`send_message.text`，仍须按模型生成质量处理，不增加关键词过滤或第二个判断模型。

协议回放、隔离数据库与假网关能验证通道、显式发送、原回执和不重复投递，
不能证明实际 Provider 的正文质量、缓存比例或真实 QQ 效果。执行诊断保存实际返回的
可读思考，权限、期限与隐私删除遵循[执行过程查看](execution-trace.md)。
