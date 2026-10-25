# Yuki 持久工作环境

[English](persistent-environment.md)

Yuki 全局共用一个 Linux 环境、`/home/yuki` 和 `/workspace`，不按人物或群划分，普通持久文件不自动过期。`short_state` 仍是独立、有期限的短提示；目录、文件和完整执行日志不自动载入上下文。Bot 的 `./workspace:/app/workspace` 是 artifact/short_state 库；终端文件在宿主 `/var/lib/yuki-sandbox/home/workspace`，经原家目录挂载显示为环境内 `/workspace`。工作文件在发送时冻结为不可变交付快照，入站附件从共享缓存直接复制到普通工作区。

所有会话的临时媒体共用 `/inputs/media-cache`，它是 Bot 原缓存目录的同一份只读挂载，不在 2 GiB HOME 中再复制一份。Bot 仍是唯一写入和到期清理 owner；媒体到期后由原清理器删除，普通工作文件、已收藏表情和不可变 artifact 不随它清理。Main 可直接用终端读取、复制到 `/workspace` 或表情目录自由编辑，也可用`workspace_inspect` 选读或 `send(path="/inputs/media-cache/...")` 发送；Host 复用原安全 FD 桥，不跟随模型写的链接访问宿主文件。临时媒体的全局共享由用户明确授权，不再声称终端读取受当前 Conversation 限制。

## 运行时与工具

可信宿主 Manager 管理常驻 gVisor 容器 `yuki-environment`，标签为 `io.yuki.sandbox=persistent-v1`。execd 固定为 `441042c288a3eacbe93b36236a9baf6f6254ad4d`，镜像保留 Apache 许可证。终端和服务共用 execd PTY 与原 Supervisor，没有第二套安装执行器或 sudo 审批。

环境内执行身份为 root，`HOME=/home/yuki`。Bash、Python、Node.js、Git、curl、jq、rg、压缩和编译工具已预装；apt、pip/npm、任意 shell 和环境内文件操作直接通过终端完成。pip user base、npm prefix 和缓存保留原家目录。环境内管理员能力不提供宿主 Docker 控制、Bot 数据库或凭据。

| 工具 | 行为 |
| --- | --- |
| `terminal_exec` | 启动一个原执行，约五秒交回原 `run_id`；`tty=true` 提供保留目录、变量和函数的真实交互 Bash |
| `terminal_read` | 按原 `run_id` 和字节游标读进展、退出码及原终态 `completion/artifact`；`wait_seconds` 默认零，只等待观察 |
| `terminal_control` | `input` 发送原样文本，`interrupt` 发送 SIGINT，`cancel/close` 停止原任务；已写文件保留 |

执行默认不计时；显式 `timeout_seconds=0` 也不设期限，正值使用指定期限，没有软件最大值。取消一次读取只取消观察，不停止命令。空输出不表示完成，非零退出不撤销此前文件写入；日志轮转会明确标记游标丢失。

Bot 的任务意图、completion、permission 回调与 continuation 使用主库共享 writer，沿原
request/run 提交后才 ACK；completion 与父 Work 输入仍原子结算。文件、JSON 准备和远端等待
在 writer 外完成。Manager 的独立 jobs.sqlite3 保留自己的 SQLite owner，不接入 Bot 的主库队列；
取消观察或 Bot 重启不会凭空取消原 Manager 执行，也不会重新提交已有请求。

新 Python、Node 和系统包工作均使用终端。`run_python`、`environment_packages`、`get_code_run`、`cancel_code_run` 和 `terminal_write` 不再接受新调用。旧 Python/APT 任务仍按原运行编号由 `terminal_read` 读取、由原 completion outbox 确认。升级不会启动旧未派发 APT，也不会重放已经派发但结果未知的安装。不再新建安装预算、dpkg 收集或自动 Docker 检查点；已有检查点镜像与原回执保留。

`environment_service` 保留登记、启动、停止、删除、状态、日志和真正的跨环境启动恢复，沿原 `restart` 策略执行。显式停止先禁用恢复；没有服务数量或连续失败次数封顶。修改登记保留正在运行的原执行，下次实际启动采用新内容。

