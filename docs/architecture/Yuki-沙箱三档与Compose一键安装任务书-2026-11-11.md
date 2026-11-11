# 沙箱三档与 Compose 一键安装任务书

日期：2026-11-11。源码基线：`main` `c304e6b7`。对应 issue #524 未完成部分。

实施前阅读[共同开发约束](development-contract.md)、[持久工作环境](../operations/persistent-environment.zh-CN.md)、
[桌面与浏览器](../operations/desktop-computer.zh-CN.md)、[Desktop 本机部署](../operations/desktop-local-deployment.zh-CN.md)、
[Mac ARM64 迁移记录](../operations/mac-arm64-migration-20261107.md)。

## 1. 结论

用户决定：

- 沙箱分三档：`lite`（服务器低配）、`headless`（无头浏览器）、`desktop`（有头桌面）。
- 走 Compose 路线：Manager 成为 Compose 服务，与 Bot 用同一镜像、同一版本，
  不再在宿主装 systemd unit、venv、loop 盘和同步脚本。
- gVisor 可有可无：Docker 注册了 runsc 就用，没有就用 runc，安装器不负责安装 gVisor。

目标：装好 Docker 后，一条命令完成选档、拉镜像和启动；服务器与本机（Colima、Docker Desktop）
走同一套 Compose。选配机制在 #567 已有，本任务让它可用，并删除压在前面的宿主安装链。

本稿已经过一轮对照源码的独立复审，第 4 节的 P0 结果是 2026-11-11 在本机 Colima `yuki` VM
（Docker 29.5.2、内核 6.8、arm64）上用一次性探针容器实测的，未触碰线上环境。

## 2. 现状与问题

#567 已有：浏览器模式 `none/headless/headed`、预装包、内存/swap/CPU/PID/tmp/shm、
两块持久盘容量，以及在原 rootFS 上增量切换的 `install-desktop.sh`。未设置时 Manager 默认 512 MiB、1 CPU
（`persistent.py:233-234`）。

问题：

1. 没有档位，十几个变量要手工合进 `/etc/yuki-sandbox/deployment.env`，示例只有 3 GiB 有头一档。
2. 环境镜像不发布，只能在宿主用 `compose.build.yaml` 本地构建。
3. `install.sh` 只做配置、不启动服务（`install.sh:128-135`）。Manager 要手工装 Python 3.12 venv、runsc、
   出口网桥与防火墙、ext4 loop 存储单元、`install-manager.sh --bot-container`，只支持带 systemd 的 Linux。
4. Desktop 本机部署不装 Manager，本机没有终端、浏览器和桌面。
5. `install.sh` 默认下载 4.0.0，GitHub 最新 release 是 3.9.0，一键安装当前直接 404。

## 3. 参考：Memoh

`felinics/Memoh`（原 memohai/Memoh）只发布一个约 640 MB 的工作区镜像，自带 XFCE、TigerVNC、Chromium，
桌面在首次使用时才启动；选配在 Web 管理界面按 bot 设置（镜像、桌面开关、CPU/内存/磁盘、GPU），
改资源要重建工作区。安装是 `curl … | sh`：交互问目录和密钥，拉多架构镜像后 `compose up`，
再运行即升级。它安装简单，是因为 server 容器 `privileged` 并内嵌 containerd，宿主只需 Docker。

借鉴：预发布多架构镜像、一条命令安装和升级、桌面懒启动、宿主只要 Docker。
不照搬：按开关增删工具（违反固定工具合同）、单一大镜像（低配服务器不需要 Chromium）、不分档。

## 4. P0：已实测与待实测

已实测（Colima `yuki`）：

