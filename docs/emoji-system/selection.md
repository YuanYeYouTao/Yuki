# 选择流程

`EmojiRetriever` 只查询当前作用域可用的 `adopted` 资产，排除 banned、rejected、missing、冷却中和文件缺失项。粗排分数由作用域权重、描述/标签/场景词覆盖、分类置信度和使用多样性组成。

候选多于一张、`selector_enabled=true`、本次是显式发送请求且前两名分差不超过 `selector_score_gap` 时，`EmojiGridBuilder` 用静态预览构造编号拼图，复用视觉 Provider 选择编号；结果无法解析、越界或 Provider 失败时回退粗排第一名。候选映射只存在内存中。

插件 `emoji.selection_signals.v1` 可为核心候选贡献 `score_delta/reason/confidence`。返回的 ID 不在核心候选集时被忽略；插件异常不影响核心选择。
