# Plugin API 3.0 迁移

Plugin API 3.0 删除了把媒体效果排入“下一条自动回复”的隐式状态：

- 删除 `EmojiFacade.queue_reply_effect` 和权限 `emoji.send`；
- 删除 `SpeechFacade.queue_reply_voice` 和权限 `speech.reply_effect`；
- 插件需要发送内容时，直接使用消息、语音或媒体 Facade，并以真实回执判断结果；
- Host 不再保存、恢复或解释插件产生的 reply state。

这是主版本升级。把无需上述旧接口的插件 manifest 改为：

```toml
plugin_api = "3.0"
```

仍声明 2.x 的插件会在导入插件代码前明确拒绝，不提供运行时兼容 shim。
