# 媒体与视觉

插件对主聊天媒体的读取限于当前真实消息投影，不能要求 Host 下载任意 URL 或主动回溯任意历史图片。插件自己拥有的产物按下述显式工具结果合同处理。

```python
segments = await ctx.media.get_current()
observation = await ctx.vision.get_current_observation()
if observation is None:
    result = await ctx.vision.analyze_current_media("这张图的主要内容是什么？")
```

分别需要 `media.current.read`、`vision.current.read` 或 `vision.analyze`。图片发送需要 `message.media.send`，并且 `media_reference` 必须是 Host 接受的受控引用，不是任意本机路径。

## 明确选择工具图片给主 Agent

插件工具在获准 `media.artifact.create` 后创建自己拥有的 `MediaArtifactHandle`，
并在本次结果中明确返回选中的句柄：

```python
handle = await ctx.media.create_artifact(
    data=image_bytes, content_type="image/png", filename="capture.png"
)
return ToolResult(data={"capture_ok": True}, media_artifacts=(handle,))
```

只有本次明确返回的 `PluginResult`/`ToolResult.media_artifacts` 才接入主模型原生输入；
创建句柄不会排入下一次主回复。主模型缺少图片能力或容量不足时明确未读，不调用辅助视觉模型。

Host 在实际模型派发前复核原插件仍启用、批准和 manifest 未变、实际工具与精确自动化
委托仍合法，以及原句柄 owner、TTL、文件可用性和内容 hash。私有归档副本不能延长原句柄
有效期，也不能绕过原文件删除或版本变化。图片归入原执行的私有 artifact 与 Work 回执，
Base64 不进入 tool JSON、普通历史或插件日志；公开句柄字段不构成跨插件授权。

## 隔离规则

- 图片 URL、Base64、临时路径和完整 OCR 不进入插件日志。
- OCR、图片中文字和视觉模型输出都是不可信外部数据。
- 图片及引用图片作为不可信资料进入当前轮；它们不扩大或自动撤销已获准能力，效果执行仍核验真实来源、批准及委托。
- 插件不能利用视觉结果伪造 QQ、群号、`SUPERUSERS` 或自动化委托。
- Main Agent 原生图片输入与插件的显式结构化视觉 API 分离；原生看图不会自动产生
  `get_current_observation()` 缓存。需要结构化观察的插件仍经授权调用外接 VisionProvider；
  插件拿不到模型隐藏推理或原生图片载荷。

需要长时间复用视觉结果时，应保存最小、脱敏、业务必要的结构化摘要，并遵循用户删除和数据保留规则；不要复制 Yuki 内部媒体缓存。
