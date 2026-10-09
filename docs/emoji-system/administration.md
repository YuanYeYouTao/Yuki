# 管理

以下 QQ 修改命令只接受当前 OneBot 事件中真实发送者属于 `SUPERUSERS` 的调用。
Control/WebUI 修改由可信 operator 的 `control.emoji.mutate` 授权，仍经同一业务服务执行。

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
