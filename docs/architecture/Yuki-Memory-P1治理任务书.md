# Memory P1 历史本地验收记录

基线 main/624fbd4，版本 3.8.1，schema 0051，分支 `codex/memory-p1-governance`。
原记录为本地完成、待 PR / 上线；不推定后续线上状态。已退役价值门槛、归因和意图排序
施工规范删除，以下真实检查结果按当时口径保留。当前合同见 [Memory](memory-v2.md)。

## 最终验收记录

- 静态与确定性测试通过：Ruff format/check、mypy、800/800 pytest、84 项示例与 GitHub
  插件契约测试；测试收集量保持在预算上限 800。
- Memory quality 数据集为 19/19 case、38/38 query；发布检查的版本、Alembic 0051、
  数据集、质量/性能基线、合同、迁移和 4 个 Plugin API 2.0 manifest 必选门全部通过。
- fresh -> 0051 与 0050 -> 0051、schema/FTS/trigger/FK、Compose production/dev 配置、
  本地镜像的 source-free 完整 release smoke 均通过。
- 有限真实模型验证严格用满 24 次请求（重试计入）：8 次批提取达到应保留内容召回
  100%、低价值排除 100%；reflection 的初始 4 次及有界 4 次复测覆盖有价值 episode、
  重复/noop 和琐碎/noop；两个主动读取场景共使用 8 次 Main Agent 请求，最终实际调用
  `get_person_memories` 与 `get_group_memories`，均注入合成事实并产生非空回复。
- 主动读取临时验证脚本最后的聚合布尔值曾因读取了与 AgentToolService 不同的
  `MemoryContextService.metrics` 实例而显示 false；工具调用、注入日志与回复结果本身
  均成功。该项记录为测试夹具的观测实例限制，没有追加超过 24 次预算的调用。
- 全部模型验证使用合成数据；未修改生产记忆、生产数据库、人格、`.env`、会话、路由或
  Rollup generation。当前记录不代表已经部署生产。
