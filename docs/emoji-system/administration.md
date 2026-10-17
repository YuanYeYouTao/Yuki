# 管理

以下 QQ 修改命令只接受当前 OneBot 事件中真实发送者属于 `SUPERUSERS` 的调用。
表情管理继续通过 `/ai` 调用原领域服务；资源上传等管理界面能力本期不另设入口。

```text
/ai emoji list [状态]
/ai emoji show <ID>
/ai emoji adopt|unadopt|reject|ban|unban|reanalyze <ID>
/ai emoji pin <ID> on|off
/ai emoji group enable|disable
/ai emoji import
/ai emoji stats|cleanup|doctor
```

ID 可以使用无歧义前缀。`import` 只接受当前或被回复消息里的图片，不接受任意 URL。自然语言管理员动作 `emoji.*` 调用同一个 `EmojiAdminService`，不会复制业务逻辑。`doctor` 检查原图和预览，原图缺失标记 `missing`，预览缺失加入重建任务。
