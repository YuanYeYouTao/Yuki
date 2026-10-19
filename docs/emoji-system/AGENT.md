# 表情库操作说明

这是 Yuki 自己的共享表情库。用普通终端收藏、整理、修改和删除即可；原图和 JSON 是资料源，`search.sqlite3` 是可重建的搜索缓存。

## 收藏与编目

- 原图放在 `/workspace/stickers/<platform>/files/`，资料在同平台的 `index/`。已有 `qq`、`telegram` 目录；其他平台也沿相同结构。
- 文件名可以用自然名称，保留原格式。资产 ID 按平台和内容生成，同平台同字节复用同一资产，不必再存一份来改名字。
- 收藏后给它补上贴近情绪、语气和用途的短标签。长描述与 OCR 可以另写，文件名不会自动成为搜索标签。
- 从聊天附件保存或从 `/inputs/media-cache` 复制都可以。保存完成后运行 `update` 编目；普通复制不会自动补标签，人工编目也不会自动调用 Qwen。

```sh
python /opt/yuki-runtime/sticker_index.py --workspace /workspace \
  update 'stickers/qq/files/开心.png' --tag 开心 --tag 赞同
```

把平台、路径和标签换成实际内容。`--tag` 可重复；给已有资料传标签时会替换原标签列表，需要保留的也一起传入。

## 修改与整理

- 可以直接编辑 `index/<asset-id>.json` 的 `tags`、`description`、`ocr_text`，再对该 JSON 运行 `update`。
- ID、hash、格式和路径等文件事实交给 helper 计算。改名或移动原图后运行 `rebuild`，让资料和真实文件对齐。
- 跨平台复制时，目标平台也要编目；希望保留文字资料时同时复制对应 JSON，再运行 `update` 或 `rebuild`。
- 只有文件名或标签还不代表看过画面，需要辨认时使用现有看图工具。

```sh
python /opt/yuki-runtime/sticker_index.py --workspace /workspace \
  update 'stickers/qq/index/<asset-id>.json'
python /opt/yuki-runtime/sticker_index.py --workspace /workspace \
  rebuild --platform qq
python /opt/yuki-runtime/sticker_index.py --workspace /workspace \
  search --platform qq '捂嘴偷笑'
```

## 删除

删除整个表情时，先从 JSON 确认实际原图路径和资产 ID，再删除原图及对应 JSON，最后 `rebuild`。同内容副本仍在时，重建也会再次收录。只删 JSON 而保留原图，后续重建会重新编目；只删原图会留下孤立 JSON。已经发布的快照与历史发送回执沿自己的生命周期保留。

## 日常配图

用 `send_message` 的 `emoji.goal` 描述想表达的情绪或含义；有文字就先发文字，选图由后台处理。也可以只传 `emoji` 发纯表情。表情承接语气，无需逐项念出图中文字或标签。