普通文件读写、搜索、目录、修改和删除使用终端。八项普通 `workspace_*` CRUD、公开 artifact 目录/文本分页/删除及普通覆盖 CAS 全链退出，不保留新调用别名。`workspace_inspect` 仍把指定媒体交给模型；`send(path=..., attachment_kind=...)` 内部固化不可变 artifact 并交付，模型侧不再提供发布工具。收藏与附件保存直接从只读缓存流式复制到最终文件，不留下永久中转快照。会话附件提升、私有工具 artifact 和真实媒体桥保留；终端可经共享只读挂载读取临时媒体原件，Bot 私有 artifact 索引不挂载。已发布版本不随后续文件修改变化。

artifact manifest 使用独立 SQLite。文件准备、SHA256 和 fsync 在写事务外完成；短写事务核验真实 revision/不可变事实并提交 metadata。读取先安全打开原文件 FD，再释放读取事务并校验内容。发布异常只回收私有 pending 文件；结果未知时不删除已 rename 的 blob，留给按真实引用工作的原 GC。普通发布不设 200 MiB、512 MiB 或对象数量经验门槛；模型媒体与平台传输仍受其真实容量限制。

### 外部 Agent 与原环境分离

external_agent 已移出环境工具和 Manager 新任务路径。用户电脑上的原生 Agent 经独立连接器接入 ExternalAgent 服务，不再通过服务器／workspace 启动 Codex、Claude 或 ACP 子进程。普通终端、文件和环境服务沿原 Manager 运行，设备离线不影响这些工具。

升级时删除原 Manager ExecStart 中的 --acp-config 参数和部署环境中的 YUKI_MANAGER_ACP_CONFIG。新环境不安装 ACP SDK、不挂载 acp_peer.py，也不安装供应商 adapter 充当用户电脑。现有 rootFS、用户安装和原进程不会因源码升级被自动删除或重建。

历史 server ACP 的 request／run／peer／permission／cursor／completion 保留原事实。legacy reader 只查询原活动、接纳精准取消／原权限回答并结算，不接纳新 peer 或 prompt。旧活动未决时保留未决，不批量重置 Work、不重发付费操作。既有旧挂载可留到容器真实重建，不为删除一项挂载强制中断全部终端。原生会话、用户登录和供应商侧历史不由任务归档代为删除。

## 恢复、资源与网络

Manager 在派发前持久化原请求、执行意图和 execd 会话标识。断线后先检查原生退出记录，否则接回原会话；原 `started` 标记阻止再次执行。PTY 暂未运行、握手超时、旧安装缺 heartbeat 都不能按年龄推断失败。提交未知时查原请求，不盲目重提。Bot 持久化完成回执后才确认 outbox；丢确认或重复读取不重开命令、不重置预算。

容器 ID 与启动时间共同组成环境代际。Bot/Manager 重启保留原容器和进程；实际环境重建后，旧普通任务记录真实中断，登记服务按策略恢复。容器 restart policy 为 `no`，由 Manager 在文件系统挂载后启动。升级 Manager 不会暗中删除或重建原环境。

默认部署保留 2 GiB 家目录 ext4、独立 128 MiB 运行记录 ext4，以及 Docker 的 512 MiB 内存、额外 128 MiB swap、1 CPU、128 进程和有界 `/tmp`。这些是部署资源，不是任务接纳评分。没有主要执行 1 个、PTY 4 个、服务 2 个、低内存 256 MiB、安装增量 1 GiB、队列 4 或未确认完成 512 项门槛。容量、进程和 OOM 结果按真实 OS/Docker 事实返回。每任务保留两个 4 MiB 日志段；回收已完成日志和七天前已确认的运行文件，SQLite 执行与完成回执独立保留。

新环境接入现有 `yuki-sandbox-egress` 公网 Docker bridge。宿主防火墙阻止环境主动访问宿主、私网、链路本地及其他非公网 IPv4 目标；普通公网 TCP/UDP（包括 SSH）不按端口白名单限制。execd 不发布宿主端口，只允许回应宿主已建立的控制连接。环境不挂 Docker socket、Bot 配置/数据库、QQ 凭据或私有 artifact 索引。该 bridge 关闭 IPv6。默认不强制 HTTP 代理，仅运维显式 `--proxy` 配置代理。旧内网 Squid bridge 可为部署过渡保留，新环境不依靠它出公网。

