# 生命周期与增长路径清理记录（2026-10-23）

本记录覆盖《代码删除与清理任务书》的 F06–F11 可执行面及 F14。基线为
`0aa3e08bd3b5a78677b7831c7a22301966526adf`；全量集成与 PR 状态由总交付说明记录。

## 实施范围

| 项目 | 变更与保留边界 |
| --- | --- |
| F06 | 订阅插件以 `RestartPolicy.NEVER` 退出 Host 热重启，非法配置仍抛出真实错误并停止原服务；修正后明确停用/启用恢复原 pending/cursor。逐源 HTTP 等暂时失败继续沿既有领域轮询间隔处理。SDK 的通用 `ON_FAILURE`、其它消费者和 Q01 调用身份不变。 |
| F07 | 删除 RSS/Atom/JSON Feed 的 200 条拒绝及 FeedState 的 200/800 条计数限制。保 1 MiB 响应字节预算、XML 实体拒绝、去重、MAX_SEEN、持久 observed 标记、存储额度及通知调度配额。804 条 pending 可原样冷装载并分轮处理，CAS 或中途发布失败不推进未提交的事实。 |
| F08 | 每个实例独立 start/recover；ready 实例不等待挂起兄弟或慢 observation 恢复。后就绪通过原连接的 catalog 登记并返回 `catalog_ack`，不重建 device/instance ID 或替换连接。初始化与 ready driver 均由服务 owner 收尾。 |
| F09 | 删除 Qwen 前五项截断及 hot 配置 maximum=5，保默认值 5、真实类型/索引/缺项关联、来源与缓存；六项合法结果完整返回，重复/越界 index 继续沿原规则处理。 |
| F10 | ActivationTasks 每次等待都 shield 原 join，多次 cancel 不能打断已接纳 child 的 finally；原取消在实际收拢后传播。Connector 服务关闭使用同一语义，初始化中 driver 的 stop 也不被第二次 cancel 中断。 |
| F11 | 移除反复全表 JSON 物化与 sent 集合。Journal 按原 sequence 读取下一条已保存 JSON；每个原持久事件是传输信用与 ACK 单位，commit 后 ACK 才推进。断连重传未 ACK 原件，不重派原生执行。RPC reader 不等待数据库；Host 显式 `receive_events([...])` 保原整笔 list 原子性。重复/迟到 ACK 按原 future 的实际完成状态处理。 |
| F14 | Manager 在同一 `jobs.sqlite3` 事务登记 jobs、environment_jobs 与 service.run_id/attempts/next_start；commit 后才入队。退出 restore_services 后置第二次 owner 写，保真正 restart 新 run ID、原请求/hash/dispatched/unknown 与 stop/restart 政策。 |

Connector 升为 **0.2.0 / WSS protocol 2**。Bot 与 Connector 须配套升级，协议 1 明确拒绝，
没有旧协议别名或后备链。HTTP `/agents/v1` 路径、原 Journal、设备凭据、原生 session/
execution/operation 身份不改名。此批没有发布包，也没有操作生产或运行真实用户 Agent 任务。

## 定向验证

- Connector 全包：36 passed；ruff、mypy（12 source files）、锁文件校验、sdist/wheel 构建及独立 wheel 安装通过；独立环境确认没有 Bot 包且模块/发行物/CLI 版本均为 0.2.0。
- 实例启动/信用/重连/取消 owner：13 passed，包括真实测试子进程的重复取消 barrier。将新增取消反例放回原 `0aa3e08b` 源码时会因 cleanup 未完成即退出而失败，当前实现通过。
- Manager Linux：23 passed，含 owner UPDATE 故障整笔回滚、commit 后入队中断、关闭重开及 recover/restore 只认原 job。测试不执行真实 Docker/用户命令。
- 订阅插件：30 passed；ruff、mypy（5 source files）及 `yuki plugin test` manifest/permission/register/start/stop 合同通过。非法配置与暂时源故障分别验证，未请求真实订阅端点。
- 最终核心定向集：57 passed，覆盖重复取消、六图及 hot 配置、真实连接生命周期、视觉来源编辑、WSS 桥与显式原子 batch。更早完整 server 相关集 53 passed；后续新增两个桥接反例已包含在最终定向集中。

本地日志位于 `.cache/deletion-all/lifecycle-*`。Windows 的 POSIX Manager 路径单独在
Linux 容器验证；三系统 CI、全量 unit/integration/Native 与上线验收分别记录，不由这些定向结果代替。

## 传输成本与取舍

独立对照使用仓库的 NativeComputer 测试 peer、真实 TLS WebSocket、临时主 SQLite 和
原 Work 私有输入：两侧同为 80 个持久事件、每项正文 8 KiB、ACK 延迟 5 ms。先持有一个
测试 writer 确认 RPC 在 commit 等待期间仍可返回，再在第二个接收 writer 持有期间排入
合成聊天账本写入。比较原 `0aa3e08b` 的 burst 与当前信用策略，结果如下；这是一组局部
观测，不是生产延迟、供应商执行、上游缓存或全部内存成本的验收。

| 观测 | 原 burst | 原事件信用 |
| --- | ---: | ---: |
| 端到端 | 2.305 s | 5.068 s |
| 吞吐 | 34.7 events/s | 15.8 events/s |
| Python tracemalloc 峰值 | 3,757,014 B | 1,207,405 B |
| 单笔接收 writer 最大持有 | 1.758 s | 0.117 s |
| 合成聊天 writer FIFO 准入等待 | 1.759 s | 0.049 s |
| 同 socket RPC（commit 阻塞期间） | 0.0059 s | 0.0016 s |
| Host repository encode 调用 | 242 | 320 |

该选择减少积压物化并缩短单笔 writer 持有和竞争者等待，代价是每事件一次提交/ACK、
更多小事务准备及较低批量吞吐；高 RTT 下代价会更明显。不将它表述为整体加速。

另对同一批 400 条 × 512 字符的 Journal 作独立本地对照：原逐 ACK 全扫描解析 80,200
条 JSON、峰值 1,038,734 B；keyset 选取 400 条原记录，不再解析事件正文、峰值 20,958 B。
这仅说明该局部路径的物化成本，不能替代上表的端到端取舍。JSON 与原始日志均已保留。
