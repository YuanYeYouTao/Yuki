# 存储与清理

```text
data/emoji/
├── original/<sha-prefix>/<sha256>.<真实扩展名>
└── preview/<sha-prefix>/<sha256>.webp
```

原图以内容 SHA-256 命名并原子写入；PNG/JPEG/GIF/WebP 的扩展名由 Pillow 解码结果决定，不信任 URL 后缀。动画 GIF/WebP 原文件不转码，预览只使用第一帧。dHash 仅用于近似关系提示，近似资产可以共存。

周期维护和 `/ai emoji cleanup` 仅删除超过 `cache_retention_days` 的非 adopted、非 pinned 候选及残留临时文件。删除顺序先删数据库可删记录，再清理对应文件；正式池资产不受缓存清理影响。账本只保存安全摘要、MIME 和内部表情 ID，不保存 Base64。

任务领取先用只读查询发现有界候选，空轮询不保留 SQLite writer；过期 processing
任务可由新 attempt 接管。分类结果写入及后续自动采用核验原 job 的 processing 状态、
claimed_by 和 claimed_until，迟到 attempt 不得覆盖或结束新 claim。替换选择在事务外完成；
采用前在短写事务重新核验 claim、当前状态、容量及替换对象仍启用且未固定，旧作用域移除
与新作用域采用一起提交。来源 canonical owner 解析在候选 upsert 首次写入之前完成。
