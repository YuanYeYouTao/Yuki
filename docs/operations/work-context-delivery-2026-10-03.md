# Work 上下文与普通聊天续接实施记录

本文中的 responseSchema 实验属于该次历史交付；此分支已于 2026-10-09 退役，不作为当前摘要配置步骤。

交付快照：2026-10-03，[PR #228](https://github.com/YuanYeYouTao/Yuki/pull/228) 已合并并完成 Bot-only 部署，合并提交 `3d9e273`、数据库 `0089`。本文保留该轮证据，不代替真实 QQ 能力验收；后续 `0090` 与前台整理行为见[PR #229 交付记录](foreground-rollup-sqlite-contention-2026-10-03.md)。

原施工任务书已退役，现行上下文规则见[主 Agent 合同](../architecture/main-agent-runtime.md)；Pi Durable 的职责、检查点和持久回执对照见[源码对照](../architecture/pi-durable-work-context-comparison-2026-10-03.md)。本轮借用设计和验证思路，不引入 Pi 依赖、TaskEngine 或第二套执行台账。

## 实现与定向证据

| 任务书项 | 实现和证据 | 边界 |
| --- | --- | --- |
| F01 / V01 | Work 正常业务恢复接当前公共聊天；真实多人组合验证两个 steer、两个一般群聊及后续输入按事件顺序各出现一次，重开仍保留原输入消费和回执 | 原子 publication 故障回归已通过；合成回归不代表自然群聊验收 |
| F02–F04 / V02、V08 | Main W1→W2→W1、入口恢复及实际读集核验；派生 Rollup 维护不清冻结公共视图，真实来源和隐私变化仍拒绝 | 未决协议及 child 保留原精确恢复路径 |
| F05 / V09 | 观察来源与摘要 coverage、paid 页复用及原文引用接替 | 摘要质量由模型决定，确定性引用和来源校验不保证语义无损 |
| F06 / V05、V05b | 可选 context_note 复用 checkpoint CAS，按实际 actor/read-scope/privacy 校验；缺 note 不硬停 | 不以 note 代替原目标、输入和执行回执 |
| F07–F08 / V04、V11 | 完整资料提前归档、分页回查和多个引用拥有者；真实插件身份/记忆 scope 隔离、GC 和删除围栏 | 线上资料质量和全部权限组合不以已有定向测试代替 |
| F09 / V03、V10 | 普通无 Work 工具续接容量整理；原请求仍 fit 时辅助摘要失败可继续，真实超限停止；公开原生响应镜像与确定性效果事实参与整理 | 真实摘要失败的限制见下文；后续软整理独立于前台回复见 PR #229 |
| F10 | 主请求硬容量与软整理基准分开；本轮部署基准为 90000 token，比例约得到 81000 / 54000 | PR #229 部署调整为约 162000 / 54000；均为可调整政策 |
| F11–F12 / V06、V07 | 复用固定工具合同、原预算、效果回执和 ProtocolStore；不新增沙箱私有资料挂载 | 已核验部署保留；不表示全部线上业务已结项 |

## 实际模型实验的边界

[独立实验记录](work-context-cache-experiment-2026-10-03.md)保留全部十七轮 110 个物理请求：Gemini 68、DeepSeek Chat 22、Responses 20；reported input 为 2640045、output 为 235217、total 为 2875262，原 HTTP 400 的缺失 usage 另计未知。Responses 同会话九轮样本总命中比例约 95.108%，后八轮约 99.023%，这些比例属于对应路由和样本，不能解释为生产缓存保证。

Gemini 普通整理及 W1→W2→W1 追加实验成功。早期 Gemini Work、DeepSeek Chat 和 Responses C06 摘要分别出现格式或引用不满足合同，全部保留严格拒绝证据，不关闭来源校验或做盲重试。最后的经典 Gemini `responseSchema` 完整 Work schema 单样本通过严格结构与引用校验，生产 converter 的离线 wire 投影与该成功样本一致；不是宣称生产 serializer 已再次实测或所有摘要质量均可靠。

独立真实 Runner 恢复实验使用合成的已接受摘要页，只发生一个真实主请求：原回执不变、原效果调用一次、pending steer 消费并呈现一次、模型预算由 1 增至 2。合成页不计物理请求，不能称 Gemini paid 分页摘要成功。实际原请求仍可装入时的继续执行另由真实离线回归验证。C07 图片/MCP 真实组合未执行，不能称完整线上能力验收。


## 0089 与代码回滚准备

0089 新增观察、选择和资料引用持久结构。非空数据不允许通过 downgrade 抹去；旧 0088 镜像不能直接启动 0089。准备的兼容回滚镜像保持 7d26e22 旧业务 runtime，只加入识别 0089 的结构校验、迁移和必要读取/lifecycle 兼容。

更新到最终 schema guard 后，隔离实际容器重新验证了六个兼容文件 hash、0089 schema、原三个 accepted 效果不重跑、原预算和可选 note 保留；兼容镜像 ID 为 `sha256:a9fd95f5101f4c72f3ad3b8c73854b02e37e48a809035f7a2524c745cec54001`。该证据不意味着所有新私有原生协议和权限组合都已用旧 runtime 验收；新 scoped 原件对旧读取器拒绝，避免扩大可读范围。代码回滚保留线上数据库、消息、产物和回执，禁止恢复旧备份覆盖升级后业务效果。部署准备还包含 Gemini 方言字段的窄 CAS：原字段不存在，仅新增指定值；兼容旧 runtime 启动前恢复该字段原 presence/value，若该字段发生第三方变更则拒绝覆盖，其他 Profile、路由及数据库不改。

## 最终交付状态

原子派发修复、公共增量经过私有压缩仍保留，以及未完成 paid 页使用原锚点的组合为 83 项通过。其后独立审查发现 paid staging 恢复在消费 pending steer 时可能重付页，已延后新输入消费并补充八项真实 Runner 回归；工具配对、paid 材料退休和通信游标在同一 writer 提交。恢复 `response + staging` 的真实 Main 入口另证当前公共历史准备，最新协议准备文件 22 项通过；这些定向证据仍不代替全量。

显式 Gemini 方言小修保持默认 `response_json_schema`，只有指定 Profile 才使用 `response_schema`。辅助请求继续使用原完整结构和本地严格来源校验，主工具合同、模式和路由保持原样。真实经典 wire 对照样本通过严格结构与引用校验；该单样本不能证明所有模型摘要均可靠。

最终 PR Quality [37066514072](https://github.com/YuanYeYouTao/Yuki/actions/runs/37066514072) 六项检查通过，Python 全量为 2551 passed / 1 skipped。全仓 Ruff、1095 文件格式检查、Linux 692 源文件 mypy 和版本基线通过；定向子组不累计为全量次数。

合并提交为 `3d9e2733991fe00f7774ed83c2c4acf9eaafdb01`，与受测提交树相同。该轮镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-3d9e273` 于台北 2026-10-03 06:00:52 启动，amd64 / 3.9.0，实际数据库 `0089`、674 个安装源码 hash 与迁移匹配，运行/健康及网关、Work、子 Agent 就绪核验通过。停止 Bot 后备份和启动后保留比对通过，原预算、回执及输入围栏未改变；未恢复旧数据库，未发送 QQ 或发布 release。

上述是当时部署快照，已由 PR #229 镜像接替。真实自然群聊、摘要质量和生产缓存改善仍需独立验收；该轮启动后仅观测到 Dream 请求，cache 字段缺失，不能把实验命中率当作线上结论。
