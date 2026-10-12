# 插件管理定时安排

定时安排保存时间、目标与完整要求；到点激活同一个 Main Agent，使用共同工具和执行后端。
插件工具统一用 `register_tool(ToolRegistration(...))` 注册，不再注册独立 Automation Action。

运行时 `ctx.automation` 提供 `list_current_owner`、`create_task`、`update_task`、`pause`、
`resume` 和 `cancel`；读需要 `automation.read`，创建与管理需要 `automation.manage_self`。
`create_task` 和 `update_task` 直接提交共同安排定义：name、goal、trigger、timezone、
constraints、delivery.target。目标值为 auto、self_private、current_group 或 none。

Host 使用当前绑定的原 ToolActor 和真实会话；SELF 保自身主体，不要求真人 Inbound，
也不借最近发言者的权限。不同到期发生保存不同的 run，重扫、恢复和迟到回执沿原执行 ID
处理。暂停将来的安排与结束今天的 Work 是不同操作。

工具内部使用的 Facade 仍需原 Manifest 批准和实际权限；定时来源不另有媒体、音乐或工具
许可菜单。未知发送及已确认效果保原 Social 回执，不重发。旧定义、版本 hash 和已发生
回执是原事实，不由新定义或模型正文重算。
