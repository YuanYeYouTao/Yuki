# 持久工作环境交付记录 — 2026-09-12

[English](persistent-environment-validation-20260912.md)

持久工作区、终端、系统包和服务工具已部署，包含语音识别提交 `1339930` 及此前合并的
内存修复。实现提交为 `18b5265`、`40af234`、`6d036d8`、`1dd3f95`、`ef28d91`。

## 线上版本与状态

| 组件 | 版本 |
| --- | --- |
| Bot | `ghcr.io/yuanyeyoutao/yuki-qqbot:persistent-1dd3f95` |
| Manager | `ef28d91`，独立 Python 3.12 venv，websockets 15.0.1 |
| 环境镜像 | `yuki-environment:20260912` |
| execd | `441042c288a3eacbe93b36236a9baf6f6254ad4d` |
| 数据库 | `0055`，对应 `0055_audio_transcripts.py` |
| 固定工具声明 | 168 个，包含全部持久环境工具，不含网易云工具 |

镜像在本地构建，传输校验后由服务器加载，摘要见英文记录。部署保留全部 Compose
覆盖配置，只重建 Bot；Docker 和 SnowLuma 未重启。持久环境 `78a71ac5fa3e` 跨 Bot、
Manager 重启保留，QQ 网关仍为 `9e7a1a89696e`。环境由 Manager 在存储挂载后恢复启动。

23 个旧 artifact 已逐个核对哈希并迁移，旧 ID 和不可变快照继续可用。家目录为 2 GiB，
运行记录与日志为独立 128 MiB 循环文件系统；ext4 元数据会占用部分容量。
发布快照另有限额 512 MiB、1000 个对象。系统包增量的 1 GiB 是接纳和监控预算，
不是 overlay 硬配额。

上线健康检查通过：数据库、QQ、RSS 正常，ASR 已启用并配置，完成回执、续跑、自动化、
插件 worker 均运行且无错误。其余两个 MCP 已连接。Rollup 正常运行，未见过期处理租约
或近期基础设施错误；检查时没有待确认的沙箱回执，最终 Bot 和 Manager 重启计数均为零。
原有记忆争议记录和历史 dream 模型失败仍在健康数据中，本次没有把它们标为已修复。

## 定向验收

- 修改代码通过 Ruff 和 Linux 平台 mypy。Windows 集中测试为 8 通过、3 个 POSIX 跳过；
  Linux 持久环境测试为 5 通过，没有运行无关全量。
- 两种 Provider 协议按实际主入口到 HTTP 请求对照，覆盖插件、定时任务和续跑的完整声明。
- 真实 execd 验证双向文件可见、中文路径、版本冲突、不可变发布、PTY 状态与输入、
  输出续读、取消、run_python 兼容输出，以及 Manager 断线时任务继续完成。
- 安装 pip `humanize==4.15.0` 和 apt `hello`，重建检查点后仍可用；验证服务恢复、普通
  任务中断不重放、网络错误、隔离 cgroup OOM、apt 安装中断及修复。
  家目录内伪造的可执行文件和 shell 启动文件不能替换提权安装命令。
- 服务器使用独立 gVisor 容器验证 UID 10001、共享文件、真实容量耗尽及清理后恢复、
  代理公网 HTTPS、主动连接宿主 SSH 被拒绝。单独的真实 execd 断线检查确认：Manager
  重连、重复提交相同请求后仍仅执行一次、生成一份规范回执，重复确认也保持幂等。
  验收没有真实 QQ 发送，隔离容器不含 Bot 或 QQ 凭据。

## 服务精简与测量

网易云 MCP、旧音乐签名容器已停止并取消自动重启；音乐卡片插件保持禁用，配置与数据保留。
multipathd 及 socket、ModemManager、fwupd 及刷新 timer/service、udisks2 已停止并 mask，
包含自动激活入口。RSS、QQ、代理、Docker 和系统运维服务保留。

首次切换发现网易云的数据库持久开关覆盖了文件中的禁用配置，保存原状态后已同步关闭。
最终固定工具声明已移除网易云。两个旧部署探针的完成事件缺少 Bot 来源，原载荷存入
`quarantined_completion_outbox` 并记录原因，没有伪造来源或触发续跑。

| 测量项 | 实测值 |
| --- | --- |
| 宿主物理内存 | 1612 MiB |
| 停服务前可用内存 | 607 MiB |
| 停服务后即时可用内存 | 906 MiB |
| 最终上线及清理后可用内存 | 约 747 MiB |
| 最终 swap 占用 | 约 799 MiB |
| Manager 进程 PSS | 约 23 MiB |
| 空闲环境 Docker 内存 | 不同采样为 20–44 MiB |
| execd 客体报告 RSS | 约 37 MiB |
| gVisor sentry 与 gofer 宿主 PSS | 后续采样约 28 MiB |
| 隔离环境分配 64 MiB 的负载 | 容器 135.5 MiB，CPU 1.19% |
| 磁盘释放 | 2,971,820,032 字节，约 2.77 GiB |
| 最终磁盘可用 | 约 8.9 GiB，占用率 77% |

内存采样受业务负载、缓存和 swap 变化影响，初次增加的 299 MiB 可用内存不能全部归因于
停用服务；没有独立对照能证明原来 50–70 MiB 的估算。客体 RSS、宿主 PSS、容器计量存在
重叠，不能相加。

清理了五份旧备份、镜像传输包、重复暂存目录、已卸载的验收文件系统和十一个未使用的
旧 Bot／回退／测试镜像标签。保留当前镜像、兼容回退镜像及所有用户数据，没有全局 prune。

## 最新恢复点

仅保留 `/opt/yuki-qqbot/backups/pre-environment-final-20260912T145430`，路径同时记录在
`/opt/yuki-sandbox/latest-restore-point`。

包含数据库、配置、artifact、持久家目录、运行文件、Manager 源码与回执、unit、Compose
覆盖配置、原服务启动状态和迁移记录。三份 SQLite 副本的 quick_check 均通过。
complete.json、health-final.json、cleanup.json、runtime-process-memory.json 保存验证证据。
备份目录权限为 0700，因配置内包含凭据。

保留的兼容回退镜像是 `persistent-40af234`，支持数据库 `0055` 和持久环境合同。
只回退 Bot 时，修改最后的镜像覆盖配置，携带全部覆盖配置重建 Bot；保留当前数据库、
家目录、运行文件、Manager 和执行回执，不用旧库覆盖新消息或文件。
本次没有重启生产宿主；恢复语义在隔离环境验收，生产仅验证服务启动顺序。
