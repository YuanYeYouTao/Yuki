# Plugin API 6.0 迁移

> 本页保留 6.0 的历史变更；当前合同见 [Plugin API 8.0](api-8.0-migration.md)。

Host 只加载精确声明 `plugin_api = "6.0"` 的插件。产品版本与 Plugin API 分别演进；本次升级不提供 5.0 在线兼容对象或旧工具执行器。

6.0 将当前消息提及统一为 `CurrentMessage.mentions` 原快照，每项保留 `kind`、`display_name`、原文位置、可证明的内部 Account／Assignment／Person／Presence 和原传输出处。它替代 `mentioned_user_ids`／`mentioned_person_ids`；需要此能力时声明 `message.current.mentions.v2`。未知账号或外部 Bot 可以没有 Person，插件不能从原生数字猜人物，也不能把普通 `@文字` 造为收到的原生提及。

```python
ctx.features.require("message.current.mentions.v2")
current = await ctx.messages.get_current()
people = tuple(dict.fromkeys(
    target
    for mention in current.mentions
    if mention.get("kind") == "member"
    and isinstance(target := mention.get("person_id"), str)
))
```

只有业务确实需要人物时才选择有 Person 的快照；精确原生账号定位使用 Host 提供的内部 Account 引用。显示名不是权限或所有权。普通人物／空间／会话参数继续使用内部 ID，人物合并不改写插件任意 JSON 内的业务值。

修改真实调用与 manifest 后重新发现、审阅并批准该 manifest；权限集合保持插件实际所需用途。后台通知沿共同发送准备，将文字中的自然 `@名字` 还原为当前目标平台的原生目标，歧义由明确内部引用解决；它不改变原 publication／job 的 source Person 或历史投递回执。

新 Host 的身份库使用 Account 与唯一 AccountAssignment。插件继续经 SDK 操作，不读取旧 `identity_bindings`／`presences` 表，也不自行查询底层新表。USER 配置沿当前人物资料读取，单字段保存使用真实字段 metadata 和原行 CAS。原执行、预算、grant、job、publication 与回执保原来源，不借当前 root 或今日账号换人。

5.0 表情接口退役记录保留在 [历史迁移](api-5.0-migration.md)；旧表情 facade 不在 6.0 中恢复。
