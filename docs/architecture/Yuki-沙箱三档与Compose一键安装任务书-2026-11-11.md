# 沙箱三档与 Compose 一键安装任务书

日期：2026-11-11。源码基线：`main` `c304e6b7`。对应 issue #524 未完成部分。

实施前阅读[共同开发约束](development-contract.md)、[持久工作环境](../operations/persistent-environment.zh-CN.md)、
[桌面与浏览器](../operations/desktop-computer.zh-CN.md)、[Desktop 本机部署](../operations/desktop-local-deployment.zh-CN.md)。

## 1. 结论

用户决定：

- 沙箱分三档：`lite`（服务器低配）、`headless`（无头浏览器）、`desktop`（有头桌面）。
- 走 Compose 路线：Manager 作为 Compose 服务，与 Bot 用同一镜像、同一版本启动，
  不再在宿主装 systemd、venv、loop 盘和同步脚本。

目标是：装好 Docker 以后，一条命令完成选档、拉镜像和启动，服务器与本机（Mac/Windows 的
Docker Desktop、Colima）走同一套 Compose。选配的机制在 #567 已经实现，本任务要做的是让人能用上它，
并删除压在它前面的宿主安装链。

## 2. 现状与问题

#567 已有：浏览器模式 `none/headless/headed`、预装包、内存/swap/CPU/PID/tmp/shm、
两块持久盘容量，以及在原 rootFS 上增量切换的 `install-desktop.sh`。未设置时 Manager 默认 512 MiB、1 CPU。

问题：

1. 没有档位，十几个变量要手工合进 `/etc/yuki-sandbox/deployment.env`；示例只有 3 GiB 有头一档。
2. 环境镜像不发布，只能在宿主用 `compose.build.yaml` 本地构建。
3. `install.sh` 只做配置，不启动服务。Manager 要手工装 Python 3.12 venv、runsc、出口网桥与防火墙、
   ext4 loop 存储单元、`install-manager.sh --bot-container`，只支持带 systemd 的 Linux。
4. Desktop 本机部署不装 Manager，本机没有终端、浏览器和桌面，最该用高配的场景反而用不了。
5. `install.sh` 默认下载 4.0.0，GitHub 最新 release 是 3.9.0，一键安装当前直接 404。

## 3. 参考：Memoh

`felinics/Memoh`（原 memohai/Memoh）只发布一个约 640 MB 的工作区镜像，自带 XFCE、TigerVNC、Chromium，
桌面在首次使用时才启动；选配在 Web 管理界面按 bot 设置（镜像、桌面开关、CPU/内存/磁盘、GPU），
改资源要重建工作区。安装是 `curl … | sh`：交互问目录和密钥，拉多架构镜像后 `compose up`，
再运行即升级。它安装简单的根本原因是 server 容器 `privileged` 并内嵌 containerd，宿主只需 Docker。

借鉴：预发布多架构镜像、一条命令安装和升级、桌面懒启动、宿主只要 Docker。
不照搬：按开关增删工具（违反固定工具合同）、单一大镜像（低配服务器不需要 Chromium）、不分档。

## 4. 三档

档位只决定环境镜像、浏览器模式和资源的初始值，每个值都可在 `.env` 单独覆盖。
数值是初值，P0 实测后修订；它们是部署资源，不是安装门槛或任务接纳条件。

| 设置 | `lite` | `headless` | `desktop` |
| --- | --- | --- | --- |
| 环境镜像 | `yuki-environment:<ver>` | `yuki-environment:<ver>-desktop` | `yuki-environment:<ver>-desktop` |
| 浏览器模式 | `none` | `headless` | `headed` |
| 内存 / 额外 swap | 512 / 128 MiB | 1024 / 256 MiB | 3072 / 512 MiB |
| CPU / PID | 1 / 256 | 1.5 / 1024 | 2 / 2048 |
| /tmp / /dev/shm | 128 / 64 MiB | 256 / 128 MiB | 512 / 256 MiB |

