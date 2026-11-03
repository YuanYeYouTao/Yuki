# 语义参与状态与消息延迟优化实施记录

日期：2026-11-03，UTC+08:00。依据[修订任务书](../architecture/Yuki-语义参与状态与消息延迟优化任务书-2026-11-03.md)实施，主Agent统筹，三个Terra max执行并交叉复核。

状态：第一轮及公开健康路径的 D10 补修均已合并，并先后上线 Bot。源码、隔离验证、生产健康和真实用户验收分别记录；本轮未发布新版 Desktop。

第一轮 Host PR #562 已合并至 main `fa01854d0c6832dc98cc8673d666386c97ce94b6`，参与库使用 `a6a13d4f04ecc36da26adb5f364abdb4f032fc1e`。当时上线的 Bot 代码源为 `004ae36dbe26046d3946b78934c4766333831bcf`、镜像 SHA-256 前缀 `b3cec`，数据库迁移为 `0156`；切换核验保留 1280 条 Work、24858 条确认回执及原预算事实。

## 基线

- Host：`b81c20a55d3a714168e7d8306ccfbf74dddb3eb4`。
- 参与库：`a035009b5cb64bef665eb3a8b85ed9ea7f3ecda0`。
- 两仓隔离分支：`codex/participation-latency-20261103`，原文档工作目录中的用户修改未覆盖。
- 私有排查快照及完整本机证据：`D:/Code/Reports/Yuki/participation-latency-implementation-20261103`；原报告仍按其固定SHA-256引用。

## 先复现再实施

失败场景按任务书E01–E14定义。基线已固定为独立源码归档，后续工作树修改不覆盖旧版；隔离Provider/网关产生的回执明确标为模拟，不代表真实QQ/Provider验收。

隔离验证使用 Linux Python 3.12.13、SQLite 3.53.1、Pydantic 2.13.4 与 pydantic_core 2.46.4。生产镜像实际为 Python 3.12.16、SQLite 3.46.1，后两项 Pydantic 版本相同；两者不是相同运行时，隔离性能数字不代表生产延迟。早期源码路径验证不作为实际锁定包安装验收。

D10 最终源码 `b7fad5c89c4e261b5221285a0ecc277b3f4b37bb` 在原生 Linux 以非 editable 方式实际安装 Host、SDK 和 a6 参与库，相关 gateway/health 组 39/39 通过；全仓 Ruff、变更文件格式及 697 个源码文件的 mypy 检查通过。目标镜像另在无网络、只读、非 root、无生产数据挂载的容器中完成 53/53 离线 E2E，0 跳过；679 Host、18 SDK 和 14 库 Python 文件共 711/711 与固定源码匹配。镜像验证有 12 条 SQLite/SQLAlchemy 警告，原始记录见本机 `server-gateway-health-image-offline-e2e.log` 与 `server-gateway-health-image-provenance.log`。这些场景使用合成数据或模拟下游；此前 `6e26` 候选镜像的 46/46 属于独立历史结果，不记作最终镜像验收。

## D10 合流与生产复核

