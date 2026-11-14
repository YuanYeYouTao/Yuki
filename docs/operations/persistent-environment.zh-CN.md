# Yuki 持久工作环境

[English](persistent-environment.md)

Yuki 全局共用一个 Linux 环境、`/home/yuki` 和 `/workspace`，不按人物或群划分，普通持久文件不自动过期。`short_state` 仍是独立、有期限的短提示；目录、文件和完整执行日志不自动载入上下文。Bot 的 `./workspace:/app/workspace` 是 artifact/short_state 库；终端文件在 Docker 卷 `yuki-sandbox-home` 的 `workspace/`，环境内为 `/workspace`。工作文件在发送时冻结为不可变交付快照，入站附件从共享缓存直接复制到普通工作区。

所有会话的临时媒体共用 `/inputs/media-cache`，它是 Bot 原缓存目录的同一份只读挂载，不在 2 GiB HOME 中再复制一份。Bot 仍是唯一写入和到期清理 owner；媒体到期后由原清理器删除，普通工作文件、已收藏表情和不可变 artifact 不随它清理。Main 可直接用终端读取、复制到 `/workspace` 或表情目录自由编辑，也可用`workspace_inspect` 选读或 `send(path="/inputs/media-cache/...")` 发送；Host 复用原安全 FD 桥，不跟随模型写的链接访问宿主文件。临时媒体的全局共享由用户明确授权，不再声称终端读取受当前 Conversation 限制。

## 运行时与工具

Compose 服务 `manager` 管理常驻容器 `yuki-environment`，标签为 `io.yuki.sandbox=persistent-v1` 与所属 Compose 项目 `io.yuki.sandbox.project`。Manager 与 Bot 用同一镜像、同一版本，经挂载的 docker.sock 控制 Docker，在宿主网络上设置环境的防火墙；Bot 只经 `manager.sock` 访问它。execd 固定为 `441042c288a3eacbe93b36236a9baf6f6254ad4d`，镜像保留 Apache 许可证。终端和服务共用 execd PTY 与原 Supervisor，没有第二套安装执行器或 sudo 审批。

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

