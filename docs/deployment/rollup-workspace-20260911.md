# 群会话阻塞与工作区沙箱访问

## 故障证据

2026-09-11 22:19–22:22（Asia/Shanghai），群会话在 ContextAssembler 阶段反复抛出 `ConversationCoverageError: raw history is over budget but no continuous prefix is compressible`，没有进入模型请求。22:23 的 `/ai new` 后恢复。

GitHub 插件唤醒任务 69、70 分别于 19:32、20:53 创建，一直 pending，attempts=0。旧进程退出时取回了保留在 asyncio Task 中的异常：PluginBackgroundTurnWorker 在 `claim_turn → BEGIN IMMEDIATE` 遇到 SQLite `database is locked`，异常逃出循环。通知投递、表情和反思 worker 也有同类数据库异常退出。不能从原有 Bot 健康状态推断这些后台循环仍然工作。

未领取任务被历史保护查询无条件保留，直到历史超出预算；查询没有排除被后续真人消息取代、或处于 `/ai new` 边界之前的任务。这是任务生命周期与 Rollup 保护的配合缺陷，不是数据库备份被删除。锁竞争的具体长事务来源尚未定位。

## 修复

- `0f7e555`：插件 worker 外层恢复领取和前置执行异常，记录错误类别，接入公开健康状态；历史保护排除已经失效的唤醒来源，继续保护有效任务。
- `26b9fee`：通知投递、表情和反思 worker 对数据库异常退避恢复；沙箱自动提供共享工作区的文件快照。
- `95d30e2`：主入口冻结工具时保留完整声明，避免目录短说明与自动化完整说明不一致。工作区说明超过原来的 240 字符后暴露了该缺陷，候选版本启动被校验拒绝；已回退，再修复并通过两协议主入口 HTTP 对照。
- 沙箱可直接读取 `/workspace/文件名`。同名文件只提供 `/workspace/by-id/artifact_id`，`/workspace/manifest.json` 列出路径，避免默取第一项。原 `/inputs/artifact_id` 接口兼容保留。快照只读，修改可复制到 `/work`，产物写 `/work/outputs` 后导回工作区。宿主数据库和索引不进入快照。

## 验证与部署

插件队列恢复及历史保护定向回归 14 项通过；沙箱合同与工作区快照 2 项通过（包含既有沙箱生命周期场景）；版本发布合同 23 项通过。Ruff 与修改模块的定向 Mypy 检查通过，未运行全量测试。

第一步部署后，生产任务 69、70 已由正常领取流程取消，原因为 `superseded_covered`，没有补发旧回复。公开健康状态确认插件 worker 和 Rollup worker 均运行、QQ 已连接。

最终镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:workspace-final`，在本地完整构建的 `workspace-26b9fee` 上加入声明修复层，再压缩流式传输。镜像 ID：`sha256:9567239f2fd1b973ac6c28712328ddde5138f7245079a603b8a120ca6837b9e2`；运行提交 `95d30e2`。

最终部署备份路径 `/opt/yuki-qqbot/backups/pre-workspace-95d30e2`；包含当时的数据库、工作区、Manager 源码和配置。回退时恢复备份中的 `manager.py`，使用追加 `docker-compose.workspace-final.yml` 之前的 Compose 文件列表启动 Bot；数据库 schema 不变，无需恢复旧业务库。

真实 gVisor 沙箱验证已通过：未传 `input_artifact_ids`，列出 33 个工作区文件，成功读取并解析棋局 JSON；未创建输出文件或发送聊天消息。回执记录在备份目录的 `sandbox-check.json`。

最终版本已上线，Bot 和数据库健康，QQ 已连接，插件唤醒、表情、Rollup worker 均运行；启动日志无 ERROR。主入口成功冻结 168 个工具。任务 69、70 保持 cancelled，未补发。最终 `health-check.json` 和 `deployment-result.json` 位于上述备份目录。SnowLuma 容器未重建。
