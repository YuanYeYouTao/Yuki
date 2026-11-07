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

构建、验证、PR 合并、迁移演练与正式切换分别记账。云端当前仍为正式实例；本地恢复、实际 Provider 与两端设备切换尚未由上述结果证明完成。
