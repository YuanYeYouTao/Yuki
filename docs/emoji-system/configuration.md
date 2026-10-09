# 配置

配置按 `user > group > global > .env > 代码默认值` 解析。核心键：

| 组 | 键 |
|---|---|
| 开关/收集 | `emoji.enabled`、`collection_enabled`、`collection_mode`、`collect_private`、`collect_group` |
| 采用/容量 | `auto_adopt_enabled`、`auto_adopt_min_confidence`、`pool_capacity`、`replacement_mode` |
| 选择/冷却 | `selector_enabled`、`selector_candidate_count`、`selector_score_gap`、`selector_timeout_seconds`、`same_emoji_cooldown_seconds`、`scope_repeat_cooldown_seconds` |
| 去重/维护 | `near_duplicate_enabled`、`near_duplicate_distance`、`cache_retention_days`、`analysis_version` |
| Worker | `worker_batch_size`、`worker_poll_seconds`、`worker_lease_seconds`、`worker_max_attempts`、`worker_retry_delay_seconds` |

`selector_candidate_count` 默认是 3。日常 `optional` 表情直接采用本地描述和标签评分第一名；显式发送请求且前两名分差不超过 `selector_score_gap` 时才调用视觉精选；Main Agent 的 `send_message.emoji` 始终属于显式发送请求，包括 Agent 自主选择发送，并受 `selector_timeout_seconds` 短超时约束。视觉调用超时或失败会立即回退本地第一名。

`pool_capacity` 未设置表示无限；两个 cooldown 都允许 `0` 表示关闭。`storage_root` 和预览尺寸是启动配置。不存在 `emoji.review_enabled`。

真实作用域、资产状态与投递回执约束实际发送；准备失败作为工具结果返回，后端不自动追加失败文字或重发。