| 项目 | 结果 |
| --- | --- |
| 宿主网络 + `NET_ADMIN` 的容器读写宿主防火墙 | 可以。容器内 `iptables v1.8.10 (nf_tables)` 能 `-C` 到现有 `INPUT -i yuki-egress0 -j DROP`、读 `YUKI-EGRESS` 链，新建并删除测试链成功；VM 的 Docker 同用 nft 后端 |
| `docker update` 调内存/CPU/PID | runc 与 runsc 都即时生效：128 MiB 起建、调到 400 MiB 后都能分配 220 MB。runsc 容器内 `MemTotal` 仍显示创建时的值，限额以宿主 cgroup 为准 |
| 内存超限的影响范围 | **runc 只杀超限的那个进程，容器继续运行；runsc 整个环境退出**（`OOMKilled=true`，其他进程一起结束）。gVisor 下一个命令超内存会中断全部终端和服务 |
| 命名卷子路径挂单个文件（`volume-subpath`，Engine 26+） | runc 与 runsc 都可以，只读挂载生效；可用于把代码文件挂到环境内原路径、把 HOME 内的 `resolv.conf` 挂到 `/etc/resolv.conf` |
| 当前本机有头环境 | runsc，`--overlay2=none`；Chromium 运行中（9 个进程）+ Xvnc，`docker stats` 451 MiB / 2 GiB（单次读数） |

待实测：

- Docker Desktop（Mac、Windows WSL2）上宿主网络容器能否写防火墙规则、能否连到网桥上的容器 IP；
  Docker 用 nftables 防火墙后端（没有 DOCKER-USER）时的行为。
- 三档在同一页面负载下的内存：空闲、打开一个页面、看 VNC 画面。用于修订第 5 节初值。
- 两个环境镜像的实际大小。
- 服务器部署的实际 `deployment.env`：是否用 `--proxy` 指向 Squid、是否用了 `connect-host-proxy.py`
  的宿主代理中继。决定第 8 节 Squid 能否删除。

## 5. 三档

档位只决定环境镜像、浏览器模式和资源初值，每个值都可在 `.env` 单独覆盖。数值是初值，
P0 实测后修订；它们是部署资源，不是安装门槛或任务接纳条件。

| 设置 | `lite` | `headless` | `desktop` |
| --- | --- | --- | --- |
| 环境镜像 | `yuki-environment:<ver>` | `yuki-environment:<ver>-desktop` | `yuki-environment:<ver>-desktop` |
| 浏览器模式 | `none` | `headless` | `headed` |
| 内存 / 额外 swap | 512 / 128 MiB | 1024 / 256 MiB | 3072 / 512 MiB |
| CPU / PID | 1 / 256 | 1.5 / 1024 | 2 / 2048 |
| /tmp / /dev/shm | 128 / 64 MiB | 256 / 128 MiB | 512 / 256 MiB |

**两个镜像**：`-desktop` 镜像装全部有头包（`install-programs.sh headed`：Chromium、字体、TigerVNC、Openbox、
socat、xdotool、x11-utils、imagemagick），`headless` 与 `desktop` 共用。是否值得为 `headless` 单独发镜像，
等 P0 镜像大小再定。

**模式改为运行时设置**：

- `.env` 的 `YUKI_SANDBOX_BROWSER_MODE` 由 Manager 写入 runtime 卷，`desktop_service.browser_mode()` 读它。
- manifest 只记录实际安装的软件与版本，不再写 `desktop` 与 `browser_mode`。
- Manager 端所有按 manifest 判断的地方改为按"当前模式 + 实际安装"计算：
  - `computer_runtime.py:49-53, 96-100, 278-284`；
  - `client_api/capabilities.py:34`（Desktop 电脑页）；
  - `environment_status`（`persistent.py:1130`）。
- 否则共用镜像会让 `headless` 档误报有桌面。
- 改模式时，正在运行的 `yuki-desktop` 服务要以新运行重启，旧运行按原回执结束。

**工具声明不变**：`computer`、`browser` 三档都声明。`lite` 下返回 `browser_unavailable`，`headless` 下
`computer` 返回 `display_unavailable`。环境说明和 `environment_status` 写明档位、模式、运行时和实际限额。

