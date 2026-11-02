# 语义参与状态与消息延迟优化实施记录

日期：2026-11-03，UTC+08:00。依据[修订任务书](../architecture/Yuki-语义参与状态与消息延迟优化任务书-2026-11-03.md)实施，主Agent统筹，三个Terra max执行并交叉复核。

状态：第一轮已合并并上线 Bot。本次记录修订时，公开健康路径的 D10 补修正在隔离工作树验证，尚未合并或上线；补修的镜像、部署及生产验证待完成后另记。

第一轮 Host PR #562 已合并至 main `fa01854d0c6832dc98cc8673d666386c97ce94b6`，参与库使用 `a6a13d4f04ecc36da26adb5f364abdb4f032fc1e`。已部署 Bot 代码源 `004ae36dbe26046d3946b78934c4766333831bcf`、镜像 SHA-256 前缀 `b3cec`，容器 `e9ffc6d32de9786b052cc2a14130ec7c1726e717492d23a1e3fee4a605f96f1a`，数据库迁移为 `0156`；环境、挂载、网络、角色、模型与其他容器保持原样。切换核验保留 1280 条 Work、24858 条确认回执及原预算事实。Desktop 相关 PR 已合并，本轮没有发布新版 Desktop。

## 基线

- Host：`b81c20a55d3a714168e7d8306ccfbf74dddb3eb4`。
- 参与库：`a035009b5cb64bef665eb3a8b85ed9ea7f3ecda0`。
- 两仓隔离分支：`codex/participation-latency-20261103`，原文档工作目录中的用户修改未覆盖。
- 私有排查快照及完整本机证据：`D:/Code/Reports/Yuki/participation-latency-implementation-20261103`；原报告仍按其固定SHA-256引用。

## 先复现再实施

失败场景按任务书E01–E14定义。基线已固定为独立源码归档，后续工作树修改不覆盖旧版；隔离Provider/网关产生的回执明确标为模拟，不代表真实QQ/Provider验收。

隔离验证使用 Linux Python3.12.13、SQLite3.53.1、Pydantic2.13.4 与 pydantic_core2.46.4。2026-11-02 21:33:55 UTC 的生产只读采样实际为 Python3.12.16、SQLite3.46.1，后两项 Pydantic 版本相同；两者不是相同运行时，隔离性能数字不代表生产延迟。早期源码路径验证不作为实际锁定包安装验收；第一轮生产镜像与迁移见上，D10 补修的最终镜像和运行结果仍待验证。源码、检查、E2E/性能、提交、合并与上线按实际结果分别记录。

## 健康与诊断兼容

公开 `/healthz` 删除无仓内消费者的 `memory_embedding_coverage`、`memory_embedding_pending_jobs`、`memory_embedding_failed_jobs`，不再为一次健康读取加载并逐条 hash 全部 Memory。enabled/configured 读取原运行时与实际 Provider 装配；精确状态仍经原受权 `/ai memory embedding status` 按需查询。仓内 Desktop/Monitor/Connector 消费者已核对；外部自行读取已删除字段的调用方须改为按需详细查询。

第一轮部署后 `/healthz` 仍有秒级样本，D10 补修正在从公开响应移出无仓内消费者的历史聚合，保留数据库读写、网关、Worker 与配置的真实基础状态。Memory 自省和一致性原有受权详情继续按需读取；Rebuild、Dream 的受权列表和单次运行状态不等于原公开全局汇总。旧公开字段的外部消费者未知，需按实际接口调整；补修的生产耗时与自然流量效果尚未验证。

语义观察诊断使用显式字段图，在原容量准入后才复制独立树；不执行任意 Pydantic serializer 或 computed field。记录入队后仍核原 privacy generation，诊断丢弃与失败不改变业务回执。