- 只发布两个环境镜像。有头只比无头多 Xvnc、Openbox、xdotool 等几个小包，`headless` 与 `desktop`
  共用 `-desktop` 镜像，模式改为运行时设置：Manager 把模式写入 runtime 卷，`desktop_service` 读它，
  不再在构建时写死进 manifest。manifest 只记录实际安装的软件与版本。
- 工具声明不变。`computer`、`browser` 在三档都声明；`lite` 下按真实情况返回 `browser_unavailable`，
  `headless` 下 `computer` 返回 `display_unavailable`（现有行为）。环境说明和 `environment_status`
  写明当前档位、模式和实际限额。
- 安装器显示 Docker 报告的内存并给出推荐，由用户选，不拦截：低于 2 GiB 推荐 `lite`，
  2–4 GiB 推荐 `headless`，更多推荐 `desktop`；Desktop 本机部署默认 `desktop`。
- 换档：改 `.env` 的 `YUKI_SANDBOX_PROFILE` 后 `docker compose up -d`。升档时 Manager 在原 rootFS
  上沿 `install-programs.sh` 增量安装缺少的包，结果写入状态；降档保留已装软件和浏览器资料，只改模式和资源。
  内存、CPU、PID 用 `docker update` 立即生效；/tmp 和 /dev/shm 只能在下次明确重建时生效，
  状态里分别列出配置值和观测值（沿用现有 `limits` 与 `container_at_recovery`）。

## 5. Compose 架构

```
docker-compose.yml
  bot       ghcr.io/yuanyeyoutao/yuki:<ver>   挂 sandbox-run 卷取 manager.sock（只读）
  manager   同一镜像，command: python -m yuki.sandbox.manager
            network_mode: host，cap_add: NET_ADMIN
            挂 /var/run/docker.sock、sandbox-state、sandbox-home、sandbox-runtime、
            sandbox-code、media-cache、./workspace
  egress 网络 yuki-sandbox-egress（Compose 定义）
yuki-environment  由 manager 经 docker.sock 创建，不属于 Compose 项目，restart=no
```

- **同一镜像**：Manager 与 Bot 版本永远一致，删除 `sync-manager.py`、`releases/<提交>/src`、
  `/opt/yuki-sandbox/venv` 和 `requirements-manager.txt`。runtime 镜像加入 docker CLI 与 iptables；
  Bot 容器不挂 docker.sock，不因此获得 Docker 权限。
- **网络**：Manager 用宿主网络，与现在宿主进程的网络位置相同，`network-guard.sh` 在 Manager 启动时执行，
  原 INPUT/DOCKER-USER 规则不变。删除早已不用的内网 Squid（`egress` 服务、`yuki-sandbox-internal`
  网络、`squid.conf`、`YUKI-SANDBOX` 链）；新环境只用公网出口网桥。
- **卷**：HOME、runtime、Manager 状态（`jobs.sqlite3`）、媒体缓存都改为 Docker 命名卷，
  由安装器创建并在 Compose 中声明 `external: true`，`docker compose down -v` 不会删除它们。
  Manager 通过 docker.sock 创建环境时用 `type=volume`，不传 Manager 容器内路径，
  这样 Docker Desktop、Colima 和普通 Linux 行为一致。删除 ext4 loop、`environment-storage.sh`、
  `yuki-environment-storage.service`、`colima-boot.conf` 和 `HOME_MIB/RUNTIME_MIB`。
- **Supervisor 与 helper**：Manager 启动时把本版本的 `environment_supervisor.py` 等写入 `sandbox-code` 卷，
  环境只读挂载。替代现在指向宿主源码路径的 bind，旧 rootFS 也能拿到当前代码。
- **媒体缓存**：Bot 的 `CONVERSATION_MEDIA_CACHE_DIRECTORY` 改为挂 `media-cache` 命名卷，Bot 读写，环境只读。
  Bot 仍是唯一写入和清理者。