新 Python、Node 和系统包工作均使用终端。`run_python`、`environment_packages`、`get_code_run`、`cancel_code_run` 和 `terminal_write` 不再接受新调用。旧 Python/APT 任务仍按原运行编号由 `terminal_read` 读取、由原 completion outbox 确认。升级不会启动旧未派发 APT，也不会重放已经派发但结果未知的安装。不再新建安装预算或 dpkg 收集；整台电脑的还原点见下文[整机快照与还原](#整机快照与还原)。

Manager RPC `environment_service` 保留登记、启动、停止、删除、状态、日志和真正的跨环境启动恢复，沿原 `restart` 策略执行，供运维登记系统服务（如 runsc 下的 `sandbox-dns`）；模型工具 `environment_service` 已退役。显式停止先禁用恢复；没有服务数量或连续失败次数封顶。修改登记保留正在运行的原执行，下次实际启动采用新内容。

普通文件读写、搜索、目录、修改和删除使用终端。八项普通 `workspace_*` CRUD、公开 artifact 目录/文本分页/删除及普通覆盖 CAS 全链退出，不保留新调用别名。`workspace_inspect` 仍把指定媒体交给模型；`send(path=..., attachment_kind=...)` 内部固化不可变 artifact 并交付，模型侧不再提供发布工具。收藏与附件保存直接从只读缓存流式复制到最终文件，不留下永久中转快照。会话附件提升、私有工具 artifact 和真实媒体桥保留；终端可经共享只读挂载读取临时媒体原件，Bot 私有 artifact 索引不挂载。已发布版本不随后续文件修改变化。

artifact manifest 使用独立 SQLite。文件准备、SHA256 和 fsync 在写事务外完成；短写事务核验真实 revision/不可变事实并提交 metadata。读取先安全打开原文件 FD，再释放读取事务并校验内容。发布异常只回收私有 pending 文件；结果未知时不删除已 rename 的 blob，留给按真实引用工作的原 GC。普通发布不设 200 MiB、512 MiB 或对象数量经验门槛；模型媒体与平台传输仍受其真实容量限制。

### 外部 Agent 与原环境分离

external_agent 已移出环境工具和 Manager 新任务路径。用户电脑上的原生 Agent 经独立连接器接入 ExternalAgent 服务，不再通过服务器／workspace 启动 Codex、Claude 或 ACP 子进程。普通终端、文件和环境服务沿原 Manager 运行，设备离线不影响这些工具。

升级时删除原 Manager ExecStart 中的 --acp-config 参数和部署环境中的 YUKI_MANAGER_ACP_CONFIG。新环境不安装 ACP SDK、不挂载 acp_peer.py，也不安装供应商 adapter 充当用户电脑。现有 rootFS、用户安装和原进程不会因源码升级被自动删除或重建。

历史 server ACP 的 request／run／peer／permission／cursor／completion 保留原事实。legacy reader 只查询原活动、接纳精准取消／原权限回答并结算，不接纳新 peer 或 prompt。旧活动未决时保留未决，不批量重置 Work、不重发付费操作。既有旧挂载可留到容器真实重建，不为删除一项挂载强制中断全部终端。原生会话、用户登录和供应商侧历史不由任务归档代为删除。

## 她的电脑：快照、自启动与节律

这台环境是 Yuki 自己的电脑：她布置、安装、改动，坏了能整体回到一个时间点。设计与验收见[任务书](../architecture/Yuki自己的电脑任务书-2026-11-14.md)。

### 整机快照与还原

快照的单位是整台电脑：容器 rootFS（`docker commit`，含 apt 装的软件、`/etc`、`/root`）与 HOME 卷（`workspace/`、Chromium 资料与登录、收藏的表情、`.config` 等）在同一次 `docker pause` 内保存，作为一个时间点。HOME 排除 `inputs/media-cache/`（另一卷）和 Chromium 的 `Cache`、`Code Cache`、`GPUCache`。不包含 Bot 的记忆、会话、主库与已交付 artifact，也不包含 Manager 状态、运行记录与代码卷；还原不回滚这些，也不回滚网站侧已经发生的事。

- rootFS 存为镜像 `yuki-environment-snapshot:<id>`（标签 `io.yuki.snapshot`、`io.yuki.sandbox.project`）；HOME 用 `rsync -aHAX --numeric-ids --delete --link-dest` 复制到外部卷 `yuki-sandbox-snapshots` 的 `<id>/`，未变化的文件与上一份硬链接。HOME 先在环境运行时复制一遍，暂停内只再同步差异，所以暂停主要是 `docker commit` 的时间。副本改名就位后写完成标记 `<id>.complete`，没有标记的副本不会被还原。运维的 `YUKI_SANDBOX_RESOLV_CONF` 文件不进快照、还原也不回滚它；运维登记服务（如 `sandbox-dns`）放在 HOME 里的其他文件会随 HOME 一起回滚。快照卷**只挂给 Manager**，环境与 Bot 都没有它，环境内进程不能创建、删除或还原快照。
- 记录在 Manager `jobs.sqlite3` 的 `environment_snapshots`（`request_id` 唯一）与 `environment_restores`。同一请求键（工具调用键或桌面端请求）返回原记录或当前进度；一次只进行一份快照或一次还原，另一份请求得到 `snapshot_in_progress`／`restore_in_progress` 与原请求 ID。
- 三种来源：Yuki 的 `computer(action="snapshot"|"restore")`、本人桌面端（Client API `POST /computer/snapshots`、`/computer/restore`）、Manager 每日检查。快照与还原在 Manager worker 中串行执行，期间截图与输入动作排队，新的终端执行照常入队、快照结束后派发。暂停期间已运行的命令冻结后继续，显式 `timeout_seconds` 按墙钟可能提前到期。
- 新快照就绪后才轮换最旧的一份，保留 `YUKI_SANDBOX_SNAPSHOT_KEEP` 份（至少 1 份；`checkpoint_image` 指向的那份永不轮换）：先在与新快照就绪同一事务里记为 `rotated`（此后不再受理还原），再删完成标记、副本与镜像标签；Manager 中途结束时由恢复清理残留；创建失败（如磁盘写满）删除自己的部分产物，不动任何旧快照。正在运行的容器所基于的镜像 Docker 拒绝删除，标签留到下一次重建后由恢复清理。
- 每日：距最新一份快照满 `YUKI_SANDBOX_SNAPSHOT_INTERVAL_HOURS`（且距上次检查也满）时检查 HOME（Manager 一侧）与 rootFS（环境内跳过所有挂载点）有没有 ctime 晚于上一份快照的条目；有变化才拍，没有只记检查时间、不占名额。本人持有控制权时推迟到交还后。没有任何快照时视为有变化，所以升级后 Manager 就绪即拍第一份。
- 还原：写入 `environment_restores` 即接纳；停容器之前先确认快照仍为 `ready`、HOME 副本完整、镜像存在，任一不满足就记 `failed`（`snapshot_not_found`／`snapshot_incomplete`／`snapshot_image_missing`），原电脑不动。随后停止并删除原容器，`rsync --delete` 把该快照的 HOME 写回（媒体缓存与 Chromium 缓存不动），以该快照镜像经原 `recover()` 创建新环境，`apply_programs()` 按当前档位补齐软件包（不按快照档位改配置）。旧代际已启动的执行结束为 `failed`、`interrupted`、`environment_restored`（附 `snapshot_id`），完成回执照常送到原 Work，不重放；未派发的执行在新环境里启动。Manager 中途结束时 `recover()` 继续收敛同一快照，HOME 写回完成前绝不启动环境；写回本身失败（如磁盘写满）时还原记 `failed`（`restore_home_partly_written`），环境以该快照镜像重建，可以再还原一次。只有容器确实重建后还原才记 `done`；`docker inspect` 的瞬时错误不被当成容器不存在。
- `checkpoint_image` 始终指向最新快照：容器丢失而 HOME 还在时，只从最新快照重建 rootFS，HOME 保持当前，`environment_status.recreated_from` 报告来源快照与时间点。
- Manager 在暂停中结束：`recover()` 先 `unpause`，未完成的快照记 `failed`（`interrupted`），删除 `.partial` 目录与已打的标签。runsc 下暂停期间整环境 OOM 时，这份快照记 `failed`，旧快照不动。

`environment_status`（模型 `get_environment`）列出快照（状态、来源、备注、各部分大小、暂停秒数、轮换掉的那份）、保留份数、快照卷占用、最近一次还原与 `recreated_from`。大小与磁盘占用随档位和使用而定，计入 Docker 与 Bot 共用的磁盘；创建时会短暂存在多一份。

### 环境级自启动

`/etc/rc.local`（rootFS，随快照保存）在每个环境代际恰好运行一次：Manager 以 `autostart:<代际>` 为请求键提交一次 Supervisor 服务执行，输出写 `/var/log/rc.local.log`，末行为真实退出码；脚本用 `&` 起的进程在脚本结束后由 docker-init 收养继续运行。Manager 重启（环境未重启）不再运行；环境重启（runsc OOM、还原、VM 重启）后再运行一次。同一代际内想立即生效就在终端直接运行它。运维登记的 `sandbox-dns` 等系统服务不受影响。

### 节律：闲置休眠

画面层（`desktop` 的 Xvnc、她的会话、Chromium 与驱动；`headless` 的无头 Chromium 与驱动）闲置 `YUKI_SANDBOX_DISPLAY_IDLE_MINUTES` 分钟（默认 30，`0` 不自动休眠）后由 Manager 停止，回执键 `idle:<桌面服务 run_id>`，附休眠时开着的标签页及其 Work。"在用"是：对自己环境的任一 `computer`／`browser` 调用、任何一路画面流（含桌面端紧凑预览；桌面端窗口隐藏或最小化时断开画面流）、本人持有控制权、每次唤醒。终端命令不算，休眠不影响终端、后台进程与 `rc.local` 起的守护进程；用 `DISPLAY=:1` 起的图形程序随显示器退出。计时只在 Manager 内存，Manager 重启从启动时刻计。下次 `computer`／`browser` 自动唤醒，Chromium 以 `--restore-last-session` 恢复标签页（tab id 改变，旧 id 得 `tab_not_found` 附当前清单）。在终端里用脚本直接驱动浏览器（经 CDP 或 `yuki-computer`）不算在用，长脚本可能被闲置休眠打断；需要时把闲置分钟调大或设为 0。Yuki 也可自己 `computer(action="sleep"|"wake")`；本人持有控制权时 `sleep` 返回 `control_held`。`lite` 没有画面层，`sleep`／`wake` 返回 `browser_unavailable`。

## 恢复、资源与网络

Manager 在派发前持久化原请求、执行意图和 execd 会话标识。断线后先检查原生退出记录，否则接回原会话；原 `started` 标记阻止再次执行。PTY 暂未运行、握手超时、旧安装缺 heartbeat 都不能按年龄推断失败。提交未知时查原请求，不盲目重提。Bot 持久化完成回执后才确认 outbox；丢确认或重复读取不重开命令、不重置预算。

容器 ID 与启动时间共同组成环境代际。Bot/Manager 重启保留原容器和进程；实际环境重建后，旧普通任务记录真实中断，登记服务按策略恢复。容器 restart policy 为 `no`，由 Manager 完成自身检查后启动。升级 Manager 不会暗中删除或重建原环境。

资源按档位给初值（见下文），每项可在 `.env` 单独覆盖。这些是部署资源，不是任务接纳评分；容量、进程和 OOM 结果按真实 OS/Docker 事实返回。用 runc 时超出内存只结束超限的进程；用 runsc（gVisor）时整个环境退出，按真实代际中断处理。每任务保留两个 4 MiB 日志段；回收已完成日志和七天前已确认的运行文件，SQLite 执行与完成回执独立保留。

HOME、运行记录、Manager 状态、代码文件、快照与媒体缓存是 Docker 命名卷，没有单独的容量上限，与 Docker 和 Bot 数据共用磁盘；`environment_status.storage` 报告这块共享磁盘的容量与剩余（`shared_disk: true`）。环境写满磁盘会让 Bot 写库失败，按真实错误报告。

环境接入 `yuki-sandbox-egress` bridge（`yuki-egress0`，关闭 IPv6），不存在时 Manager 创建。Manager 在 Docker 使用的 iptables 后端（含 DOCKER-USER 链的那个）中设置：环境不能主动访问宿主、私网、链路本地及其他非公网 IPv4 目标；普通公网 TCP/UDP（包括 SSH）不按端口白名单限制；只允许回应 Manager 自己的 execd 连接。Docker 改用 nftables 防火墙后端（没有 DOCKER-USER）时 Manager 报 `network_guard_unsupported`，不创建环境；Manager 不在宿主网络（如 rootless Docker）时报 `network_guard_missing`。环境不挂 Docker socket、Bot 配置/数据库、QQ 凭据或私有 artifact 索引。`YUKI_SANDBOX_PROXY` 可为环境内程序设置 HTTP 代理，默认不设。

Docker 自定义 bridge 的 `127.0.0.11` DNS 无法由 gVisor 访问。用 runsc 时把 HOME 内一个文件（如 `.config/resolv.conf`，环境内 `/home/yuki/.config/resolv.conf`）写好可达的 DNS，并设 `YUKI_SANDBOX_RESOLV_CONF=.config/resolv.conf`，Manager 创建环境时把它挂到 `/etc/resolv.conf`。runc 直接使用 Docker 的 DNS。宿主走 fake-ip 代理（如 Clash/mihomo TUN）时，公共 DNS 会返回 `198.18.0.0/15` 的假地址，而这一段按非公网被防火墙丢弃；这时让环境使用能返回真实地址的解析器（例如环境内的本地 DNS 服务）。见 [gVisor DNS 说明](https://gvisor.dev/docs/user_guide/faq/#dns-doesnt-work-on-a-docker-user-defined-bridge-network-including-docker-compose)。

所有 Main 入口、自动化和子 Agent 使用固定能力合同。工具目录不修改已提交声明，环境管理员能力不扩大人物、发送、委托或插件领域授权。可选 Code Mode 的 Monty 仍是无文件系统/网络的编排环境，不能当作持久终端别名。

## 安装、档位与升级

### 安装

`install.sh`（Windows 为 `install.ps1`）一次完成：下载并校验发布包，首次配置角色与模型，选档位，确定运行时，创建数据卷（`deploy/sandbox/prepare.sh`），拉取镜像，`docker compose up -d`，再以 Bot 的用户运行 `yuki setup environment-check` 等环境就绪。宿主只需要 Docker 与 Compose；不再安装 systemd unit、venv、loop 盘或同步脚本。

```sh
sh install.sh --dir ~/yuki                       # 交互
sh install.sh --dir ~/yuki --yes --profile lite --answers model.json   # 无交互
```

`model.json` 与 Desktop 的配置输入相同：`{"protocol", "base_url", "api_key", "model"}`。在同一目录再运行一次即升级：替换发布文件，保留 `.env`、数据与 `docker-compose.override.yml`（自定义改动放这里）。卷是 external，`docker compose down -v` 不会删除它们。一台 Docker 只运行一个 Yuki 沙箱：另一个 Compose 项目的 Manager 遇到不属于自己的环境时报 `environment_name_in_use`，不接管。

### 档位

| 设置 | `lite` | `headless` | `desktop` |
| --- | --- | --- | --- |
| 环境镜像 | `yuki-environment:<版本>` | `yuki-environment:<版本>-desktop` | `yuki-environment:<版本>-desktop` |
| 浏览器 | 无 | 无头 Chromium | Chromium + Xvnc 桌面 |
| 内存 / 额外 swap | 512 / 128 MiB | 1024 / 256 MiB | 3072 / 512 MiB |
| CPU / 进程数 | 1 / 256 | 1.5 / 1024 | 2 / 2048 |
| /tmp / /dev/shm | 128 / 64 MiB | 256 / 128 MiB | 512 / 256 MiB |

安装器按 Docker 报告的内存推荐（低于 2 GiB `lite`，2–4 GiB `headless`，更多 `desktop`），由用户选，不拦截；Desktop 本机部署默认 `desktop`。`computer`、`browser` 工具三档都声明：`lite` 返回 `browser_unavailable`，`headless` 下 `computer` 返回 `display_unavailable`。`environment_status` 报告档位、模式、运行时、安装结果与实际限额。

换档：改 `.env` 的 `YUKI_SANDBOX_PROFILE`，再 `docker compose up -d`。换大时 Manager 在原 rootFS 上安装所需 Debian 包（结果写入 `programs`，失败保留原安装、下次启动重试）；换小只改模式与资源，已装软件和浏览器资料保留。正在运行的桌面服务以新运行按新模式重启。内存、swap、CPU、进程数用 `docker update` 即时生效，Docker 拒绝时原文写入 `limits_update_error`；/tmp 与 /dev/shm 只在明确重建时生效，未生效项列在 `recreate_required`。

运行时 `YUKI_SANDBOX_RUNTIME` 在首次安装时按 Docker 是否注册了带 `--overlay2=none` 的 runsc 写成 `runsc` 或 `runc`，之后保持，不会隐式从 gVisor 换成 runc。配置 runsc 但 Docker 没有时报 `gvisor_unavailable`；runsc 缺 `--overlay2=none` 时报 `gvisor_overlay_unsupported`（否则环境内安装不进 Docker 层，检查点会丢软件）。

### 代码文件

环境内 Yuki 自己的程序（Supervisor、表情索引、桌面服务、浏览器驱动、`yuki-computer` 等，见 `persistent.ENVIRONMENT_FILES`）不在环境镜像里。Manager 每次启动把本版本的文件写入 `yuki-sandbox-code` 卷，环境把每个文件只读挂到原路径。原地改写保持 inode，运行中的环境与旧 rootFS 检查点都随 Bot 升级拿到当前代码，不需要重建。

### 状态与恢复

execd token 与执行回执在 `yuki-sandbox-state` 的 `jobs.sqlite3`。状态卷丢失而环境仍在时，Manager 报 `manager_state_missing`，不生成新 token、不接管。Manager 恢复失败时退出并在日志写原因，由 `restart: unless-stopped` 重试；Bot 照常运行，终端类工具返回环境不可用。

### 从宿主 Manager 迁移

旧部署（systemd、loop 挂载的 ext4、宿主 venv）用 `deploy/sandbox/migrate-host-manager.sh` 显式迁移一次，安装器在检测到旧环境时拒绝并指向它。脚本在 Docker 宿主以 root 运行：确认没有活动执行，停止并禁用宿主 Manager，把原环境 rootFS `docker commit` 为私有检查点（runsc 缺 `--overlay2=none` 时先按 [Mac 迁移记录](mac-arm64-migration-20261107.md) 导出 upper 层），把 HOME、运行记录、媒体缓存和 `jobs.sqlite3` 复制进对应卷并指向该检查点，删除旧 Squid、内网 bridge 与相关防火墙规则，把原配置写入 `.env`。原 loop 镜像、数据库和容器（改名为 `yuki-environment-retired`）原样保留；`docker compose up -d` 并核对终端、文件、服务与网络后，再以 `--cleanup` 删除它们。

检查点镜像可能含原 execd secret，作为私有备份保管，不公开推送。

### 媒体快照清退

旧中转快照清退使用 [离线脚本](../../scripts/retire_media_snapshots.py)，在完整停写副本上审计；默认只输出候选，`--apply` 只清退确证无引用、且普通文件仍有相同字节的快照。

## 定向验收

- `scripts/sandbox_compose_e2e.py`（CI `sandbox-compose`）：用真实安装器装 `lite`，经 Bot 的用户调用 Manager，验证终端、公网可达而宿主与私网不可达、`lite` 浏览器不可用、重启 Manager 保留环境与原执行、`down -v` 保留数据，再换到 `headless` 在原环境上安装浏览器并打开页面。产物 `test-results/sandbox-compose/steps.json`。
- 同一 CI 作业（runc 与 runsc 两路）由 `scripts/sandbox_snapshot_e2e.py` 在 `lite`、`headless` 与第三阶段 `desktop` 各自验证 `rc.local` 自启动、快照、还原、同键重试、轮换、容器丢失后重建、每日检查、Chromium 资料随 HOME 还原、闲置休眠与唤醒、会话自启随电脑还原；产物 `snapshots-<档位>.json` 与 `desktop/` 截图。
- `tests/unit/test_environment_snapshots.py`、`tests/unit/test_computer_lifecycle.py`：任务书 §11 的快照、还原、恢复与生命周期决定（Docker 由模拟引擎应答）。
- `tests/unit/test_sandbox_profiles.py`：状态卷丢失、项目归属、运行时检查、换档安装与失败、即时限额与需重建项。
- `tests/unit/test_terminal_environment.py`：Manager SQLite、旧 APT 不重放、原 rootFS 选择、卷挂载、防火墙规则与后端选择，以及隔离原生 Supervisor 的 PTY、输入、文件、后台和显式超时。
- 有头桌面见 `tests/integration/test_computer_browser_e2e.py`（CI `desktop-headed`）。

这些不冒充生产上线或供应商执行。
