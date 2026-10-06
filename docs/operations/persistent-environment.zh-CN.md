# Yuki 持久工作环境

[English](persistent-environment.md)

默认 Compose 已挂载 `./workspace:/app/workspace`，这是 Bot 与 Manager 共用的
artifact/short_state 库；终端可写目录由 Manager 管理在
`/var/lib/yuki-sandbox/home/workspace`，通过现有家目录挂载显示为 `/workspace`。
不新增第二个可写工作区，也不把 Bot 数据库、QQ 凭据或 Docker 控制挂入环境。

配置安装器不安装宿主 Manager。路径文件工具和终端需要 Linux Docker 宿主上的
Python 3.12、已验证的 runsc、环境镜像、独立出网/防火墙与有界家目录/运行记录文件系统。
Docker Desktop 或 Bot healthy 本身不证明这些依赖可用。准备下文的宿主依赖后显式连接：

```sh
sudo /opt/yuki-sandbox/deploy/sandbox/install-manager.sh --deployment-root /absolute/deployment/path
docker compose exec --user 10001:10001 bot qq-ai-bot-cli setup environment-check
```

部署根必须是 Compose 挂载 `./workspace` 的目录。安装入口只记录
`/etc/yuki-sandbox/deployment.env` 的 `YUKI_MANAGER_WORKSPACE_STORE`，保留其它配置，
拒绝覆盖已配置的 artifact 根；不带参数保留该文件或旧 `/opt/yuki-qqbot/workspace` 默认值。
只识别本工具生成的整段双引号值或简单绝对字面路径，`#` 保留为路径字符；复杂引号或续行
明确拒绝，不猜旧路径，其它 EnvironmentFile 字节原样保留。
它不迁移持久家目录、不改已有容器挂载，也不暗中切换活动 Manager 的 artifact 库。
已有自定义路径应继续使用原配置；迁移须另行规划。

`environment-check` 要求实际 UID 10001，检查 artifact 目录可写性，仅读取现有
`environment_status` 和一项 `workspace_list`。缺 socket、UID 错误、环境未 ready 或
文件接口失败均非零退出，不建容器、不执行代码、不调用模型、不发 QQ、不打开 Bot 数据库。
这是连接与读取能力验收，不冒充实际终端/包安装测试；已有部署覆盖配置仍生效。

Yuki 全局共用 `/home/yuki` 和 `/workspace`，不按用户或群划分，文件不自动过期。
`short_state` 继续独立、有界并过期；文件目录、内容和完整日志不自动载入提示词。

工作区 manifest 已初始化后，`short_state` 快照只读，不创建缺失的短期状态表，也不取得
writer 清理过期槽。读取按同一时刻将过期 text 投影为空，保留 slot/revision/期限且不续期；
实际更新仍在原事务清理过期正文并核验 CAS 与容量。过期正文的物理清空延至下一次
实际更新，读取和冲突回执不暴露旧正文。首次工作区 schema 初始化仍需必要 DDL。

采用现有 gVisor、常驻容器和宿主 Manager，execd 固定在
`441042c288a3eacbe93b36236a9baf6f6254ad4d`，镜像内保留 Apache 许可证。
没有新增 Jupyter、浏览器或另一个 Agent。

家目录使用 2 GiB ext4 循环文件系统。Bash、Python、Node.js、Git、curl、jq、rg、
压缩及编译工具已预装，pip/npm 依赖和缓存保存在家目录。文件工具和终端实时共用文件；
容量耗尽会返回空间错误和占用信息。文件更新使用实际内容的 SHA256 版本，拒绝陈旧覆盖；
宿主逐层使用目录 FD，不跟随符号链接或硬链接。任意终端程序仍可能与最终 rename 竞争，
同一文件的并发编辑应自行串行。

`workspace_publish` 生成不可变 artifact，继续通过现有发送工具交付。旧 ID、
name/revision 参数、不带 path 的快照列表继续兼容。迁移保存 ID 到路径的映射、
by-id 别名及 manifest.json；artifact 表保持原有九列，便于回退。
快照库仍独立限制为 512 MiB、1000 个对象，可显式删除；可写家目录另有 2 GiB。

