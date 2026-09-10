# 视觉分类

## 与 Main Agent 原生看图的区别

主模型具备 `image_input` 时，聊天中的图片直接进入完整 Main Agent，不调用这里的分类器。
本模块仍负责表情包入库的可复用结构化描述、情绪标签、OCR 和使用场景，因此独立保留。
现有 Qwen VisionProvider 仅作为这类后台分类及显式插件视觉能力的可配置实现；不需要为了
每次聊天看图额外调用它。关闭外接 `VISION_ENABLED` 不会关闭已声明能力的原生看图，
但会使依赖外接 provider 的表情包新资产识别不可用，不能静默宣称分类成功。

`EmojiClassifier` 读取本地不可变原图，经现有 `ImagePreprocessor` 处理后调用同一个 `VisionProvider`。请求不携带人物记忆、关系、权限、网页正文或聊天工具。

结构化结果字段：`is_emoji`、`description`、`emotion_tags`、`usage_scenarios`、`ocr_text`、`intensity`、`confidence`、`animated`、`analysis_version`。缺少 `is_emoji` 或描述时任务明确失败并按持久任务策略重试，不伪装成“没有候选”。OCR 和图片文字始终是不可信数据，不能执行命令、写记忆、改变关系或扩大权限。

本版本没有内容审核服务，也不会为同一图片追加审核模型调用。