**推荐，不拦截**：安装器显示 Docker 报告的内存并推荐档位：低于 2 GiB `lite`，2–4 GiB `headless`，更多 `desktop`。
Desktop 本机部署默认 `desktop`。

**换档**：改 `.env` 后 `docker compose up -d`。

- 升档：Manager 用 `docker exec` 在原 rootFS 上运行 `install-programs.sh` 增量装包。
  所需的程序文件（`install-programs.sh`、`desktop_service.py`、`yuki_computer.py`、`cdp.py`、`yuki-computer`）
  从 `sandbox-code` 卷以子路径只读挂到原路径。lite 镜像里没有这些文件，这一步取代现在的 `install-desktop.sh`。
- 降档：保留已装软件和浏览器资料，只改模式和资源。
- 内存、CPU、PID 用 `docker update` 即时生效（P0 已测）：
  - 调到低于当前用量时，先停桌面服务，再如实报告 Docker 的结果；
  - 调整后刷新 `container_at_recovery`，不只在 recover 时读。
- /tmp、/dev/shm 只能在明确重建时生效，状态里分开列配置值与观测值。

## 6. Compose 架构

```
docker-compose.yml
  bot      ghcr.io/yuanyeyoutao/yuki:<ver>（原 start.sh，UID/GID 沿原配置）
           挂 sandbox-run 卷（manager.sock）、media-cache 卷
  manager  同一镜像，entrypoint: python -m yuki.sandbox.manager
           user 0:${YUKI_RUNTIME_GID:-10001}，umask 0007，init: true，stop_grace_period 长
           network_mode: host，cap_add: NET_ADMIN，restart: on-failure
           挂 /var/run/docker.sock、sandbox-state、sandbox-home、sandbox-runtime、
           sandbox-code、sandbox-run、media-cache、./workspace
外部资源（安装器创建，Compose 中 external: true）
  网络 yuki-sandbox-egress（网桥 yuki-egress0）
  卷   sandbox-state / sandbox-home / sandbox-runtime / media-cache
yuki-environment  由 manager 经 docker.sock 创建，不属于 Compose 项目，restart=no
```

**同一镜像**：

- Manager 必须有自己的 `entrypoint`，因为镜像的 `start.sh` 会忽略参数去启动 Bot（`Dockerfile:72`、`scripts/start.sh:35-36`）。
- 删除 `sync-manager.py`、`releases/<提交>/src`、`/opt/yuki-sandbox/venv`、`requirements-manager.txt`。
- runtime 镜像加入 docker CLI、iptables（含 legacy 与 nft 两个后端）和 `network-guard.sh`；Bot 容器不挂 docker.sock。
- 叠加文件（`docker-compose.local.yml` 的 `YUKI_LOCAL_IMAGE`、`docker-compose.codemode.yml`）必须同时覆盖
  `manager` 的镜像。`desktop-image.yml` 发布 `desktop-<sha>` 时同时发布对应的环境镜像。

**权限**：

- Manager 以 root 用户、Bot 的组运行，umask 0007，等同现在 systemd 的 `Group=10001`、`UMask=0007`。
  这样 socket、`manifest.sqlite3`、blob 和 `.artifact.lock` 对 Bot 可用。
- Desktop 本机部署把宿主 UID/GID 传给 Bot，所以组号和 `deployment_check` 的期望 UID 都从配置取，
  不再写死 10001（`deployment_check.py:29,35`）。

**网络**：

- Manager 用宿主网络，与现在宿主进程位置相同；启动时执行 `network-guard.sh`。
- 后端选 Docker 实际使用的那个：哪个后端里有 DOCKER-USER 链就用哪个。都没有（Docker 的 nftables 后端）时报
  `network_guard_unsupported`。
- `modprobe` 与 `sysctl` 不能在容器里做。`br_netfilter` 未加载或 `bridge-nf-call-iptables` 不是 1 时，
  报 `network_guard_missing` 并给出宿主命令，不尝试加载内核模块。
- 出口网络是 external，`docker compose down` 不会删它，也不会因为环境还连着而报错。

