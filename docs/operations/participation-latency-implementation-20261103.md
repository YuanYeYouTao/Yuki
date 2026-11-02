# 语义参与状态与消息延迟优化实施记录

日期：2026-11-03，UTC+08:00。依据[修订任务书](../architecture/Yuki-语义参与状态与消息延迟优化任务书-2026-11-03.md)实施，主Agent统筹，三个Terra max执行并交叉复核。

状态：实施中。当前授权包括两仓实现、合流、E2E与性能验证，以及验证完成后复审并合并 Yuki 相关仓库所有未合并 PR、按既有流程上线 Bot。生产配置与部署尚未修改。

## 基线

- Host：`b81c20a55d3a714168e7d8306ccfbf74dddb3eb4`。
- 参与库：`a035009b5cb64bef665eb3a8b85ed9ea7f3ecda0`。
- 两仓隔离分支：`codex/participation-latency-20261103`，原文档工作目录中的用户修改未覆盖。
- 私有排查快照及完整本机证据：`D:/Code/Reports/Yuki/participation-latency-implementation-20261103`；原报告仍按其固定SHA-256引用。

## 先复现再实施

失败场景按任务书E01–E14定义。基线已固定为独立源码归档，后续工作树修改不覆盖旧版；隔离Provider/网关产生的回执明确标为模拟，不代表真实QQ/Provider验收。

隔离验证使用 Linux Python3.12.13、SQLite3.53.1、Pydantic2.13.4 与 pydantic_core2.46.4。2026-11-02 21:33:55 UTC 的生产只读采样实际为 Python3.12.16、SQLite3.46.1，后两项 Pydantic 版本相同；两者不是相同运行时，隔离性能数字不代表生产延迟。早期源码路径验证不作为实际锁定包安装验收；最终镜像的版本、迁移和运行结果另行记录。源码、检查、E2E/性能、提交、合并与上线按实际结果分别记录。

## 健康与诊断兼容

公开 `/healthz` 删除无仓内消费者的 `memory_embedding_coverage`、`memory_embedding_pending_jobs`、`memory_embedding_failed_jobs`，不再为一次健康读取加载并逐条 hash 全部 Memory。enabled/configured 读取原运行时与实际 Provider 装配；精确状态仍经原受权 `/ai memory embedding status` 按需查询。仓内 Desktop/Monitor/Connector 消费者已核对；外部自行读取已删除字段的调用方须改为按需详细查询。

语义观察诊断使用显式字段图，在原容量准入后才复制独立树；不执行任意 Pydantic serializer 或 computed field。记录入队后仍核原 privacy generation，诊断丢弃与失败不改变业务回执。
