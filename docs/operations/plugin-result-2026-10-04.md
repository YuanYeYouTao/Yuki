# 插件后台小回执身份解析补修交付记录

状态：2026-10-04，代码已合并并部署 Bot；持续接话的 QQ 行为验收仍由用户完成。

## 问题与改动

插件后台 Main 已经通过 `send_message` 完成真实交付，旧代码随后在整理短工具回执时提前取 Artifact actor，触发 `tool_actor_unavailable`。原消息及 Social 成功回执仍存在；不能因整理失败重发。本次修复此前已有的接线问题，不改语义参与的任务模型。

`ToolResultBudgeter` 只在真正写入原件前解析原 Artifact 身份。短状态和发送回执按原 call ID 回到 Agent；短研究仍完整归档，分页读取不递归归档。真实归档缺少身份仍拒绝，原 typed outcome、来源及已提交回执保留。没有新增持久表、第二个 runtime、匿名授权或恢复循环。

语义参与主任务的设计和 C01–C15 证据见[原交付记录](semantic-continuation-2026-10-03.md)，本补修保留其双方接口和库 pin。

## 验证与部署

- [PR #232](https://github.com/YuanYeYouTao/Yuki/pull/232) 已合并；测试提交 `d57d31e6333aee8174ef6bd23e84f0e52ddbbc4e`，实际部署合并提交 `8d7fa982e1af68e935cb5db4f2f1a77d8e8727ef`。
- 库固定 `68bf033c37a524a2d377d2f67eaa25ce779c9e90`。镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-8d7fa98`，镜像 ID `sha256:0b49d81eaa269aaf6d6f4f5bc7f878500218364e914decb61e000ea882704a7a`；实际安装源码和归档 pin 按完整文件哈希核验。
- CI [Quality](https://github.com/YuanYeYouTao/Yuki/actions/runs/37142699010) 完成：Python **2742 通过、1 跳过**；Docker、类型、格式、Memory 基准及插件检查通过。另有 56 项定向测试，包括 7 项新回归；旧源码在隔离进程复现发送后报错，新源码证明仅发送一次且原回执保留。
- schema **0091 → 0091**，没有迁移；停 Bot 后完成一致 SQLite 快照、完整文件及原件引用校验。保留原 7 个未终态 Work 及其预算、来源、已消费输入和效果回执；启动后比较通过。
- Bot 的网关、Work 与子 Agent 轻量 readiness 通过；其他服务未操作。有效备份保留两份，旧备份的实际 Compose 引用先等价迁出，备份指针已推进。没有正式版本 tag 或 GitHub Release。

首次隔离预检的外层 Docker CLI 在 90 秒超时，而隔离 guard 容器实际于 170 秒后 exit 0；生产 Bot 当时未停止。补修部署采用与风险匹配的检查：前后全部迁移、库源码及两处补修以外的所有 Host 源码一致，实际镜像按文件核验，因此删除重复的离线整库 guard。完整停写备份的 integrity/FK 与引用校验、应用自身启动 guard 保留；没有修改应用 schema 校验或通过调大等待掩盖异常。

## 缓存观察与范围

补修前只读窗口 UTC `2026-10-03T17:24:11.10873207Z` 至 `2026-10-03T18:07:19.671600+00:00`，Gemini `gemini-3.8-flash` 的 `chat_agent` 计量成功 20 次。已知缓存 16 次，其输入 796,239 token、命中 688,972 token，加权命中 **86.53%**；其余 4 次未报告缓存。未知不计作零命中。

部分计量缺少原 turn ID，不能全部归到真人聊天；窗口中没有已确认 `user_message` 续聊样本。该观察不证明真人接话已验收，也不是补修前后缓存改善的 A/B 结论。本补修没有改固定提示词、工具声明或历史前缀；没有发送线上 QQ 或模型验收探针。

独立的 Memory evidence compaction `OperationalError` 只有异常类记录，缺少 SQL、SQLite code 与 stack，仍未查明；本补修不宣称解决它。轻量 readiness 也不代替 Memory/Dream 全量健康审计。

公共聚合证据：[部署 JSON](evidence/plugin-result-deployment-20261004.json)。私有原始工作、回执和聊天内容未发布。