- **运行时**：`.env` 的 `YUKI_SANDBOX_RUNTIME=runsc|runc` 由安装器按 `docker info` 写成显式值。
  配置了 runsc 但 Docker 没注册时，照旧报 `gvisor_unavailable`，不静默降级；`environment_status`
  报告实际运行时。
- **启动顺序**：命名卷总是可用，不再需要 systemd 挂载顺序。`manager` 设 `restart: unless-stopped`
  和 healthcheck，`bot` 的 `depends_on` 等它健康；Manager 恢复时启动原环境（现有 `recover`）。
- 删除 `install-manager.sh`、`configure-manager.py`、`yuki-sandbox.service`、`profile.env.example`。
  `compose.build.yaml` 留作本地自定义构建，档位变量改用 `.env`。

## 6. 安装与发布

- `release.yml` 发布 `yuki:<ver>`、`yuki-environment:<ver>`、`yuki-environment:<ver>-desktop`，
  均为 amd64/arm64 多架构；部署包包含新的 Compose 文件。以 4.0.0 首发，修复当前 404。
- `install.sh` / `install.ps1` 一次完成：
  1. 检查 Docker 与 Compose；
  2. 空目录下载部署包，已有部署进入升级，保留 `.env`；
  3. 选档位、角色和模型（复用 `yuki setup`），检测运行时，创建命名卷；
  4. `docker compose pull && up -d`；
  5. 等待 Bot 健康，并运行 `environment-check`。

  非交互：`--yes --profile lite|headless|desktop`。重新运行即升级。
  失败时打印最近日志，不回滚用户数据。
- Desktop 本机部署（Yuki-Desktop 仓库）：`docker-compose.local.yml` 一并启动 `manager`，
  向导增加档位，默认 `desktop`；电脑页能看到本机环境的画面。

## 7. 已有部署迁移

适用于本机 Colima `yuki` 和服务器（如仍在用）。每次只迁一处，迁完验证后删除备份。

1. 记录原容器 ID、启动时间、Manager 检查点和活跃运行，让工作结束。
2. 停止并禁用宿主 `yuki-sandbox` 与存储单元，备份 `jobs.sqlite3`、HOME、runtime、媒体缓存，
   并保存原环境 rootFS 的私有 `docker commit`。
3. 用一次性容器把 loop 盘内容 `cp -a` 进对应命名卷，把 `jobs.sqlite3` 放入 `sandbox-state`，
   并把 `checkpoint_image` 指向第 2 步的 commit。
4. 归档原容器，`docker compose up -d`，由 Compose Manager 沿检查点创建唯一的活动环境。
5. 核对 UID/HOME、已装软件、原回执、服务恢复、公网/私网边界和档位，再卸载 loop 盘，
   删除 `/opt/yuki-sandbox`、宿主 unit 和备份。

Compose Manager 发现同名环境的挂载不是自己的卷时，报 `environment_owned_elsewhere` 并拒绝启动，
不接管、不删除。迁移有专门脚本（`scripts/migrate_sandbox_to_compose.sh`），不写进日常安装器。