快照 manifest 使用独立 SQLite 文件。文件准备、SHA256 和 fsync 在写事务外完成，
短事务重新核验 revision、不可变标记和配额后 rename 并整批提交 metadata。
读取和列表只用读事务；读取先安全打开并核验普通单链接 FD，再释放数据库读取和校验 hash。
清理在事务外发现文件，短写事务重验引用并清理 metadata，提交后每次最多删除 128 个废 blob。
兼容 Manager 在线程准备文件，随后在原事件循环连续发布并完成任务，无中间取消点，
jobs.sqlite3 的共享连接不跨线程。
发布异常只回收私有 pending 文件。提交报错可能发生在实际提交之后，已 rename 的 blob
保留到按数据库引用核验的 GC，不能因结果未知删除已经提交的文件。

终端约五秒返回任务 ID，支持字节游标续读、原样输入、中断、取消和关闭。
真正的 PTY Bash 保存变量、函数和目录。普通执行默认 1800 秒，可设零表示不计时，
显式计时上限 86400 秒；run_python 保留最多 120 秒的兼容合同和输出 artifact 发布。

容器内监督进程保存退出码与分段日志。Manager 在启动前记录意图和 execd 会话标识，
重连先检查持久回执；已停止会话不重新执行，启动标记进一步防重。
容器启动时间属于环境代际，重启后普通任务标记中断，已登记服务按策略恢复。
提交结果不确定时查询原请求，不盲目重试。明确拒绝接纳的任务不会生成续跑。
容器的 Docker 重启策略设为 no，由已启用的 Manager 在两个文件系统挂载后启动。
Bot 或 Manager 重启会保留现有容器及其中的进程。

最多登记两个内部服务，支持登记、启动、停止、状态和日志。停止先禁用恢复；故障退避，
连续启动五次后停下，健康运行一分钟或显式 start 重置次数。系统包通过 Manager 在容器内以 root 执行 apt，
串行安装、记录 dpkg 版本并保存镜像检查点，保留当前和前一检查点。系统包累计增量预算
1 GiB，包含从检查点恢复的增量；这是接纳与监控预算，不是 overlay 硬配额。
安装中断后可调用 repair。普通操作使用 UID/GID 10001。

容器限制 512 MiB 内存、额外 128 MiB swap、1 CPU、128 进程；最多一个主要执行、
四个 PTY、两个服务。宿主可用内存低于 256 MiB 时暂停接纳。
独立 128 MiB ext4 文件系统限制运行记录和日志，防止从日志挂载绕过家目录容量。
单任务保留两个 4 MiB 日志段，已完成日志轮转，七天前已确认的运行文件清理；
SQLite 执行和完成回执独立保留。

环境不挂载 Docker 控制、Bot 配置/数据库、QQ 凭据或私有 artifact 索引。
execd 端口不公开，防火墙仅允许回应宿主已发起的控制连接，仍阻止主动访问宿主。
公网 HTTP(S) 继续走现有代理，内部服务可用 localhost。
所有主 Agent 入口、自动化和续跑采用相同完整工具声明，发送和委托边界继续保留，
已有自动化授权不会自动增加新能力。

部署在本地构建并校验传输，服务器只加载镜像。先保存启动状态，禁用网易云 MCP、
旧音乐签名和闲置硬件服务，保留 RSS、QQ、代理、Docker 与运维服务。
只暂停 Bot 和 Manager 做数据库、配置、artifact 和回执的一致性备份；初始化文件系统，
运行 migrate-only 并逐文件校验，再更新 Manager 与仅 Bot 的 Compose 服务。
保留全部已有 Compose 覆盖配置，不重启 SnowLuma 或 Docker。
上线检查 healthz、OneBot、RSS、完成/续跑 worker、工具声明及真实 gVisor 行为。
网易云必须同时关闭配置文件与 mcp_server_states 中的持久开关；后者优先于文件配置。

回退替换代码/镜像和 unit，保留新家目录、运行记录和消息数据库。
只回退 Bot 时可让持久环境继续运行；旧 python-v1 清理不会选择 persistent-v1 容器。
禁用环境前明确停止服务并保留两个循环镜像及 Manager 回执库，不能拿旧数据库覆盖新消息。
旧二进制仍能读取 artifact 快照，新路径工具需要新版合同。

定向验收包含双向文件、中文路径/游标、版本冲突、不可变发布、两种 Provider 请求对照、
真实 execd 交互、断线接管、pip/apt 和检查点恢复、服务恢复、OOM、网络失败、安装中断，
以及服务器独立 gVisor 容器内的容量和网络边界。验收不发送真实 QQ 消息，不跑无关全量。

实际部署版本、测量结果和恢复点见 [2026-09-12 交付记录](persistent-environment-validation-20260912.zh-CN.md)。
