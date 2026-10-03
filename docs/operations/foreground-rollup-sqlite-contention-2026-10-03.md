# 前台回复与 SQLite 写锁修复交付记录

日期：2026-10-03。基线：PR #228 / `3d9e273`。设计见
[任务书](../architecture/foreground-rollup-and-sqlite-contention-taskbook-2026-10-03.md)。

状态：本地实现与交叉审查进行中；以下不表示已合并、已部署或真实群聊验收完成。

## 实现

- 小预取按真实剩余容量补齐完整事件；完整输入能 fit 时不等待软整理。
- 当前激活保留原冻结前缀；下次正常激活达到水位时采用就绪语义摘要一次，保留所有后续事件。
- 私有线索没有就绪摘要而原请求 fit 时不购买辅助摘要；真正超限的候选按真实容量接纳。
- Rollup semantic/overlay 使用显式 deferred 读快照，517 有限纯数据库重备复用原付费输出。
  租约核验包含 generation、原 owner/token、SQL 执行时有效期；领取与续租时刻在取得 writer 后确定。
- Memory 来源窗口游标扫描零修补也推进，固定 high-water 后回绕；治理恢复读取有界候选并重核完整 claim。
  0090 仅增加 reflection `status/claimed_at/id` 与 Dream cluster `status/id` 索引。
- 精确已有 Canonical/Social 回执、重复 observation/input、已准备来源与插件正常上下文只读返回。
  正式 selection/journal 发布、显式 resume 和 input 状态推进仍保留原短写与原子边界。
- JSON/hash/DTO、Dream 完整聚合及观察引用准备前移；来源、隐私、owner、回执与预算检查保留。
- 健康检查提供固定分桶的 SQL、获取、首次写入、commit/rollback 和持有观测下界。
  驱动、连接池、SQLite 内部等待仍明确未知；诊断队列单独计量，不递归写数据库。

不增加第二 runtime、压缩调度器、永久候选存储或发送状态机；不提高 busy timeout。

## 对照验收

| 项目 | 证据及边界 |
| --- | --- |
| V01 | 新 `test_foreground_rollup_nonblocking`：真实 Processor/Runner/TaskModelExecutor、SQLite、Social fake transport；后台摘要仍被 barrier 阻塞时，ordinary 与启用 Work 的普通入口已回复，Work 数为零，原文完整 |
| V02 | `test_rollup_snapshot_read_budget` 与 history soft coverage：分组后实际成本、小预取补齐、多页整条事件及真实超限 |
| V03 | Runner/容量原检查继续验证完整请求；准备共享 Profile 和首请求工具形状，媒体/opaque 与后续尾部仍在最终预检 |
| V04/V13 | `test_foreground_rollup_adoption`：压缩源读取后新聊天到达，当前冻结片段不变，下一轮一次采用且后续事件不丢；原 wire 回归继续验证工具和输入顺序 |
| V05/V06 | 现有主入口、Work、来源/隐私、reset、hold、unknown/opaque 回归；不将只测 Repository 冒称真实所有入口验收 |
| V07/V08 | `test_rollup_commit_snapshot`：真实 WAL 第二连接追加、编辑、reset、lease/generation 与 hold；517 重备同次 paid 候选；实际提交后确认异常不自动重提 |
| V09/V10 | 新 duplicate/memory/plugin/observation fastpath 回归：另一个 writer 被持有时只读完成；SQL 轨迹无空 DML；候选竞争、持续追加下游标回绕、查询计划 |
| V11 | Dream recovery snapshot、首写边界及观察引用测试：聚合完整、517 重备原 operation，source/privacy 拒绝与原子发布保留 |
| V12 | SQLite phase/physical close、真实两连接竞争、慢 commit/rollback、失败与 savepoint；未知计时不冒称精确锁等待 |

各子组通过次数不能相加冒充最终全量次数。最终冻结提交的 CI 与部署单独登记。
常驻回归只用 fake Provider 和隔离 SQLite；手动真实 Gemini/DeepSeek 报告单列。

## 本次部署的软参数

用户最后确认：聊天约 162000 token 触发，压后原文保留目标维持约 54000；
这是可调整的整理政策，不是请求准入标准。每批 256 条 / 32768 字符保持原值。
源码默认保持兼容；部署使用现有共享整理窗口 180000、聊天比例 0.90 / 0.30。
当前大容量路线的 Work 比例折算为 0.45 / 0.25，名义触发 / 目标仍为 81000 / 45000。
共享窗口还参与 Work 压后增长间隔，因此不能声称全部 Work 整理行为完全不变；
当前已有压缩检查点可能较晚再次整理，原执行、预算、回执和实际容量检查继续保留。
参数随 Bot 配置切换一起生效；不修改 Profile，不重建历史或正在运行的请求前缀。

## 缓存数据口径

此前 17 份手动报告共 110 次物理调用：Gemini 68、DeepSeek Chat 22、Responses 20。
Gemini 28 次有 cache 字段，40 次缺失；已知 input 618571、hit 484766，加权 78.37%。
这是冷轮、辅助与失败实验混合样本，缺字段不算零，也不是自然流量平均。
此前同 transcript 的 DeepSeek Responses 9 轮已知加权 95.11%，后 8 热轮 99.02%。
原报告见 [缓存实验](work-context-cache-experiment-2026-10-03.md)。

新增 18 次真实请求全部 HTTP 200、零重试，Gemini 13、DeepSeek Responses 5。

| 场景 | 实际 input/output | cache 字段与已知加权 |
| --- | --- | --- |
| Gemini C08，同 transcript 8 轮 | 159758 / 3547 | 6/8 次有字段；known input 120776、hit 96705，80.07%；其余未知 |
| Gemini C06，冷主轮＋普通整理＋3次追加 | 主请求 79694 / 1622；辅助 59236 / 1494 | 3 热主轮 input 60427、hit 48409，80.11%；辅助未知 |
| DeepSeek Responses C08，同 transcript 5 轮 | 103498 / 757 | 全有字段，hit 95104，整体91.89%；后4热轮98.99% |

主链完整工具与固定设置 hash 不变，C06 摘要通过严格 schema 后显式新输入继续。
逐物理请求、cache miss、固定设置及首次差异记录保存在 ignored
`.cache/foreground-rollup-live-*-20261003.json`，汇总说明为
`.cache/foreground-rollup-live-metadata-20261003.md`；报告不包含正文、签名或凭证。
本次真实 API 使用 TaskModelExecutor 与合成 session；它不能代替 V01 完整 Processor 的
阻塞后台回归。未把异尺寸/冷暖/不同协议历史报告称为同 fixture 的修复前后因果对照，
不据此宣称线上缓存改善。前缀保持与代理上游实际命中是不同证据，不承诺固定缓存率。
不将直接 serializer 合成场景当作完整运行时回复时延或自然 QQ 能力验收。

## 保留的边界

治理 discover 的只读自连接成本本轮未重构。SQLite 内部忙等待与驱动调度不可精确分离；
观测 engine 之外的连接不在 holder 列表。Rollup 未发布输出不保证跨进程崩溃免再次付费。
模型压缩质量、线上负载差异与真实群聊能力由后续自然样本和用户验收确认。

## 合并与上线

待最终检查后记录 PR、合并 SHA、Bot 镜像、0090 实际数据库状态、备份和健康。
仅替换 Bot，保留线上事件、预算及回执，不恢复旧数据库，不操作 AGM/SnowLuma/Mihomo。
