# Yuki 持久工作环境

[English](persistent-environment.md)

Yuki 全局共用一个 Linux 环境、`/home/yuki` 和 `/workspace`，不按人物或群划分，文件不自动过期。`short_state` 仍是独立、有期限的短提示；目录、文件和完整执行日志不自动载入上下文。Bot 的 `./workspace:/app/workspace` 是 artifact/short_state 库；终端文件在宿主 `/var/lib/yuki-sandbox/home/workspace`，经原家目录挂载显示为环境内 `/workspace`。两者通过显式发布和附件导入衔接。

## 运行时与工具

可信宿主 Manager 管理常驻 gVisor 容器 `yuki-environment`，标签为 `io.yuki.sandbox=persistent-v1`。execd 固定为 `441042c288a3eacbe93b36236a9baf6f6254ad4d`，镜像保留 Apache 许可证。终端和服务共用 execd PTY 与原 Supervisor，没有第二套安装执行器或 sudo 审批。

环境内执行身份为 root，`HOME=/home/yuki`。Bash、Python、Node.js、Git、curl、jq、rg、压缩和编译工具已预装；apt、pip/npm、任意 shell 和环境内文件操作直接通过终端完成。pip user base、npm prefix 和缓存保留原家目录。环境内管理员能力不提供宿主 Docker 控制、Bot 数据库或凭据。

| 工具 | 行为 |
| --- | --- |
| `terminal_exec` | 启动一个原执行，约五秒交回原 `run_id`；`tty=true` 提供保留目录、变量和函数的真实交互 Bash |
| `terminal_read` | 按原 `run_id` 和字节游标读进展、退出码及原终态 `completion/artifact`；`wait_seconds` 默认零，只等待观察 |
| `terminal_control` | `input` 发送原样文本，`interrupt` 发送 SIGINT，`cancel/close` 停止原任务；已写文件保留 |

执行默认不计时；显式 `timeout_seconds=0` 也不设期限，正值使用指定期限，没有软件最大值。取消一次读取只取消观察，不停止命令。空输出不表示完成，非零退出不撤销此前文件写入；日志轮转会明确标记游标丢失。

新 Python、Node 和系统包工作均使用终端。`run_python`、`environment_packages`、`get_code_run`、`cancel_code_run` 和 `terminal_write` 不再接受新调用。旧 Python/APT 任务仍按原运行编号由 `terminal_read` 读取、由原 completion outbox 确认。升级不会启动旧未派发 APT，也不会重放已经派发但结果未知的安装。不再新建安装预算、dpkg 收集或自动 Docker 检查点；已有检查点镜像与原回执保留。

`environment_service` 保留登记、启动、停止、删除、状态、日志和真正的跨环境启动恢复，沿原 `restart` 策略执行。显式停止先禁用恢复；没有服务数量或连续失败次数封顶。修改登记保留正在运行的原执行，下次实际启动采用新内容。

普通文件读写、搜索、目录、修改和删除使用终端。八项普通 `workspace_*` CRUD、公开 artifact 目录/文本分页/删除及普通覆盖 CAS 全链退出，不保留新调用别名。`workspace_inspect` 仍把指定媒体交给模型，`workspace_publish` 固化不可变 artifact 并通过统一发送工具交付。会话附件提升、私有工具 artifact 和真实媒体桥保留；终端不能直接读 Bot 临时缓存或私有索引。已发布版本不随后续文件修改变化。

artifact manifest 使用独立 SQLite。文件准备、SHA256 和 fsync 在写事务外完成；短写事务核验真实 revision/不可变事实并提交 metadata。读取先安全打开原文件 FD，再释放读取事务并校验内容。发布异常只回收私有 pending 文件；结果未知时不删除已 rename 的 blob，留给按真实引用工作的原 GC。普通发布不设 200 MiB、512 MiB 或对象数量经验门槛；模型媒体与平台传输仍受其真实容量限制。

## 恢复、资源与网络

Manager 在派发前持久化原请求、执行意图和 execd 会话标识。断线后先检查原生退出记录，否则接回原会话；原 `started` 标记阻止再次执行。PTY 暂未运行、握手超时、旧安装缺 heartbeat 都不能按年龄推断失败。提交未知时查原请求，不盲目重提。Bot 持久化完成回执后才确认 outbox；丢确认或重复读取不重开命令、不重置预算。

容器 ID 与启动时间共同组成环境代际。Bot/Manager 重启保留原容器和进程；实际环境重建后，旧普通任务记录真实中断，登记服务按策略恢复。容器 restart policy 为 `no`，由 Manager 在文件系统挂载后启动。升级 Manager 不会暗中删除或重建原环境。

默认部署保留 2 GiB 家目录 ext4、独立 128 MiB 运行记录 ext4，以及 Docker 的 512 MiB 内存、额外 128 MiB swap、1 CPU、128 进程和有界 `/tmp`。这些是部署资源，不是任务接纳评分。没有主要执行 1 个、PTY 4 个、服务 2 个、低内存 256 MiB、安装增量 1 GiB、队列 4 或未确认完成 512 项门槛。容量、进程和 OOM 结果按真实 OS/Docker 事实返回。每任务保留两个 4 MiB 日志段；回收已完成日志和七天前已确认的运行文件，SQLite 执行与完成回执独立保留。

