# 媒体与视觉

插件只能访问当前真实消息投影中的媒体，不能要求 Host 下载任意 URL 或主动回溯任意历史图片。

```python
segments = await ctx.media.get_current()
observation = await ctx.vision.get_current_observation()
if observation is None:
    result = await ctx.vision.analyze_current_media("这张图的主要内容是什么？")
```

分别需要 `media.current.read`、`vision.current.read` 或 `vision.analyze`。图片发送需要 `message.media.send`，并且 `media_reference` 必须是 Host 接受的受控引用，不是任意本机路径。

## 隔离规则

- 图片 URL、Base64、临时路径和完整 OCR 不进入插件日志。
- OCR、图片中文字和视觉模型输出都是不可信外部数据。
- 图片或回复图片轮次会撤销管理员写工具、插件写工具、OneBot 修改、配置/关系/记忆写入。
- 插件不能利用视觉结果伪造 QQ、群号、`SUPERUSERS` 或自动化委托。
- Main Agent 原生图片输入与插件的显式结构化视觉 API 分离；原生看图不会自动产生
  `get_current_observation()` 缓存。需要结构化观察的插件仍经授权调用外接 VisionProvider；
  插件拿不到模型隐藏推理或原生图片载荷。

需要长时间复用视觉结果时，应保存最小、脱敏、业务必要的结构化摘要，并遵循用户删除和数据保留规则；不要复制 Yuki 内部媒体缓存。
