# 2026-10-18 前台工作删减实施与验证

基线：`main a12f9dd1`、schema `0135`。本次沿[原任务书](../architecture/Yuki-前台链路删减与并发边界任务书-2026-10-18.md)实施参与准备、自报、Social 和媒体重复工作删减。新增并行仍由 [#475](https://github.com/YuanYeYouTao/Yuki/issues/475) 独立研究，不是本次交付依赖。

## 实际修改

- 显式入口将已有 EventRecord 和 RuntimeConfigSnapshot 交给 binding 准备；场景原六次关联读取合为一个查询。原事件、场景和配置的准备移到现有 Controller 应用锁外，锁内读取当前场景；Controller 应用、水合、冷装载和保存仍保留原锁。
- 普通接纳反馈与自报共用本次真实 admission/source 的局部准备；热自报不再扫描 Social 近窗、配置和历史页。admission 已提交但派生反馈没来得及应用时，沿该 admission 幂等补齐原输入。quiet、原 Jev 纠正排队与快照保存仍在观察主响应返回前完成。
- store 的关闭检查保留在原应用锁内，准备期间关闭不重新创建 Controller；取消保存先确认原提交和 revision，不重买模型、不重发效果。
- Social 复用已校验消息、同次配置、分条结果及各条参数；删除 reader 目标存在性预读，writer 首次 DML 前仍检查真实目标。原 claim、身份/路由核验、分条等待、ACK 和结算不变。旧 sequence payload 继续识别，原 call 恢复原回执。
- 裸图片字节不在包装层预先重复哈希，由既有解码器统一计算；下载器提供的期望 hash 仍核验。ASR、文档提取、转码及成员解析未发现本次可以直接删除的同阶段重复，因此未改其派发或失败语义。

没有新增运行时队列、缓存、TTL、游标、锁分片、预装配、默认 Code Mode 或同会话多 Main。

## 处理量对照

参与准备使用真实 SQLite、实际控制器、64 条近窗历史、驻留 owner 与合成 Main 自报；每个边界 5 次等价样本。SQL 是主数据库经 SQLAlchemy 捕获的语句，包含显式 BEGIN，不包含独立参与快照库内部 sqlite3 语句；原快照仍真实保存。时延来自本地受控调用，受并行研究负载影响，不作生产提速证明。

| 边界 | SQL 前→后 | 历史行前→后 | 本地中位耗时前→后 |
| --- | --- | --- | --- |
| binding，只有 event ID 的调用 | 31→27 | 64→64 | 34.537→35.845 ms |
| binding，显式入口复用事件和配置 | 31→21 | 64→64 | 34.537→31.494 ms |
| 普通 Main 自报 | 36→6 | 64→0 | 44.140→12.022 ms |

兼容 binding 的墙钟没有改善，不声称全部路径加速。上表不是平台入站到首个 ACK，也没有模型或平台网络耗时；组件中位数不能相加成整体收益。

Social 使用真实 SQLite、原 service/receipt 和受控 Bot：每场景 8 次，舍弃首样本，其余 7 次统计；分条上限配置为 20。SQL 与提交数稳定，CPU 粒度较粗且有其他测试负载，因此只报告处理量。

| 发送场景 | SQL 前→后 | 首发前 SQL 前→后 | 提交次数前→后 |
| --- | --- | --- | --- |
| 单条 | 40→39 | 20→19 | 3→3 |
| 三条 | 105→105 | 21→21 | 7→7 |
| 十二条 | 375→375 | 21→21 | 25→25 |
| 三条，现场读取配置 | 109→107 | 25→23 | 7→7 |

Social 消息校验 2→1，多条分条计算 2→1。没有减少必要 claim/settlement 提交，不以少一遍 CPU 处理冒称 writer 竞争已经消失。裸 PNG 受控样本为 290 字节，原件哈希扫描 2→1，media_hash、帧的 data URL 字节和来源前后相同；该项仅影响裸字节准备，不宣称当前入站 DownloadedMedia 路径获得同样收益。

## 保留绑定历史读取的实证

实际 Social 发送 ACK 和事件已入账，后台尚未 tick，另一人物随后引用这条新 SELF 消息：完整准备能冻结原讨论、SELF reply ref 和无歧义单元。模拟直接删去热绑定水合，会把当前输入冻结到自身新讨论、丢 reply ref 并形成歧义；后续 tick 仍复用同 revision 的错误投影。此路径的 64 条读取因此保留，原任务书的“只处理新增事实”设想已纠正，不为省这份真实输入另建影子缓存或补渲染机制。

## 验证与交付边界

检查本次 CI 时发现原 `main a12f9dd1` 的[全量基线](https://github.com/YuanYeYouTao/Yuki/actions/runs/39127802227)已是 2 failed / 3067 passed / 90 skipped，耗时约 33 分钟。本分支附带修复这两项，避免把已知基线红灯留给本次优化：

- 复用 [#476](https://github.com/YuanYeYouTao/Yuki/pull/476) 已提出的独立修复：ingress 透传已有 `reply_to_event_id`，优先按原内部 ID、同 Conversation 和 keeper 解析，不先丢掉它再从平台编号反查。引用本身带来的真实正文继续进入正常 Main；原测试不再把它断言为无输入，并同时核对内部 ID 与引用正文。没有引入 #476 的人物授权、迁移或管理重构。
- 复用 #476 的 compact 前缀断言：历史长度翻倍时选中的有限前缀一致，不固定要求覆盖 20 个事件。另外删除旧 `prepare(messages=())` 得到的空序列化计数与 `0≤0` 断言；保留真实 coverage 与观察到的耗时，不把它描述成 SQL 或 CPU 扩展性实测。

新增真实 SQLite 回归覆盖锁外准备、等待期间 reset/close、同 scope 冷恢复唯一性、来源修订/撤回、已提交未反馈输入的局部恢复、重复 continuation 不重复累计，以及取消保存后的真实 revision。另有真实入口到受控 Telegram ACK 的整链测试：quiet 先持久化，再依原顺序发送两条；同次 Main 的合法发送不会被自己的 quiet 追溯阻止，出站账本与原 Social 回执对应，重复入站不创建新 Main 或发送。

该整链测试检查逻辑请求前缀和固定 tools；Provider wire、当前轮资料、输入交接及 usage/cache 字段另复用原回归。所有测试使用假模型与受控传输，没有调用付费模型或真实 QQ/TG 账号；它们不证明生产缓存命中率或自然流量延迟。

提交前实际验证如下；分批存在重叠，不相加成独立用例总数：

| 检查 | 结果 |
| --- | --- |
| 参与模块 `test_participation_*.py` 及新增交错回归 | 95 个不同用例通过（94 项组合，另 1 项新增用例单独通过） |
| 普通交接、quiet/分条整链、TG runtime、平台执行来源、当前轮资料合同 | 57 passed |
| 固定平台提示、当前轮资料、输入交接 wire、usage/cache 字段 | 34 passed |
| Social 并发、身份、原生发送、完整回执、canonical 来源与 SELF 自动化 | 分批去重 84 项通过 |
| 原生媒体、共享原件与相关准备回归 | 25 passed，1 项 POSIX-only 在 Windows 跳过 |
| 最后复用已验证 UnitBinding 后的冷热反馈、恢复及发送整链复验 | 31 passed |
| 基线修复后的空输入、compact、canonical ingress、原接入回执与反馈发送整链 | 63 passed |
| `ruff check .`、受影响 Python 文件格式、`git diff --check` | 通过 |
| `mypy --platform linux src` | 631 份源码通过 |

本机 Windows 默认平台的全库 mypy 会报告未改 POSIX 环境模块的 29 项缺符号；按运行时和 CI 的 Linux 目标检查通过，没有为测试改变 POSIX 业务源码。现有 canonical conversation/legacy alias 循环外键 SAWarning 仍存在，没有把该历史警告当成本次新增失败。

本地可从仓库虚拟环境运行 `python -m pytest -q -s tests/integration/test_participation_foreground_cost.py` 取得当前参与处理量；新增行为回归位于 `tests/unit/test_participation_foreground_preparation.py`、`tests/integration/test_foreground_feedback_delivery.py` 和原 Social/媒体测试文件。它们不是固定 SQL 次数或毫秒通过门槛。

以上为提交前本地结果，远端 CI 以本次 PR 为准。合并、部署和自然流量验收未执行；没有以旧 PR 的绿色结果或健康检查替代本次验证。