[PR #563](https://github.com/YuanYeYouTao/Yuki/pull/563) 的 [11 项 CI](https://github.com/YuanYeYouTao/Yuki/actions/runs/41551516736) 全部成功，于 2026-11-03 00:37:10 UTC 合并至 main `5a7fdb29b2c3da354241b42154214bb888db3236`；其 Git tree 与 `b7fad5` 相同。Bot 于 00:38:33 UTC 以代码 revision `b7fad5`、镜像 `sha256:ee52916c4352a48b5c38b786bd25a9004f86d608ad12dad8e4e9a1f8b4b1a48a` 启动，容器 `abeb418d191e951123ead5678ba86aed41e387f99376e1b4d6e70b2e9e67c218` 在 00:39:46 UTC 核验为 healthy、重启 0 次。schema 前后均为 `0156`；切换前 1281 条 Work、2963 条输入原 ID 无缺失，24862 条确认 Social 回执所核字段无变化；预算行无缺失，model/tool 计数无下降。原环境、挂载、网络及受保护配置未变，其余四个容器的 ID 与启动时间未变。切换前活动 Work 为0，不据此宣称生产已覆盖在途 Work 恢复。原始摘要见本机 `server-gateway-health-cutover-summary.json`。

第二次备份在 00:04:33–00:06:08 UTC 停写窗口制作两库副本并同步文件，95 秒后恢复同一原 Bot；独立副本的完整性、外键及全部原引用核验在 00:19:40 UTC 完成，覆盖83262个协议对象、151个工具文件及48对参与快照。备份点早于后续运行，切换保留 live 新事实；rsync 按大小和时间同步，不能称整个文件树逐字节同一时点。

第一轮上线后的首次 `/healthz` 为 7342.07 ms、暖请求为 4045.91 ms，`/livez` 为 24.42 ms。D10 上线首探在服务尚未监听时得到 ConnectionRefused，原始记录保留；ready 后五次本机 HTTP `/healthz` 样本为 15.05、5.66、4.65、5.09、4.60 ms，中位数 5.09 ms；`/livez` 中位数 1.68 ms。五次均返回健康、数据库可读、writer accepting、QQ 连接为 true，见 `server-gateway-health-final-probes-ready.json`。前后不是受控同负载性能实验，样本也不是 QQ `/ai ping`；没有主动发送 QQ 验收消息或发起付费模型验收，不能将连接状态或健康耗时当成真实聊天端到端验收。

## 健康与诊断兼容

公开 `/healthz` 删除无仓内消费者的 `memory_embedding_coverage`、`memory_embedding_pending_jobs`、`memory_embedding_failed_jobs`，不再为一次健康读取加载并逐条 hash 全部 Memory。enabled/configured 读取原运行时与实际 Provider 装配；精确状态仍经原受权 `/ai memory embedding status` 按需查询。仓内 Desktop/Monitor/Connector 消费者已核对；外部自行读取已删除字段的调用方须改为按需详细查询。

D10 补修从公开响应移出无仓内消费者的自省、Memory 一致性、Rebuild、Dream、Work 与 Automation 历史聚合，语义管理健康也删除无消费的历史诊断汇总与辅助计数；数据库读写、网关、Worker 与配置的真实基础状态保留。Memory 自省和一致性原有受权详情继续按需读取，Work 完整 health 仍由原生命周期使用；Rebuild、Dream 的受权列表和单次运行状态不等于原公开全局汇总。旧公开字段的外部消费者未知，需按实际接口调整；Client/Admin OpenAPI 不变不代表公开 `/healthz` 字段未变。详情统计失败不再自动使基础健康失败，也不代表详情已被核验；持续数据库故障仍可能使 gateway 查询返回 HTTP 500。

Gateway 健康将 Presence ID、Account ID/platform 和 enabled 窄投影后按平台单次分组，保留历史 disabled/ended 所属平台、原 owner 语义和跨等待的连接读取时点。P 为读出的 Presence 历史阶段数、K 为平台数时，原 Python 层按平台重复扫描的形状为 Θ(P·K)，现在仍读取并处理 P 行，另有平台排序、registry 扫描、SQL 和 owner 等待；K 固定时原扫描对历史增长也是 Θ(P)，不能将此次改动表述为消除了全部历史线性成本。

语义观察诊断使用显式字段图，在原容量准入后才复制独立树；不执行任意 Pydantic serializer 或 computed field。记录入队后仍核原 privacy generation，诊断丢弃与失败不改变业务回执。

## 成本与验收边界

在现有单块 JSON body 表示下，真实冷体变化仍需编码并提交相应 B 字节；实际需要检查的来源、返回或更新的 k 项事实仍须处理。来源版本与出站映射可能保留已无消费者的历史键，完整跨 owner 回收证明尚缺；reception 的历史衰减聚合仍为既有近似，旧聚合不能逐源重建。这些局部必要工作不证明全系统已达到全局理论最优。

Desktop PR #39、#40 已合并，本轮未发布新版本。main `bee5692292d8601033798cb2772a075b8db8004b` 的 [CI run 41541324476](https://github.com/Institute-of-Agent-Life/Yuki-Desktop/actions/runs/41541324476) 最终失败：五个 job 成功，macOS Intel job 的 E2E 为 183 通过、6 跳过、2 项业务阶段超时，后续该平台打包未执行。同源码树的另一轮两例通过，但不能抹去此次失败或排除产品回归；这不是已完成的 Desktop 全平台验收。