**卷与路径**：

- Manager 经 docker.sock 创建环境时只用 `type=volume`，不传 Manager 容器内路径。
- `sandbox-code` 卷由 Manager 每次启动时写入本版本的 `environment_supervisor.py`、`sticker_index.py`、`media.py`
  及桌面程序，再用 `volume-subpath` 只读挂到环境内原路径。路径不变，旧 rootFS 也拿到当前代码，
  已持久化的服务命令不用迁移。
- `resolv.conf` 仍放在 HOME 内的原位置，用 `volume-subpath` 挂到 `/etc/resolv.conf`。runc 能用 Docker 内置 DNS，
  只有 runsc 需要它。
- 媒体缓存：Bot 的 `CONVERSATION_MEDIA_CACHE_DIRECTORY` 挂 `media-cache` 卷，Bot 读写，环境只读。
  `media_cache_shared` 改为按卷名核对，`type=volume` 的 `Source` 是 daemon 侧路径，不能用来比对（`persistent.py:467-472`）。
- 删除 loop 专用的 `os.path.ismount` 检查（`persistent.py:362-365`）和"挂载就绪前 restart=no"的说明
  （`persistent.py:473-476`）。`restart=no` 本身保留：环境由 Manager 启动。

**运行时**：

- `.env` 的 `YUKI_SANDBOX_RUNTIME=runsc|runc` 由首次安装按 `docker info` 写入显式值，重装和升级保留原值、不重新检测。
  Manager 把它传给 `PersistentManager(runtime=…)`，现在没有传（`manager.py:51-60`）。
- runsc 必须带 `--overlay2=none`，否则环境内安装不进 Docker 层，`docker commit` 检查点会丢东西
  （迁移记录 `mac-arm64-migration-20261107.md:37`）。Manager 从 `docker info` 的 runtimeArgs 核对，不满足报
  `gvisor_overlay_unsupported`。
- `environment_status` 报告实际运行时。

**启动与停止**：

- 不设 `depends_on: service_healthy`。Bot 与 Manager 各自启动；Manager 未就绪时 Bot 的终端类工具返回环境不可用，
  其他功能照常。
- Manager 恢复失败时照旧退出，原因写日志，由 `restart: on-failure` 重试；安装器失败时打印 `manager` 日志。
- `stop_grace_period` 设长，因为 `close()` 要等在途文件任务（`persistent.py:1440-1446`），
  原 unit 是 `TimeoutStopSec=infinity`。

**一台 Docker 一个沙箱**：

- 环境名、出口网络和卷名固定。环境标签加入 Compose 项目名。
- 另一个项目的 Manager 发现环境属于别的项目时，沿现有 `environment_name_in_use`（`persistent.py:356-357`）拒绝，不接管。

**状态卷丢失**：

- execd token 在 `jobs.sqlite3` 里（`persistent.py:387-390`）。`sandbox-state` 为空但环境容器已存在时，
  报 `manager_state_missing`，不生成新 token、不接管。
- 命名卷不会被 `docker volume prune` 默认删除（Docker 23 起只删匿名卷）。

**磁盘**：

- 取消 ext4 loop 后，环境写入与 Bot 的 SQLite、Docker 共用一块盘。环境里的 root 写满它会让 Bot 写库失败。
- `disk_status` 现在报告的是整个文件系统，改为明确标注"共享磁盘"的容量与剩余。

## 7. 删除清单

- **宿主安装链**：
  - `install-manager.sh`、`configure-manager.py`、`sync-manager.py`、`requirements-manager.txt`；
  - `yuki-sandbox.service`、`yuki-environment-storage.service`、`environment-storage.sh`、`colima-boot.conf`；
  - `profile.env.example`、`install-desktop.sh`；
  - `YUKI_SANDBOX_HOME_MIB/RUNTIME_MIB/ROOT`、`YUKI_MANAGER_WORKSPACE_STORE/MEDIA_CACHE`。
