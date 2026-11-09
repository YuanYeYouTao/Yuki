# 桌面电脑与有头环境

Yuki 的持久 Linux 环境、远程画面和本人电脑的文件执行器是三项真实能力。原生 Agent Connector 继续管理 Codex／Claude／ACP 会话，不替代 VNC 或本机文件、URL、通知操作。

默认持久环境保持无头，原资源限额不变。Manager 从实际环境的 `/opt/yuki-runtime/manifest.json` 读取有头组件；没有组件时，电脑接口返回 `display=unavailable`。服务端声明电脑接口不代表现场已经安装有头环境。

## 构建可选有头镜像

在仓库根目录构建同一版本的基础环境与有头扩展：

```sh
docker build -f deploy/sandbox/Dockerfile.environment -t yuki-environment:local .
docker build -f deploy/sandbox/Dockerfile.environment-desktop \
  --build-arg BASE_IMAGE=yuki-environment:local \
  -t yuki-environment:desktop-local .
```

扩展安装真实的 TigerVNC/Xvnc、Openbox、Chromium、socat、xdotool、ImageMagick 和中文／Emoji 字体，并复制 `yuki_computer.py`、`desktop_service.py` 与连接器包中只依赖标准库的共享驱动 `yuki_agent_connector/cdp.py`。构建时查询实际软件包版本并生成 manifest。VNC 和浏览器 CDP 仅监听容器 loopback，不发布 5901 或 9222 到公网。

Manager 创建环境时以原环境名称设置稳定 hostname，使持久 HOME 中 Chromium 的会话和配置在容器重建后仍属于同一主机。浏览器配置和锁文件不作为安装缓存清理。

新环境的原 Manager 启动命令使用 `--image yuki-environment:desktop-local`。根目录、共享工作区、socket、持久 HOME、网络、代理和 DNS 继续使用部署的真实配置；不另起一套 Work 或安装管理器。有头运行需要按宿主实际容量配置资源，Manager 支持显式 `--memory-mib`、`--swap-mib`、`--cpus`、`--pids` 和 `--tmp-mib`。这些值持久保存在原 Manager 数据库，未指定时保留原值和原默认值；新容器创建时应用。

例如在原启动命令后追加 `--memory-mib 3072 --swap-mib 512 --cpus 2 --pids 2048 --tmp-mib 512`，表示操作员选择的上限，不是程序判定任务成功的门槛，也不是默认部署变更。现有容器不会仅因更改镜像参数而重建；已有 checkpoint 也不会被暗中删除。

## 为已有环境安装组件

确认现场原容器名称和持久数据后，在宿主显式执行：

```sh
sh deploy/sandbox/install-desktop.sh --container ORIGINAL_ENVIRONMENT_NAME
```

该脚本核对原 `persistent-v1` 环境，只在已经运行的原容器内安装组件、更新电脑 CLI 与共享驱动、生成 manifest，不启动或重建容器。只读挂载中的源码必须与本 checkout 相同；需要更新挂载时沿原环境升级流程处理。

`computer`／`browser` 工具（2026-11-09）的上线顺序：先用本脚本更新环境内的 `yuki_computer.py`、`desktop_service.py` 与 `cdp.py`，更新并重启 Manager，再让桌面服务重新启动一次（电脑页休眠后唤醒；已在运行的旧桌面服务没有长驻驱动，请求不会被领取，Manager 等待到期后报告确定未执行的 `timeout`），最后部署 Bot。Bot 先于 Manager 上线时，两工具得到 `unknown_method`，不产生效果。

现有容器若需调整内存、CPU、进程数量，应由操作员明确执行对应 `docker update`，并使 Manager 资源配置与之保持一致；`tmpfs` 大小仅在容器创建时应用。安装后更新原 Manager 代码并沿现场服务管理方式重启 Manager，让它读取实际 manifest；保留原容器、HOME、jobs 数据库、终端与回执。本次开发不执行这些生产操作。

## 使用与核验

桌面端使用电脑页的唤醒、接管、交还和文件入口。Yuki 用模型工具 `computer`（截屏、点击、双击、右键、移动、拖拽、输入、按键、滚动，坐标是本次结果所附截图的像素坐标，给 `window` 时只截该窗口、坐标相对该窗口）和 `browser`（按快照节点操作 Yuki 专用 Chromium，含对话框、下载、文件输入与跨站 iframe）操作这台电脑；桌面服务未运行时，第一次动作沿原 `display_wake` 路径唤醒。

动作经 Manager RPC `computer_action`／`browser_action` 按环境串行执行：输入类先写 `environment_mutations` 待定回执，在 Manager 进程内按调用键取 agent 租约，经 runtime 挂载 `computer/driver/requests/` 把请求交给桌面服务托管的长驻驱动（`yuki_computer.py serve`，浏览器部分为 `cdp.py`），驱动以改名领取请求，每次输入（含 xdotool）前复核 `control.json` 中的租约，结果与图像写回 `computer/driver/results/` 后由 Manager 读取删除，最后写结果回执并释放租约。同一调用键重试返回原结果；只剩待定回执（Manager 中途结束）时报 `unknown`，并撤回尚未被领取的请求，绝不再执行；Manager 等待到期时请求仍未被领取则确定未执行。截图、快照与沙箱 `resume` 是读取，不写回执、不取租约，本人接管期间照常可读。沙箱没有“本人叫停”状态，`resume` 只返回与截图相同的读取结果；本人接管与交还仍在桌面端完成。驱动在 `computer/driver/tabs.json` 写出标签页及其 Work 归属（新标签页按 `openerId` 归属），电脑页据此显示。

