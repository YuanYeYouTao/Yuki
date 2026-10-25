# 2026-10-25 沙箱 Manager 版本错配与恢复

## 故障

Bot 已运行 `35b49fe9ee001c8f51ec5a5c59a41687130493bc`，宿主
`yuki-sandbox.service` 的 `90-source-release.conf` 仍将 `PYTHONPATH` 指向
`185eba710695bb24cb996a60edd19a3fccad4937`。两者使用同一个 artifact manifest。

新代码已删除 artifact 的过期字段，旧 Manager 却在 `WorkspaceStore.cleanup()` 中执行
`UPDATE artifacts SET expires_at=...`，启动恢复时报
`sqlite3.OperationalError: no such column: expires_at`。恢复发生在监听 socket 之前，
所以磁盘上虽然留着 `manager.sock`，连接仍返回 `ECONNREFUSED`。

17:09（UTC+8）采样时服务处于 `activating / auto-restart`，累计重启 24,984 次。
停止循环前计数为 25,273。Bot 容器健康不能证明这个独立的宿主服务可用。

三次表情意图均在选图前以 `sandbox_unavailable` 结束，未选择资产，也没有平台表情
发送回执；伴随的文字已送达。现有记录没有被重新打开或重发。

另有宿主资源问题：机器约 1.575 GiB 内存，发生持续换页，采样中的 IOwait 达
65%–90%。这与 Manager 不可用分开判断。恢复 Manager 后仍观察到资源压力波动，
不能把服务恢复写成全部响应延迟已经解决，也没有证据据此认定内存泄漏。

## 当次恢复

用户明确授权后，停止了 Manager 的崩溃循环，并一致性备份原 `jobs.sqlite3` 和共享
artifact manifest。将完整 Manager 源码及 workspace 依赖同步到 Bot 的同一提交，
仅替换既有 source drop-in 中的 `PYTHONPATH`，沿原 venv、参数、媒体缓存配置和持久路径启动。
没有补回 `expires_at`，没有用旧数据覆盖新事实。

17:44:04（UTC+8）Manager 启动成功，随后真实 `environment_status` 返回 `ready=true`。
Bot、QQ 网关、代理及原 `yuki-environment` 的容器 ID、启动时间和重启次数均保持不变。
原环境仍使用既有 Supervisor 挂载，旧挂载来源没有被覆盖或清除。

## 已验证的结果

- Bot 实际 UID 10001 的 `yuki setup environment-check` 返回 `ok=true`、`manager=connected`。
- 原 1,833 条任务的 `id / request_id / payload_hash` 全部保留，没有重复 request。
  `environment_jobs` 仍为 1,801 条，原 completion outbox 为空，隔离回执仍为两条。
- artifact manifest 的 359 条 artifact 和 329 条不可变快照保留。
- Manager 能在原环境读取一份现存表情文件；真实 `sticker_index search` 返回一条结果，
  未要求重建索引。没有调用模型或发送 QQ 测试消息。
- 服务 PID 保持稳定，重启计数不再增长。健康接口返回 `status=ok`、数据库可用、
  OneBot 已连接；短时 IOwait 回落至 3%–4%，后续采样仍有内存和 IO 压力。

这证明沙箱连接及表情检索链路恢复。修复后尚无自然产生的新表情意图时，不能将其
写成真实 QQ 表情发送验收通过。原失败意图继续保留关闭状态。

## 部署入口的修正

原安装器只等待 socket 文件出现，无法识别无人监听的残留 socket；Bot 的普通健康
检查也不覆盖宿主 Manager。发布包没有同源升级 Manager 的明确入口，手工更新 Bot
后，旧 source pin 可以一直留在原处。

安装和升级现在通过同一个 Manager 安装入口选择实际 Bot 容器，从它导出完整源码，
更新既有 source pin，再以 Bot UID 发起真实连接检查。已有 unit 参数、其他 drop-in、
原数据和环境挂载继续保留。检查器只依赖标准库，能在 Manager 的最小 venv 中运行。

具体操作与首装、升级顺序见 [持久环境运维](persistent-environment.zh-CN.md)。
普通 Bot 健康检查仍是 Bot 的健康检查，不代替 Manager 的独立验收。

完整原始日志、私有数据库备份及用户会话不放入仓库。运维证据保留在本地部署报告中。