- **宿主 Manager 验证工具**：`Dockerfile.validation`、`Dockerfile.environment-validation`、`validate_runtime.py`
  （E2E 用 Compose 取代）。
- **Squid 与内网**（待 P0 确认服务器未使用）：
  - `deploy/sandbox/compose.yaml` 的 `egress` 服务和 `yuki-sandbox-internal`；
  - `squid.conf`、`connect-host-proxy.py`、`UPSTREAM.md`；
  - `network-guard.sh` 中 `yuki-sandbox0` 的规则、`YUKI-SANDBOX` 链和 `upstream-firewall.sh` 钩子；
  - `recover()` 对 `yuki-sandbox0` 的检查（`persistent.py:381-386`）；
  - `tests/unit/test_terminal_environment.py:338` 对应断言。

  `--proxy` 保留，作为通用 HTTP 代理设置；空代理覆盖（`persistent.py:455-460`）保留，旧检查点可能带着 Squid 代理变量。
- **发布与安装**：
  - `scripts/build_release_bundle.py:37-52` 的 `_MANAGER_DEPLOY_FILES`；
  - `tests/unit/test_release_packaging.py:51-59`、`tests/unit/test_manager_deployment.py`（及 `quality.yml:178`）、
    `tests/unit/test_workspace_artifact_lock.py:242`；
  - `install.sh:131-135`、`install.ps1:152-154` 的手工 Manager 指引。
- **过时说明**：`manager.py:1-5` 的文档串和 `:35` 默认镜像 `yuki-environment:20260912`。
- **文档**：
  - `persistent-environment{,.zh-CN}.md` 的安装、升级章节；
  - `desktop-computer.zh-CN.md:54-89`；
  - `desktop-local-deployment.zh-CN.md:4,27`。
- `compose.build.yaml` 保留作本地自定义构建，变量改读 `.env`。

## 8. 安装与发布

- `release.yml` 发布 `yuki:<ver>`、`yuki-environment:<ver>`、`yuki-environment:<ver>-desktop`，
  均为 amd64/arm64 多架构；部署包包含新 Compose 文件。以 4.0.0 首发，修复当前 404。
- `install.sh` / `install.ps1` 一次完成：
  1. 检查 Docker 与 Compose。
  2. 空目录下载部署包；已有部署进入升级：替换受管文件，保留 `.env`、数据和 `docker-compose.override.yml`。
     用户自定义改动放 override 文件，Compose 会自动加载。
  3. 首次安装：选档位、检测运行时并写入 `.env`，创建 external 网络和卷，运行 `yuki setup` 配角色与模型。
  4. `docker compose pull && up -d`。
  5. 等 Bot 健康，运行 `environment-check`。失败时打印 `manager` 日志，不回滚用户数据。
- 非交互：`--yes --profile …`，角色与模型从 `--answers 文件` 读，复用 Desktop 已有的
  `yuki setup desktop-configure` 输入格式（`yuki setup` 现在只有交互模式）。无 TTY 又没给这些参数时拒绝。
- 安装前检测到宿主 `yuki-sandbox` unit 在运行或 `/run/yuki-sandbox/manager.sock` 存在时，拒绝安装并指向迁移脚本。
  旧宿主 Manager 没有归属检查，换容器时会把活动运行标成 `environment_stopped`（`persistent.py:1371-1393`），
  不能让两边同时运行。
- Desktop 本机部署（Yuki-Desktop 仓库）：`docker-compose.local.yml` 一并启动 `manager`，创建 external 资源，
  向导增加档位，默认 `desktop`。

## 9. 已有部署迁移

适用于本机 Colima `yuki` 和服务器（如仍在用）。每次只迁一处，验证后删除备份。
本机现状：runsc `--overlay2=none`、HOME 32 GiB、runtime 1 GiB、2 GiB 内存、检查点
`yuki-environment:headed-migrated-final-20261107`、媒体缓存在部署目录 `data/` 下。

