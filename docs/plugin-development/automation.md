# 插件管理定时安排

定时安排保存结构化时间与完整要求；到点激活同一个 Main Agent，使用共同工具和执行后端。
插件工具统一使用 `register_tool(ToolRegistration(...))` 注册，没有独立 Automation Action。

Plugin API 9.0 的 `ctx.automation` 提供 `list_current_owner`、`create_task`、`update_task`、
`pause`、`resume` 和 `cancel`。读需要 `automation.read`，创建与管理需要 `automation.manage_self`。

```python
task = {"goal": "按完整要求完成工作，需要沟通时使用正常 send。", "trigger": {"type": "after", "seconds": 300}}
created = await ctx.automation.create_task(task, request_id="reminder-1", max_runs=1)
task_id = created.data["automation"]["task_id"]
await ctx.automation.update_task(task_id, task)  # 保留次数上限
await ctx.automation.update_task(task_id, task, max_runs=None)  # 解除次数限制
```

task 必填 `goal/trigger`，可选公开显示 `name`。完整收件人、渠道和限制写入 goal，不再传
`version/constraints/delivery/task.timezone`。创建时 name 缺省或 null 使用中性名称；更新时
name 缺省保留，null 清除自定义标签。全局目录不会从私有 goal 生成名称。

trigger 支持 after/interval 的 seconds、once 的含 UTC offset 的 local_datetime，以及
daily/weekly 的 hour/minute、weekdays 和唯一 timezone；墙时区缺省在创建时固化为共同默认。
只修改目标、名称或次数不会重新倒计时；真正修改 after/interval 的时间参数从该次修改接纳时刻计算。

max_runs 是累计发生次数上限。创建缺省或 null 不限；更新缺省保留、正整数修改、null 解除。
更新不清零原计数，不取消在途发生，也不复活 completed/cancelled。
create 的稳定 request_id 表示原操作，重试复用，另一项任务使用另一 ID；原调用键和参数冲突仍报错。
结果的 `data.automation` 返回整数型字符串 task_id、name、status、next_run_at、last_run_at、run_count、max_runs。
list_current_owner 只列原主体的 active/paused 安排。

Host 使用当前绑定的原 ToolActor 和真实会话；SELF 保自身主体，不制造真人 Inbound，
也不借最近发言者权限。执行场景保留原 Conversation、Space、Presence；实际发送沿正常 send 的
真实 target/binding。不同到期发生保存不同 run，重扫、恢复和迟到回执沿原执行 ID 处理。
暂停未来安排与结束今天的 Work 是不同操作。

Facade 仍需原 Manifest 批准和实际权限；定时来源不另有工具许可菜单。未知发送及已确认效果保原
Social 回执，不重发。原版本/hash 和已发生事实不由新定义重算。平台连接只在实际投递处核验；
内部读取、计算和恢复不因暂时离线或同 Presence 重连终止。
