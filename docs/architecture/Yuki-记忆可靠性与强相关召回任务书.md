# 记忆配置主体修正历史实施记录

起点 `92483ab`，分支 `codex/memory-relevance-and-reflection`，版本 3.8.1 / schema 0051。
下面保留 C1 的本地实现证据；旧四条自动召回、固定Schema与验收封口已退出现行规范。
原失败与真实复测见 [历史验收](memory-evidence-acceptance-progress.md)，当前合同见 [Memory](memory-v2.md)。

## 五、实施记录

### C1：配置主体入口审计

- 即时 mutation：默认保留真实用户/群配置；内部 context 可由自省服务提供配置主体。
- 自动提取 worker：现有 eligibility 只允许 canonical human inbound，不接受 Yuki、
  external_bot 或 system 证据作为自动提取主体，维持用户/群继承。
- rebuild：提交前复用相同 eligibility，保留原权限检查，不将历史维护变成扩权入口。
- self-reflection proposal/episode：由 batch 的 canonical 所有者提供 MemoryConfigScope，
  第一条 event/tool evidence 只负责 provenance，不决定配置主体。
- MemoryMutationContext → MemoryProcessingContext 明确转交该内部范围。
- 测试夹具已接入真实 RuntimeConfig；覆盖换 Presence 的 outbound 首证据、episode 的工具
  evidence、停用 Space 读取覆盖值、错误类型拒绝及原即时 mutation 行为。
- 此处只记录本地实现，不表示真实模型、完整回放或部署验收已经通过。
