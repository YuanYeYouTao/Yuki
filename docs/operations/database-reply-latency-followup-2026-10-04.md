# 回复延迟上线检查与继续修复

日期：2026-10-04，运行时排查基线 `7b35e8cac188de55cb0984778748c5e117a552a5`；
本轮代码同步至 main `9f681e4f`（其额外变更为文档与外部网关部署资产清理）。
本轮分支 `codex/database-latency-followup`；代码实现、专项验证和独立复审完成。
前轮部署与当前修复的状态分开记录；本轮提交、CI、合并和上线以PR与独立部署报告为准。

## 上线检查

前轮 [PR #236](https://github.com/YuanYeYouTao/Yuki/pull/236) 已合并部署，Bot 启动于
16:52:36（UTC+08），版本0092，末次检查 healthy、重启0。仅以自然聊天进行只读检查，
没有发合成 QQ 测试消息，没有通过清空状态或扩大超时掩盖问题。

截至17:30:16，两会话共12轮完成且有确认发送的回复：

| 指标 | 标准中位数 | 最慢 |
| --- | ---: | ---: |
| 开始处理到首主模型调用准备 | 2.7秒 | 26.8秒 |
| 开始处理到首确认发送 | 19.1秒 | 60.1秒 |
| Bot完成归一化到首确认发送 | 22.0秒 | 79.8秒 |

其中4/12轮超过30秒，2/12轮超过60秒（73.2、79.8秒）。另两轮被新输入取代，
一轮在截点尚未结束，未混入成功统计。模型均为实际 chat_agent/gemini-3.8-flash。
升级前45分钟只有两轮可比较回复，不计算固定改善比例，也不以12样本宣称稳定p95。

归一化时刻不是 QQ 客户端发送/网络到达时刻；处理开始已在部分协调和前置查证之后；
model_start 包含实际 HTTP 之前的路由/调度；确认发送在传输成功与持久回执之后。
区间有嵌套，不能将整轮减去模型时间直接认作 SQLite 等待。

长尾按内部事件83188、83189及原turn匹配：
`a3470b614602462a9edda1fdbfacc6f6`、`292b7d270dba49cea627eef12f300f05`。
主模型准备前分别已有26.8、25.2秒，后者入账准备到处理开始还等待26.6秒。

日志检查至17:36:50，记录7次写竞争：17:30:22两个 space_bindings UPDATE 和一个
runtime_work_scopes UPDATE 因原生错误码5失败，耗时5.5–5.9秒；17:30:27聊天入口有
两条 database is locked 错误记录。三次竞争时同引擎持锁者的最后成功操作都是
canonical_rollup_signals INSERT，协程 TaskHandle._run_coro，持有下界约6.02秒；
这不是该 INSERT 单条耗时。此前 chat_events INSERT 在SQL阶段也用时4.195秒。
17:31:03一次 BEGIN IMMEDIATE 成功获取阶段耗时12.709秒，不是持锁12.709秒。

主机1.6GiB内存、swap约1.5GiB。17:24附近swap-in和I/O等待出现明显峰值，
末次瞬时I/O等待降回1%；压力呈突发性。时间重叠不能证明全部延迟来自swap。
18:09:56再取两个1秒样本，第二个样本swap-in4336KiB/s、I/O等待60%；
I/O full PSI avg10为62.62%、memory full为36.15%，表明压力仍会重复出现。
18:16容器cgroup确认SnowLuma约506MiB memory与1.168GiB swap，Bot约282MiB
memory与119.5MiB swap；主要存量换页来自QQ/SnowLuma。所有容器OOMevents为0。
18:13仅两秒pidstat样本中Bot读约1.9MiB/s、写1.1MiB/s，也参与I/O；这些瞬时速率
不能证明前台、Dream或compaction谁造成17:24长尾。18:16容器I/O压力已下降，
累计BlockIO及pgmajfault不按不同容器启动时长比较速率，也不以巨大VmSize判断泄漏。
前轮停止Bot时的冻结备份有2441个memory facts、3020条evidence，候选全聚合尚非
数万规模，不能将它认作25秒主因。compaction runs已有72,599个，而items仅187个；
空轮新建run的复现与该积累相符，旧item也可能被删除，不直接声称其余run全部为空。
本轮阻止空run继续增长，未清理历史事实或关闭维护功能。
Canonical入账在信号后只读primary alias并提交，没有再扫描历史或等模型；
不能仅凭最后成功SQL把6秒持锁归因于信号函数。同步日志背压仍只是待证实可能性。

## 最小修复

- 群名额外刷新仅在获得非空名字时写入；空返回、冷却或无解析器不再重复更新
  space_bindings。普通人物观察仍维护真实群绑定的last-seen。真实群名刷新先获取短
  writer再读当前owner；展示资料SQL错误只记录类型，不中断后续聊天身份与账本校验。
- 普通聊天没有同来源、actor与交接边界的候选Work时，上下文准备跳过初次空租约，
  实际激活仍重新读取并取得原租约。同源已有Work行为保留；准备期间新增/取消有回归覆盖。
- 作用域busy/obsolete领取、失效release/renew只读返回；有效变更的owner/fence、
  generation和SQL执行时期限条件保持原状。没有统一重试，没有扩大busy timeout。
- Evidence compaction空候选且无running run时零DML、不新建run；既存processing按原
  run/item恢复，必要完成状态仍持久化。生产确认此worker启用，batch20、poll60秒。
- 短期状态在已初始化workspace manifest上使用只读事务，过期内容投影为空并保留
  原slot/revision/期限。实际update保留原CAS、容量与过期擦除；首次workspace schema
  初始化仍需必要写入，不新增缓存或后台队列。
- 普通聊天在首次主模型之前汇总一条固定准备阶段的单调时钟耗时元数据，复用原内部
  轮次与有界异步诊断队列；细分上下文阶段不重复计入总数。诊断不改变权限、事实或
  取消语义，用于部署后定位尚未精确分摊的25秒前置等待。
  记录截止 Runner 启动前；初次上下文校验不包含 Runner 首次dispatch内真正的
  projection发布，也不包含输入协调、HTTP与传输等待。
- Dream纯数据库操作及ORM flush原生517在原操作ID下最多重备三次；仅成功回滚后
  的耗尽登记为已知失败。核对原cluster的实际committed回执，保留已提交计数；
  登记或提交确认不确定时停止worker并在health显示错误，不重新调用模型或清零预算。

没有数据库迁移、新状态表或Provider行为变化。原事件ID、Work/run、预算、journal、
输入与效果回执保持原合同；数据库重备不重跑模型、工具或发送，未决Dream登记错误
停止同进程自动tick。显式重启仍按原持久回执与剩余预算恢复，不声明所有重启均无模型调用。

## 验证与交付

新增54项专项回归均通过；Ruff check及format（1140文件）、Linux mypy（698源码）通过。
完整pytest及GitHub CI另行核验，不把专项结果充当全量通过。真实SQLite/WAL持锁回归覆盖无变更
路径、读后owner/代际变化、租约到期、并发领取、原执行与计量、准备后新Work和取消。
群资料基线5项回归在原代码失败，修复后及相关profile组合25项通过。
最终群资料6项通过，包括身份拒绝不被可选展示资料SQL容错吞掉。Dream13项专属及相关
input/recovery共19项通过；实际提交确认丢失与4类失败登记错误均覆盖。阶段诊断10项及
旧execution trace/Work预检组合33项通过。各组合存在重叠，不累加为独立测试总数。
独立复审修正Dream外层异常吞掉后再次tick与真实提交被覆盖为零的两个风险，最终复审通过。

本轮消除已经复现的冗余写入；必需的账本、有效租约和效果回执仍会写数据库。
证据压缩候选的全表聚合仍是读侧成本；真正入账SQL慢与主机压力尚不能靠这些空写修复
宣称消失。合并、Bot-only上线和自然流量验收分别核验，不把fixture零写当线上速度证明。
