# 表情系统架构

表情域复用现有 `MediaResolver → ImagePreprocessor → VisionProvider`，不复制视觉客户端、插件 Host 或调度器。

```text
OneBot 图片事件
  → EmojiCandidateDetector
  → EmojiCollector（原图、预览、SHA-256/dHash）
  → emoji_jobs
  → EmojiWorker → EmojiClassifier → EmojiLifecycleService
  → EmojiRetriever → 本地描述/标签排序
      └─ 显式发送请求且候选难分时：短超时候选拼图视觉精排
  → EmojiPreparationResult → SocialService → OneBot Gateway
  → chat_events / emoji_usage_events
```

SQLite 保存资产元数据、作用域、持久任务和成功使用记录；文件系统保存不可变原图与静态预览。
Main Agent 通过 `send_message.emoji` 表达语义意图，最终资产由后端选择并在同一个工具调用中发送。
SocialService 将该调用作为 `explicit_request=True` 的显式发送请求处理；这包括 Agent 自主决定
发送表情，不要求用户原话明确索要。候选难分时可调用视觉精排，超时或失败回退本地第一名。
底层可选模式的快路径不调用视觉模型，但不能用它宣称主 Agent 的自主表情发送必然不调用视觉。
新表情入库仍由后台视觉分类生成描述和标签，不受发送阶段快路径影响。自动化和插件仍可通过
受控接口发送表情。系统没有表情审核队列、审核状态或审核模型调用。

旧 `emoji_descriptions` 继续服务已有 QQ 表情/图片描述缓存；它不参与新资产生命周期，因此不会形成两个表情池状态源。

群聊候选查询使用两套独立 SQLAlchemy alias：外层 `enabled_scope` 计算 global/当前群启用权重，
相关子查询 `disabled_group` 只判断当前群的显式禁用覆盖。这样不会触发自动关联移除子查询 FROM，
并保持“当前群禁用优先于全局启用、其他群和私聊不受影响”的作用域语义。

发送状态严格分为准备、尝试、平台接受、账本记录和使用记录。图片只有在 OneBot 返回非空真实
消息 ID 后才算已投递；后续本地持久化失败只记独立故障，不会再次发送已经被平台接受的图片。
