# Plugin API 4.0 历史迁移

> 本页记录 4.0 的身份迁移。当前 Host 精确加载 5.0；完成本页后继续 [API 5.0 迁移](api-5.0-migration.md)。以下表情选择信号属于已经退出的历史接口。

4.0 将通用服务的目标和所有权统一为内部编号。Host 精确匹配 `plugin_api = "4.0"`，旧 3.x 插件须修改、重新发现并由管理员审阅实际 manifest；不以同名字段静默接受旧的 QQ 账号参数。

- `messages.send_private`、People、Memory 的 `user_id`/person subject 使用内部 Person ID；`messages.send_group`、Group、group config/Memory 使用内部 Space ID。使用 `get_current()` 的 `person_id`/`space_id`，不要把 `sender_user_id`、`group_id` 或平台消息号当业务编号。
- `CurrentMessage.platform` 与 `mentioned_person_ids` 由 Host 沿真实入站保存；既有 `message_id`、`sender_user_id`、`group_id`、`mentioned_user_ids` 继续是传输来源，不能用于重建已存在的内部事件或所有权。
- 通知 target 的 `target_id` 同样是内部 Person/Space，`conversation_id` 选择已有的实际入口。当前场景使用原 Conversation；一个 owner 有多个私聊 Bot 或群 topic 时显式选择，不能猜当前活跃路由。授权与撤销分别作用于该 Conversation。
- 通用发消息沿统一 Social 意图和原调用回执；`send_image` 使用本插件拥有的 media artifact handle。QQ 音乐卡等扩展仍在独立授权的 `onebot` 接口，账号参数只在该明确 QQ 边界解析。
- 独立 Agent session 的主体保存 canonical Person/Space；没有真人的 SELF/插件后台不会借用收件人为 Person。`current_user` profile 需要真实 Person，群和插件范围沿现有 scope 及批准权限处理。
- Emoji selection signal 使用 `space_id`，不把内部 Space 伪装成 QQ `group_id`。配置 user/group 的 scope ID 也分别为内部 Person/Space。

数据库升级保留原 session、通知事件、outbox/job、操作回执和预算。已有平台回执只作历史读取；新调用不按旧裸号寻找 owner。插件自己的旧配置与待处理记录仍须按其真实原绑定迁移，不能清空或用今日路由猜过去的主体。API 版本更新不是对新目标的额外授权。

先完成此前 [3.5 退役迁移](api-3.5-migration.md) 中真实事件及权限的清理，再适配本页的内部编号合同。
