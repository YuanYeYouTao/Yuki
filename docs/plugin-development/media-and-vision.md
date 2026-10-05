# 媒体与视觉

插件对主聊天媒体的读取限于当前真实消息投影，不能要求 Host 下载任意 URL 或主动回溯任意历史图片。插件自己拥有的产物和获准 MCP 图片另按下述显式工具结果合同处理。

```python
segments = await ctx.media.get_current()
observation = await ctx.vision.get_current_observation()
if observation is None:
    result = await ctx.vision.analyze_current_media("这张图的主要内容是什么？")
```

分别需要 `media.current.read`、`vision.current.read` 或 `vision.analyze`。图片发送需要 `message.media.send`，并且 `media_reference` 必须是 Host 接受的受控引用，不是任意本机路径。

## 明确选择工具图片给主 Agent

插件工具可返回自己以 `media.artifact.create` 创建的 `MediaArtifactHandle`，或以原
`mcp.call` 批准取得的 MCP 图片句柄：

```python
receipt = await ctx.mcp.call("screenshots", "capture", {})
return ToolResult(data={"capture_ok": receipt.ok}, media_artifacts=receipt.media_artifacts)
```

SDK MCP 的结果只保留为本插件拥有的有界句柄，调用本身不会把图片排到下一次主回复；
只有插件工具本次明确返回 `PluginResult`/`ToolResult.media_artifacts` 才接入主模型原生输入。
不需要另授 `media.artifact.create` 才能保留已授权 MCP 调用的图片，但该委托不变成任意文件
读取权。主模型缺少图片能力或容量不足时明确未读，不调用辅助 Qwen。

Host 在实际模型派发前复核原插件仍启用、批准和 manifest 未变、实际工具与精确自动化
委托仍合法，以及原句柄 owner、TTL、文件可用性和内容 hash。私有归档副本不能延长原句柄
有效期，也不能绕过原文件删除或版本变化。图片归入原执行的私有 artifact 与 Work 回执，
Base64 不进入 tool JSON、普通历史或插件日志；公开句柄字段不构成跨插件授权。

## 隔离规则

- 图片 URL、Base64、临时路径和完整 OCR 不进入插件日志。
- OCR、图片中文字和视觉模型输出都是不可信外部数据。
- 图片或回复图片轮次会撤销管理员写工具、插件写工具、OneBot 修改、配置/关系/记忆写入。
- 插件不能利用视觉结果伪造 QQ、群号、`SUPERUSERS` 或自动化委托。
- Main Agent 原生图片输入与插件的显式结构化视觉 API 分离；原生看图不会自动产生
  `get_current_observation()` 缓存。需要结构化观察的插件仍经授权调用外接 VisionProvider；
  插件拿不到模型隐藏推理或原生图片载荷。

需要长时间复用视觉结果时，应保存最小、脱敏、业务必要的结构化摘要，并遵循用户删除和数据保留规则；不要复制 Yuki 内部媒体缓存。