Docker 自定义 bridge 的 `127.0.0.11` loopback DNS 无法由 gVisor 访问，单加 `--dns` 也不能绕过。Manager 可指定 `--resolv-conf /var/lib/yuki-sandbox/home/.config/resolv.conf`，将该普通文件挂载到 `/etc/resolv.conf`。创建环境前填入部署现场可达的 DNS；程序没有内置解析器或自动兜底。文件可由环境 root 从 `/home/yuki/.config/resolv.conf` 修改，随原持久 HOME 保留。此选项仅作用于容器创建，不替换已存在环境。见 [gVisor DNS 说明](https://gvisor.dev/docs/user_guide/faq/#dns-doesnt-work-on-a-docker-user-defined-bridge-network-including-docker-compose)。

所有 Main 入口、自动化和子 Agent 使用固定能力合同。工具目录不修改已提交声明，环境管理员能力不扩大人物、发送、委托或插件领域授权。可选 Code Mode 的 Monty 仍是无文件系统/网络的编排环境，不能当作持久终端别名。

## 安装、升级与回退

### 媒体直拷与统一发送升级

本次 Bot 与 Manager 须配套更新：模型只调用 `send`，Manager 保留私有发布并新增直接复制，旧 checkout 和导入迁移不再接受新调用。保留原 Manager 数据库、`environment_mutations`、HOME、图库、工作目录和已发布 artifact；已有任务、付费协议和真实回执不改名、不重放。工具合同切换形成合法新链，不修改已提交的工具声明或承诺复用旧前缀缓存。

源码的图库指引已更新；已有 `/workspace/stickers/AGENT.md` 是用户文件，升级时只将其中失效的模型工具名改为 `send`、Code 投影名改为 `yuki_send`，保留其余正文。不要用仓库整份文件覆盖用户指引，不批量改旧 TaskSpec 或通知的原参数。

旧中转快照清退使用 [离线脚本](../../scripts/retire_media_snapshots.py)。它不停止服务，也不提供常驻 GC；在完整停写副本上审计，或在 Bot、Manager 与环境终端均停写的窗口执行。主业务库、Manager 数据库、artifact 根、普通工作目录和私有协议资料须来自同一部署：

```sh
python scripts/retire_media_snapshots.py \
  --workspace /copy/bot/workspace \
  --home-workspace /copy/sandbox/home/workspace \
  --database /copy/bot/data/qq_ai_bot.db \
  --manager-database /copy/sandbox/jobs.sqlite3 \
  --protocol-root /copy/bot/data/work-protocol \
  --tool-artifacts /copy/bot/data/tool_artifacts > media-snapshot-audit.json
```

这些是示例路径，Manager 数据库取实际配置，不能猜文件名；`protocol-root` 是业务数据库父目录下的 `work-protocol`，`tool-artifacts` 是 Bot 进程当前工作目录的 `data/tool_artifacts`，均不能指到普通 artifact 根。独立插件或额外持久引用用重复的 `--extra-database`、`--extra-reference-root` 传入实际材料。

默认只输出候选、真实引用、同字节普通文件与保留原因。显式 `--apply` 会重新扫描，再清退确证无引用、且普通文件仍有相同字节的 `sticker:`/`attachment:` 中转快照；图库原图、JSON 和原 `environment_mutations` 保留。Social 成功或未知、实际发布、Work/Client/插件与私有已付资料中的引用都会保留快照；材料缺失不宣布无引用。不承诺回收全部候选，分别记录候选与删除数量。这是显式升级操作，本轮开发未执行生产清退。

### Manager 安装

配置安装器不安装宿主 Manager。Linux Docker 宿主须有 Python 3.12、已验证的 runsc、环境镜像、bridge/防火墙和挂载好的家目录/运行记录文件系统。Docker Desktop 或 Bot healthy 不证明这些依赖可用。

部署包带有 `deploy/sandbox/` 的宿主部署入口。Manager 源码从显式选中的 Bot 容器导出，不另选一个 checkout：先启动本次 Bot，再在 Linux Docker 宿主、实际 Compose 部署目录执行。首次安装先建立原 Manager venv：

```sh
sudo python3 -m venv /opt/yuki-sandbox/venv
sudo /opt/yuki-sandbox/venv/bin/python -m pip install -r deploy/sandbox/requirements-manager.txt
sudo sh deploy/sandbox/install-manager.sh \
  --deployment-root "$PWD" --bot-container "$(docker compose ps --quiet bot)"
docker compose exec --user 10001:10001 bot yuki setup environment-check
```

使用自定义 Compose 文件时，容器选择和最终检查都沿同一条实际 `-f` 链；也可直接给 `--bot-container` 传入已核实的容器名或 ID。该参数必填，不再从旧 `/opt/yuki-sandbox/src` 猜代码版本。

安装器调用 `sync-manager.py`，将该 Bot 的完整已安装 `yuki` 包放到 `/opt/yuki-sandbox/releases/<Bot镜像提交>/src`，定向更新现有 `90-source-release.conf` 的 `PYTHONPATH`。已有 unit 参数、媒体缓存覆盖、原 venv、HOME、工作区和仍被环境挂载的旧源码保持原样。已存在的同提交源码只核对字节并复用，不覆盖正在被引用的不同内容。同步入口本身不重启服务或修改数据；安装器随后重启原 Manager，并在真实 UID 10001 下完成 artifact 写权限与 `environment_status` 的握手。残留 socket 文件不能再被当作成功。

部署根是 Compose 挂载 `./workspace` 的目录。安装器只记录 `/etc/yuki-sandbox/deployment.env` 中的 `YUKI_MANAGER_WORKSPACE_STORE`，保留其它配置，拒绝覆盖已配置的 artifact 根；不带参数保留既有值或旧默认路径。它不迁移家目录、不改已有容器挂载。Bot `environment-check` 与宿主安装器复用同一个标准库检查实现，不建容器、执行代码、调用模型或发送消息。这只证明连接，不证明真实管理员终端/公网或QQ表情送达已验收。

已有部署升级时，先备份原 `jobs.sqlite3`、共享 `manifest.sqlite3`、配置及相关文件，停止 Manager，再按原 Compose 链升级 Bot，最后运行上面的安装器与检查。不要让旧 Manager 在 Bot 已迁移的共享索引上继续运行。`yuki setup verify` 只核验 Bot 核心服务；Manager 升级以这一安装器的真实连接结果为准。3.9 升 4.0 时，原自定义 unit 中退役的 `-m qq_ai_bot.sandbox.manager` 须明确改为 `-m yuki.sandbox.manager`，保留其余实际参数；不提供旧模块别名。

媒体缓存另外沿真实部署配置。在原 `/etc/yuki-sandbox/deployment.env` 保留其他字段，显式加入：

```ini
YUKI_MANAGER_MEDIA_CACHE=/absolute/deployment/path/data/conversation-media-cache
```

此值必须对应 Bot `CONVERSATION_MEDIA_CACHE_DIRECTORY` 挂载的真实宿主目录，不能把整个 data、数据库或配置目录挂进去。也可由 Manager CLI `--media-cache` 显式指定；程序不猜仓库路径，不修改自定义 ExecStart。新环境创建时把该目录只读挂到 `/home/yuki/inputs/media-cache`，原 `/inputs` 链接提供相同视图。Manager status 的 `media_cache.configured/shared` 报告实际配置与只读挂载事实；旧环境缺少该视图仍可用其他工具，不会自动删除或重建。

已有环境不能靠更新配置增加 Docker 挂载。此次启用共享媒体需要沿下文完整 rootFS checkpoint 流程明确替换一次：保留同一 HOME、runtime、原安装与服务，只增加缓存的只读视图。切换后用实际终端验证能读现有媒体、写缓存返回只读文件系统，复制到 `/workspace` 后可编辑；不要只凭 status 或新镜像声明验收。

以后沿上述安装器从同一 Bot 交付 Manager，不再分别手工复制 `sandbox/` 和 `workspace/`。当前依赖在原 Manager venv 安装：

```sh
sudo /opt/yuki-sandbox/venv/bin/python -m pip install -r deploy/sandbox/requirements-manager.txt
```

表情 helper 与格式读取依赖实际安装在原持久环境内，Main 与 Manager 调同一文件，Host 不直接连接用户可改的图库 cache。仅更新 Bot 镜像或给未来环境 Dockerfile 增 COPY 都不会升级现有 rootFS；部署须把当前 helper 复制到原容器 `/opt/yuki-runtime/`，验证原终端能运行并保留已有安装、文件、挂载和服务。已有checkpoint部署还须在Manager停止、确认无活动执行时，对装好helper的原容器制作私有完整rootFS快照，并离线沿现有 `set_setting` 更新 `checkpoint_image`；否则未来按旧checkpoint重建会丢helper。仅安装helper不要求重建环境；启用新的媒体只读挂载则须明确替换一次，home/runtime仍为原bind；清理同时保当前容器镜像和实际选中checkpoint。保留原 systemd 与仍有效覆盖，Supervisor 沿原只读源文件挂载；删除退役 ACP 配置，不再增加 peer 挂载或环境 SDK 安装。不能只更新 Bot 后声称环境已获得新代码。

图库原图与 JSON 是唯一资料源，`search.sqlite3` 可删重建。Main 可直接执行：

```sh
python /opt/yuki-runtime/sticker_index.py --workspace /workspace rebuild --platform qq
python /opt/yuki-runtime/sticker_index.py --workspace /workspace update stickers/qq/index/<asset-id>.json
python /opt/yuki-runtime/sticker_index.py --workspace /workspace search --platform qq '捂嘴偷笑'
```

把示例资产 ID 换成真实文件名。去掉 rebuild 的 `--platform` 可整理现有各平台目录。普通文件操作不要求经过 helper；编辑后新标签的搜索生效点是 update/rebuild 完成，不用父目录 mtime 猜测。helper 不需要 Bot 数据库或管理凭据，不调用模型、发送消息或重写历史。首次索引未完成不能当无候选，JSON 和文件仍自由编辑。

旧环境须单独安排一次明确更换：

1. 记录原容器 ID/启动时间、网络/挂载、Manager 当前检查点与活跃运行。可先让工作完成；若主动停环境，按真实代际中断处理，不自动重跑普通任务或未知安装。
2. 停 Manager 和原环境，在同一停写窗口一致备份 `jobs.sqlite3`、home/runtime、artifact/short_state 和 Bot 原回执库；保存原环境完整 rootFS 的私有 Docker commit 镜像。commit 不含 bind mount，家目录和运行记录必须另备份。
3. 保留原 HOME、运行文件和旧镜像，归档已停原容器。离线将原 `environment_state.checkpoint_image` 指向此次完整 rootFS 快照，不改原 run/request/hash、completion、服务或预算。Manager 优先此值，单改 `--image` 不覆盖已有检查点。
4. 更新 Manager/防火墙，由原 Manager 创建唯一活动环境：同一 HOME/runtime、同一 execd、`--user 0:0`、当前只读 Supervisor、公网 bridge。创建显式覆盖旧镜像用户/代理默认值，保原 HOME 路径与 apt/pip/npm 安装。不得启动第二活动环境共享家目录，不得用空基础镜像替代原 rootFS。
5. 核实实际 UID/HOME、旧文件/安装、Supervisor 挂载、原回执、服务恢复和公网/私网边界；另记 Bot health、固定工具与自然流量。镜像本地构建并校验传输，不重启 Docker、QQ、代理或无关同机服务。

rootFS 快照的镜像配置可能含原 execd secret，应作为私有运维备份保管，不公开推送或输出。回退保留最新消息、家目录、运行记录和回执库，不能用旧整库覆盖新事实。仅回退 Bot 时可保环境运行；实际环境身份/网络更换必须查容器和数据，不能以 healthy 替代。

## 定向验收

`tests/unit/test_terminal_environment.py` 覆盖原 Manager SQLite、旧 APT 不重放、无经验计数准入、原 rootFS 选择、网络规则 shell 脚本，以及隔离原生 Supervisor 的 PTY、输入、文件、后台和显式超时。工作区桥与 Work receipt 回归另覆盖发布、媒体和 owned/external run。这些不冒充实际 gVisor、管理员安装或公网测量。

`tests/unit/test_legacy_acp_receipts.py` 建立真实旧 SQLite/文件回执，验证原终态 outbox、精准取消且不关闭 peer、拒绝旧 open／prompt 新接纳。`tests/unit/test_external_agent_work.py` 核固定工具与 Code 投影、原效果恢复，以及监督不阻止 Work 完成。这些不代表生产上线、供应商执行或真实缓存命中。

`deploy/sandbox/validate_runtime.py` 仅对显式隔离 socket 做真实终端/发布探针。部署还须记录管理员身份、原安装保留、execd 重连、包管理、服务、网络和资源行为。不发真实 QQ 测试消息，不从声明缩小推算 cache/费用。

[2026-09-12 记录](https://github.com/YuanYeYouTao/Yuki/blob/v3.9.0/docs/operations/persistent-environment-validation-20260912.zh-CN.md)仅是旧部署/恢复点，不是本次 root、网络与工具合同已上线的证据。