截图由环境按当前显示几何与部署的 `VISION_MAX_DIMENSION`／`VISION_MAX_PIXELS` 直接产出最终尺寸（Host 不再缩放），下载保存到 `/workspace/downloads`，文件输入框写入环境内路径，不弹出原生选择框。需要本人协助时仍在终端运行 `yuki-computer help-request --note '请协助当前画面'` 或 `yuki-computer wait-control`；本人接管后 Yuki 的下一次输入返回 `control_held`，原工作等待原终端回执，断开桌面连接不创建第二个工作。`yuki-computer status` 显示控制状态与标签页。

核验应分别记录：实际 manifest、`display_status`、Xvnc/RFB 画面、浏览器真实输入与截图、控制权撤销、原请求重试和原工作接续。`tests/integration/test_computer_browser_e2e.py` 在有头镜像中以脚本化模型驱动完整应用、真实 Manager、Xvnc、Chromium 与驱动，产物在 `test-results/computer-browser/`。仅有一个返回成功的 fixture 不能证明现场有头环境健康。源码测试中的协议 fixture 使用可控 RFB 端点验证 UI 和授权；独立的 `test_real_headed_service_renders_unicode_exposes_rfb_and_stops_children` 启动真实 Xvnc、Openbox、Chromium，读取真实 RFB banner、中文页面和 PNG，验证进程退出。完整容器与生产接入另外验收。

本机 Codex、Claude 由桌面的原生 Agent 连接入口管理，云端复用固定 `use_coding_agent` 操作。连接本身不要求项目目录；服务器角色电脑详细环境、画面、文件及控制供 Person 当前超管管理，基础可用性仍向有效设备公开，本人 Connector 的设备、原生会话和操作仍核真实 Person 归属。旧 `local_device` 文件执行器及活 API 已退出；原表、授权记录、操作 UUID 和回执字节保留，旧 Work 仅查询原事实，不重派、不转成 Native 操作，`unknown` 不转成成功或重做。

## 按运行机器选择沙箱

从源码目录将 `deploy/sandbox/profile.env.example` 中所需的设置合入 Manager 宿主的
`/etc/yuki-sandbox/deployment.env`，保留其中原有的工作区和代理设置。也可在构建时使用本机独立的配置文件：

```sh
docker compose --env-file /etc/yuki-sandbox/deployment.env -f deploy/sandbox/compose.build.yaml build environment
```

`YUKI_SANDBOX_BROWSER_MODE` 支持 `none`（终端）、`headless`（仅 Chromium/CDP）与
`headed`（增加 Xvnc、Openbox 和桌面输入）。`YUKI_SANDBOX_PACKAGES` 接受用空格分隔的
Debian 软件包名，可独立于浏览器模式选择。构建完成后，Manager 从实际容器的 manifest
读取模式和已安装版本。两种浏览器模式都通过 `browser` 工具使用同一持久 profile；
无头模式没有可接管的画面，`computer` 返回 `display_unavailable`，`browser` 仍可截图。

新安装的 systemd 单元读取同一份配置。升级旧机器时，沿用自定义单元和 drop-in；
用 `systemctl cat yuki-sandbox yuki-environment-storage` 核对生效配置。旧单元的固定
`--image` 会覆盖环境变量，采用示例配置时删除该参数；存储单元的 `[Service]` 需包含
`EnvironmentFile=-/etc/yuki-sandbox/deployment.env`。修改单元后执行 `systemctl daemon-reload`。
CPU、RAM、额外 swap、PID、/tmp 和 /dev/shm 由 Manager 在创建容器时设置；
已有容器的实际值可用 `docker inspect` 核对。`environment_status` 的 `limits` 是配置值，
`container_at_recovery` 是 Manager 上次连接容器时观察到的镜像和限额。
已有环境继续优先使用其 checkpoint。切换模式前先交还桌面并显式停止浏览器服务，备份
HOME、运行记录与 rootFS。使用 `install-desktop.sh --container 原容器名 headless tree`
这样的命令在原 rootFS 增量安装，再重启 Manager 读取 manifest。切换到较轻模式保留
既有软件和浏览器资料。重建时沿原 checkpoint 恢复，勿用空镜像覆盖自装软件。

`YUKI_SANDBOX_HOME_MIB` 与 `YUKI_SANDBOX_RUNTIME_MIB` 分别指定两块持久卷的容量。
备份后重启 `yuki-environment-storage.service` 可在线扩容 ext4，以 `df` 查看实际容量。
未指定容量时沿用已有卷，新卷分别默认 2048 和 128 MiB。选择更小的值会保留原卷；
缩容需要单独迁移。资源数字由操作者按负载调整。

`YUKI_SANDBOX_ROOT` 指定 Manager 数据库、HOME 与运行记录卷所在的 Linux 目录，
默认仍为 `/var/lib/yuki-sandbox`。存储脚本和配套 Manager 单元读取同一配置；
已有自定义单元需同步其 `--root` 和 `--persistent-home`。指定独立数据盘时，在现场
systemd drop-in 中用 `RequiresMountsFor=` 声明实际数据盘挂载路径，核对 `findmnt`。
改路径不搬迁原数据：先停止相关写入，保留并复制原数据库、两块卷及其他文件，再更新
配置和挂载；不能把空目录当成原 Yuki。挂载已在使用时不直接移动卷文件。