脚本 `scripts/migrate_sandbox_to_compose.sh` 做以下步骤，不并入日常安装器：

1. 记录原容器 ID、启动时间、Manager 检查点和活跃运行，等工作结束。
2. 停止并禁用宿主 `yuki-sandbox` 与存储 unit，`docker compose -p yuki-sandbox down` 停 Squid。
   备份 `jobs.sqlite3`、HOME、runtime、媒体缓存。保存原环境 rootFS：
   - runsc 带 `--overlay2=none` 或 runc 时，用私有 `docker commit`；
   - 否则用 `runsc tar rootfs-upper`。
3. 创建 external 网络和卷，用一次性容器把 loop 盘内容和 `data/conversation-media-cache` 用 `cp -a` 拷进对应卷，
   `jobs.sqlite3` 放入 `sandbox-state`，`checkpoint_image` 指向第 2 步的快照。
   出口网络若是旧 `yuki-sandbox` 项目创建的，先删后由脚本重建为无项目标签的网络。
4. 删除旧 `DOCKER-USER -i yuki-sandbox0` 规则和 `YUKI-SANDBOX` 链（`ensure` 只会添加规则，不会删除）。
5. 归档原容器，`docker compose up -d`，由 Compose Manager 按检查点创建唯一活动环境。
6. 核对 UID/HOME、已装软件、原回执、服务恢复、公网/私网边界、档位与运行时。
   之后卸载 loop 盘，删除 `/opt/yuki-sandbox`、宿主 unit、`/etc/yuki-sandbox` 和备份。

## 10. 失败情况（先于代码）

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| F1 | Docker 没有 runsc | 首次安装写 `runc`，状态报告 `runtime=runc`；配置了 runsc 但未注册时报 `gvisor_unavailable` |
| F2 | 重装或升级时运行时检测结果变了 | 保留 `.env` 原值，不隐式从 runsc 降到 runc |
| F3 | runsc 没有 `--overlay2=none` | 报 `gvisor_overlay_unsupported`，不创建环境 |
| F4 | docker.sock 不可用或无权限 | Manager 退出并在日志写原因；Bot 照常，终端类工具返回环境不可用 |
| F5 | 防火墙后端与 Docker 不一致，或 Docker 用 nftables 后端 | 写入 Docker 实际使用的后端；没有 DOCKER-USER 时报 `network_guard_unsupported`，不创建环境 |
| F6 | 宿主没有 br_netfilter | 报 `network_guard_missing` 并给出宿主命令，不创建环境 |
| F7 | 环境访问宿主、网关、私网、Manager | 在环境内实测全部失败；公网 TCP/UDP 成功 |
| F8 | `compose down`、`up`、`restart manager` | 原环境与进程保留，原运行按现有恢复逻辑接回；停止时等在途文件任务结束 |
| F9 | `compose down -v`、`docker volume prune` | external 网络和卷都保留 |
| F10 | 缺少 external 网络或卷（未走安装器就 `compose up`） | Compose 报缺少资源，不自动创建空 HOME |
| F11 | `sandbox-state` 丢失但 HOME 与环境还在 | 报 `manager_state_missing`，不生成新 token、不接管 |
| F12 | 同一 Docker 上的第二个 Yuki 项目 | 报 `environment_name_in_use`，不接管另一个项目的环境 |
| F13 | 宿主 Manager 仍在运行时安装或启动 Compose Manager | 安装器拒绝，指向迁移脚本 |
| F14 | Bot UID/GID 不是 10001（Desktop 本机） | Manager 用配置的组，socket 与 artifact 对 Bot 可用，`environment-check` 通过 |
| F15 | Bot 先于 Manager 启动，或 Manager 重启中 | 期间调用返回环境不可用，Manager 回来后可用 |
| F16 | 内存或 OOM | runc 只结束超限进程；runsc 整个环境退出，按真实代际中断，登记服务按策略恢复 |
| F17 | 升档时 apt 失败 | manifest 不变，dpkg 可能部分完成，状态报告原因；下次 `up` 重试 |
| F18 | 降档，或把内存调到低于当前用量 | 已装软件保留；先停桌面服务再 `docker update`，失败如实报告 |
| F19 | 改 /tmp、/dev/shm | 状态显示配置值与观测值不同、需要重建，不擅自重建 |
| F20 | `headless` 档使用 `-desktop` 镜像 | `computer` 返回 `display_unavailable`，Desktop 电脑页不显示画面 |
| F21 | 宿主磁盘写满 | 状态标明共享磁盘余量；Bot 写库失败按真实错误报告，不另造配额机制 |
| F22 | 升级后沿旧 rootFS 检查点 | 继续用原 rootFS；代码文件经 `sandbox-code` 子路径更新到新版本 |
| F23 | 安装器在非空、非 Yuki 目录运行 | 拒绝；已有部署替换受管文件，保留 `.env`、数据、override |
| F24 | 无 TTY 且未给 `--yes --profile --answers` | 拒绝并提示参数 |
| F25 | rootless Docker 或 Podman | 宿主网络与 NET_ADMIN 作用不到真实宿主，防火墙检查失败，报 `network_guard_missing`，不创建环境 |
| F26 | arm64 与 amd64 | 三个镜像都可拉取并启动 |
| F27 | Code Mode、本机叠加文件 | `manager` 与 `bot` 用同一镜像 |

