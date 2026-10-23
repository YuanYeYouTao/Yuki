# B1–B6 代码删除与清理执行记录（2026-10-23）

执行[代码删除与清理任务书](../architecture/Yuki-代码删除与清理任务书-2026-10-23.md)，
基线为 `main 0aa3e08bd3b5a78677b7831c7a22301966526adf`。
分支沿用 `codex/code-deletion-b1-20261023`，独立 worktree 为 `.worktrees/code-deletion-b1`，
PR 为 [#500](https://github.com/YuanYeYouTao/Yuki/pull/500)。B1 独立提交为 `5490d90c`；
后续批次在同一分支集成，最终提交和全量 Quality 结果以该 PR 的具体 head 为准。
本记录不表示合并、正式 Release、包发布、部署或真实聊天验收。

## 逐项实施

| 批次 | 项目 | 完成的源码变化与保留合同 |
| --- | --- | --- |
| B1 | F24–F29 | 移除 mark_injected、重复 rebuild core、Dream defer_pending、旧账本包装、死 generation helper 和 Claim Plan 死字段/死分支；现役 reader、原证据、候选 owner、真实 NOOP 和旧 checkpoint 保留。完整子项见 [B1 记录](code-deletion-b1-20261023.md)。 |
| B1/B3 | F33 | R01–R08、G01–G04、B01–B05、D01–D07 于 B1 退出；D08 随 F15 删除备用 MemoryAdmin 调用后退出。真实执行授权、通知、原媒体/事实及工具回执保留。 |
| B2 | F01 | 插件逐调用 operation ID 不再被事件 ID 覆盖；原调用读原回执，独立调用独立接纳。历史 event-wide 回执只按实际原插件归属处理，未知映射不猜测、不重放、不批量改键。 |
| B2 | F02/F20 | Host 与独立 SDK Fake 统一 Memory 返回形状、confidence 与修正版本语义；生产者标签不能冒充真人指令。原事实与证据不批量重写，同事件修正及不同事件历史证据有真实 SQLite 回归。 |
| B2 | F03 | 新 codec 3 普通来源不依赖诊断 privacy counter；实际来源/owner/版本和 observation 隐私仍核验。旧 codec 2 保原指纹与 reader，失效的旧 paid/unknown 证明沿原失败恢复，不重新认证、购买或执行效果。 |
| B2 | F04/F05 | resume 正文保原换行/空行；release 清理失败不覆盖原接受、未知、异常或取消。原 request ID 和精确输入继续幂等。 |
| B2 | F13/F34 | 原 Person 传入 purge，FIFO writer 首次 DML 前复核当前真人/超管资格；删除旧 profile 一次事务依现有 FK 级联清退旧 job/vector，保当前 profile、计数与原事实。撤权、合并、停用和故障回滚均有反例。 |
| B2 | F14 | Manager 原 jobs/environment_jobs/service owner 同笔提交，commit 后入队；不再有提交后补 run_id 的窗口，原 unknown/dispatched 恢复不换执行身份。 |
| B3 | F15 | MemoryAdmin 必须注入真实 mutations/ledger，完整退出备用写事实业务及专属 helper；真实缺失结果仍为 None/False。 |
| B3 | F16 | 删除无人使用的 EXCLUSIVE 调度整链；前台/BACKGROUND 公平、SELF quiet、维护名额、资源容量与取消释放保留。 |
| B3 | F21/F22 | 退出演示 prompt inspect/compare、PromptTrust 及专属生产者参数；真实 CLI profiles/routes/stats、Prompt 编译、来源/角色/正文顺序与插件批准保留。 |
| B3/B6 | F23/F17 | 退出 CapabilityExposure、死 scope/core 词表及人工 opening 状态；MainAgentContract 编译并持有固定只读 schema view，各激活独立投影当前执行授权。SDK 真 metadata、同名变更指纹、schema 隔离和本地 ref 合同保留。 |
| B4 | F19/F30 | Workspace owner 打开时原子升级独立 manifest，补旧 source_key 并实际删除 expiry 列。旧到期原件保持原 ID/字节/hash/revision/来源；普通事务无 schema 检查或周期归一化，仍核路径链接、commit/rollback 和物理关闭。 |
| B5 | F06 | 只收敛订阅插件配置失败的 owner：真实非法配置停止，修正后显式重启；逐源网络失败沿原轮询恢复。通用 SDK ON_FAILURE 不在本轮删除面。 |
| B5 | F07/F09 | 贯穿退出 Feed 200/800 总量限制及 Qwen 前五项/热配置上限；实际字节、实体、存储、来源与结果 index 合同保留，804 pending 冷装载与六图实测覆盖。 |
| B5 | F08/F10 | 实例独立就绪与恢复，后就绪沿原连接更新 catalog；重复 cancel 仍等原 child/driver 真正关闭，原取消之后传播。 |
| B5 | F11 | Journal 按原 sequence keyset 取保存 JSON，退出每 ACK 全量物化与 sent 集合；协议 2 每原持久事件一个 commit ACK 信用，RPC 独立。显式 repository list 仍整笔原子提交。 |
| B6 | F12/F31 | configured 从当前实际 executor/router/pool 及凭据/注入事实投影；平台健康从真实目录、Presence 与 owner 来源汇总。保两个公开 API 的原字段/响应语义，增加平台不复制特例或建立健康状态机。 |
| B6 | F18 | 按真实对象图构造 Workspace/Media/Sandbox/Social/Emoji/Chat，删临时 ClientApi、后置 None 槽和 app:Any 总线；各领域 owner 不合并。初始化失败及启动失败均收拢已构造 owner，原错误不被关闭异常覆盖。 |
| B6 | F32 | 每个 ConfigRegistry 持有自己选中字段的 TypeAdapter；热转换不重建固定 schema。保原类型、枚举、自定义注解、范围及环境/热覆盖，不加全局缓存。 |

任务书单列的 **Q01**（Plugin Main 同参调用/原冷恢复身份）和 **F06 通用 ON_FAILURE**
恢复时机按原合同保留；它们明确不在“安全删除”范围，并非遗漏的可执行项。

## 数据与兼容

不修改主库冻结迁移，不清表、不重写历史 ID/证据/回执，不触碰生产数据。
FIFO、原请求/执行追溯、当前执行授权、组合 session、不可变发布、引用保护及 GC 继续归原 owner。
Workspace 的升级仅发生于其独立 manifest；Manager 沿其独立 jobs 库的原事务单位收敛。

连接器为 **0.2.0 / WSS protocol 2**，需与 Bot 配套升级。保原配置、凭据、设备/实例/会话/
执行/操作 ID 和 journal，协议 1 没有兼容别名。Host Python/CLI 的有意撤销与 SDK Fake
fixture 迁移见[升级说明](../upgrade-code-deletion-20261023.md)及其英文版；不把接口删除表述为无外部影响。

## 验证与基线

各批按所属模块回归，集成后再执行全量；不将重叠定向结果相加为独立覆盖数量。
下表为源码实施阶段、测试精简前保存的日志。用户随后追加了[全仓测试精简](test-cleanup-20261023.md)，
498 文件清单逐项检查，净减 4,204 行；最终新 head 的执行结果单独记录。

| 范围 | 已保存的本地定向证据 |
| --- | --- |
| B1 | Memory、上下文、Task/通知、媒体、Provider、容量及 GC 全部对应回归；固定 main 与 B1 的全量 JUnit 对照无既有用例结果变化，仅增加两个候选 owner 拒绝用例。 |
| Memory/插件 Host | 最终 17 文件 124 passed；来源、逐调用幂等、原回执、修正证据、FIFO 撤权、purge 级联与 rollback、resume 结果传播。 |
| paid 恢复/固定合同 | 48 项 paid/compact/protocol 回归通过；另有真实跨输入诊断 counter 解耦反例、固定 schema/授权隔离及 CLI 5 项回归。 |
| 生命周期/传输 | 核心最终 57 passed；Connector 全包 Windows 36 passed，owner/ACK 定向 13 passed；Linux Manager 23 passed；订阅插件 30 passed 及真实插件生命周期合同。详细证据见[生命周期记录](code-deletion-lifecycle-20261023.md)。 |
| 连接器发行物 | 独立 wheel 安装不包含 Bot 包；12 个包源码文件与 wheel 字节一致，模块/发行物/CLI 均为 0.2.0；锁文件、ruff、mypy 通过。构建并不等于发布。 |
| Workspace/装配/健康/配置 | 最终 owner 合同集 121 passed；构造/启动故障真实关闭 14 passed；最后一组消费者迁移 79 passed/4 POSIX skip。真实旧库、并发升级、故障 rollback、短状态寿命与扩展平台分别有回归；POSIX 路径以最终 Linux 全量核验。 |

集成源码全仓 ruff 通过，Linux-target mypy 659 个源文件通过。首次共享 mypy 缓存出现工具内部错误，
使用独立缓存重跑通过；最终 CI 仍在真实 Linux 上执行相同类型检查。

固定 main `0aa3e08b` 的 Quality [39873941131](https://github.com/YuanYeYouTao/Yuki/actions/runs/39873941131)
和 B1 `5490d90c` 的 [39915425124](https://github.com/YuanYeYouTao/Yuki/actions/runs/39915425124)
均为 7 jobs 成功。B1 为 unit 3607 passed、integration 882 passed/1 paid-live skip、
Native 94 passed；三系统 Connector 通过。**这些旧 head 结果不替代 B2–B6 最终 head 的全量验证。**
最终全量 unit/integration、Native、三系统 Connector 与 lint/type 检查通过后在 PR 中逐项记录。
本地日志和原始 JSON/XML 保存在 `.cache/b1-validation/` 与 `.cache/deletion-all/`。

中间提交 `ae45ba14` 的完整全量发现 unit 4 项、integration 1 项失败，已保存原红基线。
`1cff2d50` 修复 SDK 发送遗漏原 invocation 配置快照；历史 fixture 接真实调用上下文，
sidecar fixture 补原 0600 权限，旧诊断 counter/退役词表断言按 F03/F23 迁移。
聚合回执、产品权限和恢复规则没有放宽。测试精简单独提交后重新跑最终 head 全量，旧绿或红日志都不替代它。

Windows 上既有 POSIX/fcntl 用例不冒称通过，Linux 对应回归及 CI 单独验证。
Native 以 CI 构建的 pinned Monty、真实隔离和原执行回执回归为准。无真实模型、订阅端点、
用户 Agent 任务或 QQ 消息测试；上线和自然流量不属于本次交付。

## 同输入成本观察

这些是离线局部观测，无新增性能门槛，不代表线上延迟或模型缓存命中。

| 路径/条件 | 旧路径 | 当前路径 | 解释 |
| --- | --- | --- | --- |
| 固定 schema：同 4 工具、100 次激活 | 5.372 s；峰值 261389 B | 0.105 s；258069 B | 编译移至固定合同；不缓存授权，也不改变声明/revision。 |
| ConfigRegistry：同 ASR 输入，3000 次×5，中位数 | 热转换 0.222 s；峰值 5680 B | 0.00179 s；112 B | 初始化中位从 0.000263 s 增至 0.00473 s；工作从热路移至原 registry owner。 |
| TLS WSS + SQLite：80×8KiB，5ms ACK | 34.7 events/s；峰值 3757014 B；聊天 FIFO 等待 1.759 s | 15.8 events/s；1207405 B；0.049 s | 单事件信用降低物化和 writer 占用，代价是更低批量吞吐及更多小事务；不宣称整体加速。 |

更完整条件与 writer/RPC/编码调用数据见生命周期记录；原始观察脚本与 JSON 另存本地验证目录。
