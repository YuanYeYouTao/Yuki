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

扩展安装真实的 TigerVNC/Xvnc、Openbox、Chromium、socat、xdotool、ImageMagick 和中文／Emoji 字体。构建时查询实际软件包版本并生成 manifest。VNC 和浏览器 CDP 仅监听容器 loopback，不发布 5901 或 9222 到公网。

Manager 创建环境时以原环境名称设置稳定 hostname，使持久 HOME 中 Chromium 的会话和配置在容器重建后仍属于同一主机。浏览器配置和锁文件不作为安装缓存清理。

新环境的原 Manager 启动命令使用 `--image yuki-environment:desktop-local`。根目录、共享工作区、socket、持久 HOME、网络、代理和 DNS 继续使用部署的真实配置；不另起一套 Work 或安装管理器。有头运行需要按宿主实际容量配置资源，Manager 支持显式 `--memory-mib`、`--swap-mib`、`--cpus`、`--pids` 和 `--tmp-mib`。这些值持久保存在原 Manager 数据库，未指定时保留原值和原默认值；新容器创建时应用。

例如在原启动命令后追加 `--memory-mib 3072 --swap-mib 512 --cpus 2 --pids 2048 --tmp-mib 512`，表示操作员选择的上限，不是程序判定任务成功的门槛，也不是默认部署变更。现有容器不会仅因更改镜像参数而重建；已有 checkpoint 也不会被暗中删除。

## 为已有环境安装组件

确认现场原容器名称和持久数据后，在宿主显式执行：

```sh
sh deploy/sandbox/install-desktop.sh --container ORIGINAL_ENVIRONMENT_NAME
```

该脚本核对原 `persistent-v1` 环境，只在已经运行的原容器内安装组件、更新电脑 CLI 与生成 manifest，不启动或重建容器。只读挂载中的源码必须与本 checkout 相同；需要更新挂载时沿原环境升级流程处理。

现有容器若需调整内存、CPU、进程数量，应由操作员明确执行对应 `docker update`，并使 Manager 资源配置与之保持一致；`tmpfs` 大小仅在容器创建时应用。安装后更新原 Manager 代码并沿现场服务管理方式重启 Manager，让它读取实际 manifest；保留原容器、HOME、jobs 数据库、终端与回执。本次开发不执行这些生产操作。

## 使用与核验

桌面端使用电脑页的唤醒、接管、交还和文件入口。Yuki 通过现有 `terminal_exec` 运行 `yuki-computer`，例如 `yuki-computer wake`、`yuki-computer browser snapshot` 或 `yuki-computer help-request --note '请协助当前画面'`。浏览器／桌面输入沿 Manager 的真实控制租约，原工作等待原终端回执，断开桌面连接不创建第二个工作。

核验应分别记录：实际 manifest、`display_status`、Xvnc/RFB 画面、浏览器真实输入与截图、控制权撤销、原请求重试和原工作接续。仅有一个返回成功的 fixture 不能证明现场有头环境健康。源码测试中的协议 fixture 使用可控 RFB 端点验证 UI 和授权；独立的 `test_real_headed_service_renders_unicode_exposes_rfb_and_stops_children` 启动真实 Xvnc、Openbox、Chromium，读取真实 RFB banner、中文页面和 PNG，验证进程退出。完整容器与生产接入另外验收。

本机文件执行器另用固定 `local_device` 工具：`list` 返回本人真实设备和授权，`execute` 立即返回原 `operation_id/op_id`，`get` 查原回执。文件读写的 `confirmed` 与系统 URL／文件打开／通知的 `submitted` 分别认真实结果。升级保留旧执行器、原授权、原操作 UUID 和原字节；旧未决记录不因服务重启自动派发，`unknown` 不转成成功或重做。
