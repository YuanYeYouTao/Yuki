# Plugin API 3.0 历史迁移

> 本文记录 3.0 的接口变化；当前 Host 只接受 3.3，完成下述步骤后继续 [API 3.1 历史迁移](api-3.1-migration.md) 和 [API 3.2 历史迁移](api-3.2-migration.md) 和 [API 3.3 迁移](api-3.3-migration.md)。

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

2026-10-05 的兼容扩展新增 `PluginResult.media_artifacts`，`ToolResult` 继承该字段；默认
空元组不改变既有结果 JSON。需要把本次选图交给主 Agent 的工具，显式返回本插件拥有的
Host 句柄，不建立共享或“下一轮回复”图片队列。当前用法见
[媒体与视觉](media-and-vision.md)。旧 `vision.analyze_current_media` 是另有批准的结构化
辅助接口，不能拿它当作主 Agent 原生看图的隐式兜底。
