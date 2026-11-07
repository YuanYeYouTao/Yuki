# Mac ARM64 Code Mode 与本地迁移实施记录

接续 [2026-11-07 任务书](../architecture/Yuki-Mac本地迁移与ARM64完整运行环境任务书-2026-11-07.md)。用户已授权本地正式环境、完整迁移演练、代码 PR 与合并；AGM 保留云端、Clash 不变，正式切换前另行报告。源码、测试、PR、部署与切换分别记录。

## 修改前的失败场景与验证安排

基线后端 `d6dd44b3`，Desktop `337aaac`。先构建未修改的 ARM64 codemode 镜像并复现原策略；以下场景先列出，再实施。复用已有真实 worker、隔离和恢复测试，不新增硬件评分或常驻检测入口。

| 场景 | 应核对的真实结果 |
| --- | --- |
| AMD64 专用 seccomp 放行没有作用于 ARM64 | 原 ARM64 容器记录实际失败；适配后相同镜像和真实 worker 可完成调用 |
| 不同架构 syscall 参数、bubblewrap 挂载或库路径不一致 | 按 ARM64 实际系统调用确认规则，结合 enforced AppArmor 验证，不放开整个容器 |
| 默认 direct 镜像被错误启用 Code / 仅有配置没有 worker | direct 保默认合同；codemode 使用实际固定 worker、launcher 和匹配 binding |
| Bot 自身取得 mount/userns 或 worker 看到凭据、workspace、网络 | 原 Bot 限制与 worker namespace、UID、capabilities、挂载、环境、网络边界保持；实测输出证据 |
| worker 超时、取消、进程中止、owner 消失 | 复用原 watchdog 和后代清理，原调用回执与预算保留 |
| 暂停后的 Code 组合恢复、重复/迟到子结果、worker 版本变更 | 原执行续接或明确 partial，不重派已完成业务操作 |
| 数据盘未挂载或指向系统盘、恢复 HOME/runtime 时覆盖联调数据 | 核实际 mount/source 与原文件，正式数据和联调数据分开保留 |
| 旧 rootFS/checkpoint 为 x86、配置资源与实际 Docker 不一致 | 原件完整保存、ARM64 原生重建；核实际镜像与限额 |
| 在线 SQLite/WAL 或引用文件只复制一部分 | 在明确停写边界形成完整副本，校验独立库及所有文件引用 |
| 演练副本启动旧自动化、平台登录或电脑操作 | 恢复副本先离线验证，不与云端同时产生原身份的外部效果 |
| 正式改址丢失 Person/device/journal 或本地已有新事实后回退旧库 | 演练保原 ID 和回执；正式切换尚未授权执行，回退保切换后新事实 |

## 实施与证据

### ARM64 Code Mode

- Mac 的 ARM64 Ubuntu 24.04 / AppArmor 4.0.1 宿主已完成原生 `codemode` 镜像构建，没有使用 x86 仿真。原策略下真实 worker 因 namespace 创建被拒绝而失败；仅将原有精确 syscall 规则扩展到 ARM64 后通过。
- [完整隔离结果](mac-arm64-codemode-isolation-20261107.json)：实际 worker / launcher 哈希、六类独立 namespace、无 Host 凭据/文件挂载、只有 loopback、UID 65534、零 capabilities、AppArmor enforced、取消/父进程死亡清理均通过。Host watchdog 在 0.505 秒终止配置为 0.5 秒的停住 worker。这是合成脚本验证，不代表正式模型和业务负载验收。
- 同一 Mac 的 6 GiB 构建 VM 在默认 Rust release LTO 下发生过 OOM；显式 `CARGO_PROFILE_RELEASE_LTO=false`、`CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16` 后构建成功。可选参数不改默认构建值。下载因 HTTP mirror 502 改用临时构建文件中的 HTTPS mirror；沿用原 Clash 代理，未更改 Clash。
- 验证程序支持 `--output -`，避免容器退出后 tmpfs 证据丢失；容器脚本可把 stdout 保存为 JSON。CI 的 AMD64 与 ARM64 runner 均执行真实 native 合同以及 Docker seccomp + AppArmor 组合验证。

### 本地存储和正式宿主

- 新建独立 Colima `yuki` profile：ARM64，4 vCPU、8 GiB RAM、80 GiB 数据盘。正式数据根位于该 profile 实际数据挂载，不使用系统盘或原联调数据。
- Sandbox 配置为 2 vCPU / 4096 MiB、HOME 32768 MiB、runtime 1024 MiB，headed Chromium；Manager 的根路径改为可配置，默认安装路径保留。
- 在真实 loop/ext4 上用临时独立数据验证新建、卸载/挂载与扩容：HOME 64→96 MiB、runtime 32→48 MiB，原文件 SHA256 不变。实际正式恢复与全量业务验证在迁移阶段另记。
- gVisor 默认 `root:self` 的变更不等于 Docker 可见层。完整迁移应另保存 `runsc tar rootfs-upper` 与原镜像；已实测暂停原生 gVisor 容器后仍可导出包含写入文件的 upper tar，再恢复容器。新宿主使用 `--overlay2=none` 让新 rootFS 写入落到 Docker 层；不能仅凭 Docker export 声称已备份旧 gVisor rootFS。