新环境接入现有 `yuki-sandbox-egress` 公网 Docker bridge。宿主防火墙阻止环境主动访问宿主、私网、链路本地及其他非公网 IPv4 目标；普通公网 TCP/UDP（包括 SSH）不按端口白名单限制。execd 不发布宿主端口，只允许回应宿主已建立的控制连接。环境不挂 Docker socket、Bot 配置/数据库、QQ 凭据或私有 artifact 索引。该 bridge 关闭 IPv6。默认不强制 HTTP 代理，仅运维显式 `--proxy` 配置代理。旧内网 Squid bridge 可为部署过渡保留，新环境不依靠它出公网。

所有 Main 入口、自动化和子 Agent 使用固定能力合同。工具目录不修改已提交声明，环境管理员能力不扩大人物、发送、委托或插件领域授权。可选 Code Mode 的 Monty 仍是无文件系统/网络的编排环境，不能当作持久终端别名。

## 安装、升级与回退

配置安装器不安装宿主 Manager。Linux Docker 宿主须有 Python 3.12、已验证的 runsc、环境镜像、bridge/防火墙和挂载好的家目录/运行记录文件系统。Docker Desktop 或 Bot healthy 不证明这些依赖可用。

```sh
sudo /opt/yuki-sandbox/deploy/sandbox/install-manager.sh --deployment-root /absolute/deployment/path
docker compose exec --user 10001:10001 bot qq-ai-bot-cli setup environment-check
```

部署根是 Compose 挂载 `./workspace` 的目录。安装器只记录 `/etc/yuki-sandbox/deployment.env` 中的 `YUKI_MANAGER_WORKSPACE_STORE`，保留其它配置，拒绝覆盖已配置的 artifact 根；不带参数保留既有值或旧默认路径。它不迁移家目录、不改已有容器挂载。`environment-check` 在 Bot 真实 UID 10001 下验证 artifact 目录可写和原 socket/status/ready，不再调用退役 `workspace_list`；不建容器、执行代码、调用模型或发送消息。这只证明连接，不证明真实管理员终端/公网已验收。

本次需同步宿主 `src/qq_ai_bot/sandbox/`、其 `workspace/` 依赖、`deploy/sandbox/` 和原 Manager venv，保留原 systemd 配置与全部部署覆盖。Supervisor 另以单个只读源文件挂入环境。不能只更新 Bot 后声称原环境已获得 root、新代码或新网络。

旧环境须单独安排一次明确更换：

1. 记录原容器 ID/启动时间、网络/挂载、Manager 当前检查点与活跃运行。可先让工作完成；若主动停环境，按真实代际中断处理，不自动重跑普通任务或未知安装。
2. 停 Manager 和原环境，在同一停写窗口一致备份 `jobs.sqlite3`、home/runtime、artifact/short_state 和 Bot 原回执库；保存原环境完整 rootFS 的私有 Docker commit 镜像。commit 不含 bind mount，家目录和运行记录必须另备份。
3. 保留原 HOME、运行文件和旧镜像，归档已停原容器。离线将原 `environment_state.checkpoint_image` 指向此次完整 rootFS 快照，不改原 run/request/hash、completion、服务或预算。Manager 优先此值，单改 `--image` 不覆盖已有检查点。
4. 更新 Manager/防火墙，由原 Manager 创建唯一活动环境：同一 HOME/runtime、同一 execd、`--user 0:0`、当前只读 Supervisor、公网 bridge。创建显式覆盖旧镜像用户/代理默认值，保原 HOME 路径与 apt/pip/npm 安装。不得启动第二活动环境共享家目录，不得用空基础镜像替代原 rootFS。
5. 核实实际 UID/HOME、旧文件/安装、Supervisor 挂载、原回执、服务恢复和公网/私网边界；另记 Bot health、固定工具与自然流量。镜像本地构建并校验传输，不重启 Docker、QQ、代理或无关同机服务。

rootFS 快照的镜像配置可能含原 execd secret，应作为私有运维备份保管，不公开推送或输出。回退保留最新消息、家目录、运行记录和回执库，不能用旧整库覆盖新事实。仅回退 Bot 时可保环境运行；实际环境身份/网络更换必须查容器和数据，不能以 healthy 替代。

## 定向验收

`tests/unit/test_terminal_environment.py` 覆盖原 Manager SQLite、旧 APT 不重放、无经验计数准入、原 rootFS 选择、网络规则 shell 脚本，以及隔离原生 Supervisor 的 PTY、输入、文件、后台和显式超时。工作区桥与 Work receipt 回归另覆盖发布、媒体和 owned/external run。这些不冒充实际 gVisor、管理员安装或公网测量。

`deploy/sandbox/validate_runtime.py` 仅对显式隔离 socket 做真实终端/发布探针。部署还须记录管理员身份、原安装保留、execd 重连、包管理、服务、网络和资源行为。不发真实 QQ 测试消息，不从声明缩小推算 cache/费用。

[2026-09-12 记录](https://github.com/YuanYeYouTao/Yuki/blob/v3.9.0/docs/operations/persistent-environment-validation-20260912.zh-CN.md)仅是旧部署/恢复点，不是本次 root、网络与工具合同已上线的证据。