## 8. 失败情况（先于代码）

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| F1 | Docker 没有 runsc | 安装器写 `runc`，状态报告 `runtime=runc`；配置 runsc 但未注册时报 `gvisor_unavailable` |
| F2 | docker.sock 不可用或无权限 | Manager 不健康并报原因；Bot 照常运行，终端类工具返回环境不可用 |
| F3 | 宿主没有 br_netfilter 或 DOCKER-USER | Manager 报 `network_guard_missing`，不创建环境 |
| F4 | 环境访问宿主、网关、私网、Manager | 全部失败；公网 TCP/UDP 成功 |
| F5 | `docker compose down`、`up`、`restart manager` | 原环境与进程保留，原运行按现有恢复逻辑接回 |
| F6 | `docker compose down -v` | 命名卷是 external，HOME 与状态不删 |
| F7 | 宿主或 VM 重启 | Manager 自启后启动原环境，不需要 systemd 顺序 |
| F8 | Bot 先于 Manager 启动或 Manager 重启中 | Bot 等健康；期间调用返回环境不可用，Manager 回来后可用 |
| F9 | 升档时 apt 失败 | 环境与 manifest 不变，状态报告失败原因，下次 `up` 可重试 |
| F10 | 降档 | 已装软件与浏览器资料保留，模式和内存/CPU/PID 立即生效 |
| F11 | 改 /tmp、/dev/shm | 配置值与观测值不同，状态显示需要重建，不擅自重建 |
| F12 | 内存低于档位推荐 | 只提示；运行中的 OOM 按真实结果报告 |
| F13 | 宿主磁盘写满 | 命名卷没有硬上限，状态报告 HOME 用量；不另造配额机制 |
| F14 | 升级后旧 rootFS 检查点 | 继续用原 rootFS；Supervisor 与 helper 由 `sandbox-code` 卷更新到新版本 |
| F15 | 宿主 Manager 未停就启动 Compose Manager | 报 `environment_owned_elsewhere`，两边不同时管理同一环境 |
| F16 | 安装器在非空、非 Yuki 目录运行 | 拒绝；已有部署走升级并保留 `.env` |
| F17 | 无 TTY 且未给 `--yes --profile` | 拒绝并提示参数 |
| F18 | arm64 与 amd64 | 三个镜像都可拉取并启动 |
| F19 | Code Mode Compose 叠加文件 | 不受影响 |

## 9. 验收与产物

E2E 为主，每项都留可复现产物：

- **CI `compose-lite`**：用本次构建的镜像跑 `install.sh --yes --profile lite`。经 Bot 发起
  `terminal_exec`，验证 F2、F4、F5、F6、F8、F10、F17，并检查 `browser` 返回 `browser_unavailable`。
  产物：`test-results/sandbox-compose/lite.json`。
- **CI `compose-headless`**：同上，经 `browser` 打开本地页面并截图，`computer` 返回
  `display_unavailable`。产物：`headless.json` 和截图。
- **`desktop`**：复用现有有头 E2E（`test_computer_browser_e2e.py`），改为经 Compose Manager 启动。
- **现场**：本机 Colima 迁移（第 7 节），以及 Mac Docker Desktop 新装一次 `desktop`，
  记录 F1、F3、F7、F13 和三档的实测内存。结果写入 `docs/operations/`。

不发送真实 QQ 测试消息；healthz 不能代替终端、浏览器或桌面的真实结果。

## 10. 阶段

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| P0 | 实测：容器内 Manager 用宿主网络 + NET_ADMIN 能否在 Linux、Colima、Docker Desktop Mac/Windows 写入防火墙规则；runsc 容器能否 `docker update`；三档空闲、开一个页面、看 VNC 时的内存；两个镜像的大小 | 无 |
| P1 | 发布两个环境镜像，浏览器模式改为运行时设置，档位映射，删除 `profile.env.example` | P0 |
| P2 | Manager 进 Compose：命名卷、`sandbox-code` 卷、显式运行时，删除宿主安装链和 Squid | P1 |
| P3 | 一键安装器（sh/ps1）、release 4.0.0、升级路径 | P2 |
| P4 | 迁移脚本、本机 Colima 迁移、文档改写 | P2 |
| P5 | Yuki-Desktop 本机部署带 Manager 和选档 | P3 |

P0 若发现 Docker Desktop 上不能写防火墙规则，回到这里决定该平台的网络隔离，不在实现中临时放宽。

## 11. 已接受的代价

- Manager 容器持有 docker.sock，等同宿主 root，与现在宿主 root 运行的 Manager 权限相当；Bot 不持有。
- 没有 runsc 的 Docker（Docker Desktop、多数新服务器）用 runc。环境内 root 与宿主之间只剩容器隔离，
  状态里如实报告。
- HOME 不再有 ext4 硬上限，磁盘用量只报告。
- 已有部署需要一次显式迁移。