构建、验证、PR 合并、迁移演练与正式切换分别记账。云端当前仍为正式实例；以下为后续完整恢复副本的实测，设备正式改址和平台登录仍留在切换阶段。

### Colima 重启失败场景（修改前）

实际恢复副本首次启动成功；重启专用 VM 后，`cloud-final.service` 尚未挂载数据盘，存储 oneshot 已在系统盘同名目录创建空卷。随后数据盘覆盖该目录，Manager 报 `persistent_home_not_mounted`。原数据卷没有丢失，单凭 Bot `/livez` 不能证明沙箱恢复。

修复验证覆盖：数据盘实际挂载先于 HOME/runtime；原文件与派发回执跨 VM 重启保留；Manager、Bot 和 HTTPS 自动恢复；原生 Linux 安装仍使用原启动目标；已有自定义 unit、配置和数据不被覆盖。采用 systemd 既有依赖和 Colima 专用 drop-in，不新增启动检测循环。

修复后的 [实际 VM 重启记录](mac-colima-restart-20261107.json)：无人为恢复操作，62.02 秒恢复 HTTPS 与 Docker 连接；HOME/runtime 位于数据盘，原文件 SHA256 不变，原已派发执行没有重放。安装器 `--colima` 应用同一份 drop-in，发布包包含该文件。原生 Linux 的默认安装分支保持不变。Mac 物理重启、登录前启动未测试。

### 完整恢复副本实测

- 原生产快照包含部署、SQLite/WAL、连接配置、QQ 资料、sandbox HOME/runtime 和 Manager 记录。4,332,987,737 字节归档及附属文件的 SHA256 全部通过；原自定义 AMD64 镜像与 gVisor upper 另行保留。云端在一致性窗口结束后恢复运行，本机副本没有接管平台流量。
- 用云端相同提交重建的 ARM64 镜像读取原库，再用目标版本正常 `init-db`：schema 0156，完整性及外键检查通过；17 个身份/连接/Work 表的内容摘要不变，85,600 个私有协议对象和 26 个工具 artifact 的文件引用及哈希通过。8 个独立 SnowLuma SQLite 库和 Manager 库也通过完整性检查；QQ 自身加密数据库按原字节保存。
- HOME 2→32 GiB、runtime 128 MiB→1 GiB，扩容前后每个文件、链接、UID/GID 和权限一致。用户 Python 包按原版本安装 ARM64 wheels，原 AMD64 目录保留；原自编译 quaternion 库从原 C 源码重建并实际渲染，原 Resvg CLI 缓存补入同版本 ARM64 可选依赖并生成 PNG。
- 真实 AGM 模型生成程序，经原生 Monty、seccomp/AppArmor 与恢复后的 Manager 完成一次终端派发、文件写入和完成回执，独立读取文件哈希一致。这是独立运维集成探针，未声称离线副本完成了生产 Main 模型聊天。原生完整合同 CI 在 ARM64、AMD64 各 102 项通过；[实现 PR #570](https://github.com/YuanYeYouTao/Yuki/pull/570) 已合并。
- 实装 ARM64 Desktop 通过正常管理员配对，连接本机私有 HTTPS：沙箱真实画面 1440×900，接管后输入完整字符串，在实际浏览器内容中复核，再交还；同窗口 QQ 画面 1920×1080，可独立接手/结束控制。QQ 显示离线，未尝试登录。自动化零延迟键盘连发曾出现输入不完整；60 ms/键复核通过，不将该失败记成产品修复。
- 临时 Mac Connector 通过正常配对和 WSS 握手，未开启 full access。原 Windows 设备、凭据和 journal 未改址；真实 Windows/Mac 文件操作留在正式切换时验收。API 9→10 本身不代表必须升级，当前 Desktop 最低支持 API 9。
- Mac 到云端 AGM 的原配置请求、DeepSeek 原 Anthropic 协议请求、Qwen 原 DashScope embedding 请求均成功。隔离副本本身没有外网出口，避免旧自动化、QQ/TG/飞书及电脑连接同时产生外部效果。Telegram 使用长轮询，飞书使用出站 WebSocket，不因迁移凭空增加公网 webhook。
- 完整副本、QQ、有头浏览器与 Code Mode 并发的三次 Desktop 构建为 3.92、3.52、3.51 秒，内存压力正常且没有新增 swap-out；30 次实际浏览器快照+截图约 47–98 ms，Code Mode 完成约 0.674 秒。此前全量恢复后压力曾到黄色、编译 3.93–4.15 秒并产生换页。16 GiB Mac 不应据此宣称任意重编译或多沙箱都无压力；默认 Rust LTO 构建在较小 VM 上曾 OOM。

原活跃 Linux 进程不能跨机器/架构迁移。恢复后沿原执行 ID 记录 `environment_recreated` 中断，已派发标记保留，未重放。数据库、配置和持久文件迁移不等于保留运行中的进程；平台鉴权、原设备改址和正式模型业务仍需切换后验证。正式切换需要再取云端一致性增量，不能直接启用持续落后的演练快照。