## 11. 验收与产物

E2E 为主，每项留可复现产物：

- **CI `compose-lite`**（runc，GitHub runner）：用本次构建的镜像跑 `install.sh --yes --profile lite --answers`，
  模型指向测试内的脚本化 Provider。
  - 经 Bot 发起 `terminal_exec`，验证 F4、F7、F8、F9、F11、F15、F16（runc）、F18、F24。
  - `browser` 返回 `browser_unavailable`。
  - 产物：`test-results/sandbox-compose/lite.json`。
- **CI `compose-headless`**：同上，`browser` 打开本地页面并截图，F20。产物：`headless.json` 与截图。
- **`desktop`**：现有有头 E2E（`test_computer_browser_e2e.py`）改为经 Compose Manager 启动。
- **现场**：
  - 本机 Colima 迁移（第 9 节，runsc）；
  - 记录 F3、F5、F16（runsc）与三档内存，写入 `docs/operations/`；
  - Docker Desktop 部分等 P0 实测后再定。

不发送真实 QQ 测试消息；healthz 不能代替终端、浏览器或桌面的真实结果。

## 12. 阶段

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| P0 | 第 4 节待实测项 | 无 |
| P1 | 发布两个环境镜像；浏览器模式改为运行时设置；档位映射；`sandbox-code` 卷取代 `install-desktop.sh` | P0 |
| P2 | Manager 进 Compose：entrypoint、权限、external 资源、运行时与后端选择、子路径挂载、删除第 7 节清单 | P1 |
| P3 | 一键安装器（sh/ps1，非交互）、release 4.0.0、升级路径、CI E2E | P2 |
| P4 | 迁移脚本、本机 Colima 迁移、文档改写 | P2 |
| P5 | Yuki-Desktop 本机部署带 Manager 和选档 | P3 |

如果 P0 发现 Docker Desktop 上不能写防火墙规则，回到这里决定该平台的网络隔离，不在实现中临时放宽。

## 13. 已接受的代价

- Manager 容器持有 docker.sock，等同宿主 root，和现在以 root 运行的宿主 Manager 相当；Bot 不持有。
- 没有 runsc 时用 runc：环境里的 root 与宿主内核之间只剩容器隔离，状态如实报告。
  反过来，runc 下一个命令超内存只结束它自己，runsc 会结束整个环境。
- HOME 不再有 ext4 硬上限，与 Bot 数据共用磁盘；环境写满磁盘会影响 Bot 写库。
- 一台 Docker 只运行一个 Yuki 沙箱。
- 已有部署需要一次显式迁移。
