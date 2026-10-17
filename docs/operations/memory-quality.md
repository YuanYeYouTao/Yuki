# Memory 来源审计与维护

以下命令必须指向明确的数据库。来源 audit 只读，不自动修复历史缺链，也不将同 key 独立事实当成冲突。

```bash
yuki memory audit --database-url <database-url>
yuki memory stats --database-url <database-url> --hours 24
yuki memory hygiene scan --database-url <database-url>
yuki memory hygiene apply <fingerprint> --database-url <database-url>
yuki memory hygiene rebuild-fts <fingerprint> --database-url <database-url>
```

apply 和全量 FTS 重建是明确的维护动作；先查 scan，再核对原指纹和备份。不要通过清库、重置预算或重新调用模型掩盖已提交效果和历史缺链。

自省和 Dream 的请求登记发生在实际 HTTP 派发前。payload 构造失败不产生请求计数，HTTP 重试逐次登记，累计请求账本不因重启重置。
自省只从本批新事件和工具回执创建 SELF 资料，不加载全量旧事实或裁决 SELF 候选；
Dream 沿既有正式记忆、证据和分区做整理。普通提取不再生产 SELF 候选，历史行和原检查点不自动清空、解隔离或重跑。
额度只约束真实新派发：自省已有 ReflectionCheckpoint、Dream 已有来源指纹匹配的 ready preview 时，
直接沿原 run 和原 mutation/operation 回执完成纯数据库收尾，满额不要求重新调用模型。
未保存的输出不能由 usage 重建，来源失效或提交未决仍按真实事实处理。
新自省检查点不含旧版必填的 payload/fact_map/candidate_map；镜像回退前核对未完成的原已付输出及可用 reader，
不恢复旧数据库覆盖新事实，也不重新购买输出。显式隐私擦除会按实际来源清退敏感 checkpoint JSON，
保原 run、请求账本、已提交结果和预算；迟到返回不能将已擦正文写回。
未选择事实不自动写处理 checkpoint，批准的 Rebuild 子集可提交，未决项保持 REVIEW。

Dream 模型以本簇的局部事实别名、正文、scope/visibility/kind、事实元数据与证据摘录作判断。
模型请求移除 `subject_user_id`、`group_id`、`visibility_user_id`、`visibility_group_id` 四个外部定位字段；
原 DreamInput、来源指纹与持久 preview 的历史 codec 保留，不因这次模型输入调整重算旧 preview 指纹或重写原来源证明。

维护只按事实的 valid_until 处理到期；不按来源、置信度、年龄或 scope 容量隐式遗忘。归因、强化、Activation、自动注入、synthetic quality/baseline/release-check 均已退休。统计或健康检查不证明模型实际使用或自然聊天质量。

普通提取任务的失败沿原 job 重排，保留累计 attempts 和错误，不按固定三次转终态。
无事实结果正常完成；历史失败记录不因本次策略调整改写。
完成阶段的可信 SQLite BUSY 只重备原数据库收尾，不重新执行刚完成的提取。
普通提取未新增通用的跨进程模型输出检查点，不能把该收尾修复当作所有已付输出都可恢复。

永久证据抽样删除服务及其新任务、配置和装配已退役；旧 compaction 表与历史回执继续可读。
模型输入按预算抽取摘录不删除永久来源。显式隐私删除、失效事实的状态更新与未引用临时回执清理各沿原权限和引用规则处理。

查来源、facts 与后台任务时使用原内部 Person、Space、Conversation、event/run ID；
App/TG 无 QQ 投影不构成缺失主体。新查询每次执行当前 SQL 授权和事实筛选，
纯 query embedding cache 只复用向量。查询请求、向量缓存命中、失败和实际 usage 分别看待，
缺 Token 字段不算零，也不能当成 Main 模型缓存命中率。

Embedding 的可恢复失败与 Rebuild 的未提交失败按原任务、item 或 proposal 重新领取，
沿配置的重试等待间隔保留累计尝试，不按次数转 FAILED，也不因提交准备完成就第一次封口。
Rebuild 已提交事实与 proposal 回执共同核对，确认已提交不再次写入；历史失败状态保留。

当前合同见 [Memory](../architecture/memory-v2.md)与[来源审计](../architecture/memory-v2-quality.md)。
