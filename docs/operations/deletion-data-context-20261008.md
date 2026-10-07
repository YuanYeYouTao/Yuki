# 删除导向重构：Context / Memory / Persistence 验证记录

本记录描述 2026-10-08 集成工作树的源码与离线验证。部署、生产复核、自然流量验收由主会话单独记录，本文不作为线上验收声明。

| 任务 | 删除与唯一 owner | 当前证据 |
| --- | --- | --- |
| CTX-01 | 删除每个 source 必须被摘要引用的覆盖门；实际 refs 决定覆盖，完整 parent DAG 决定有效性；未覆盖 observation/portable records、原工具成对记录与合法媒体保留 | `test_context_observation_sources` 部分摘要/残余/父源删除通过；ordinary_compaction、foreground_rollup_nonblocking、work_protocol_continuity 联合 56 passed |
| CTX-02 | 成功送达后派生 attribution 失败独立记录缺样，finally close；embedding get_fact 与调度均位于已提交事实之外 | 新 `test_deletion_data_contracts` 两种派生失败及真实 mutation receipt 重放通过 |
| CTX-03 | 删除隐藏 512 UTF-8 envelope 门及无 caller inject；唯一可见合同仍为三槽、每条 300 字符、24 小时与 CAS | 中文/ASCII 300 字符、tombstone、过期只读、冲突测试通过 |
| CTX-04 | PromptCompiler 不再以动态字符门拒绝 required；删除 PromptCapacityError 与 SESSION stability；完整候选请求只在最终容量 owner 比较 | compiler/no-work 容量单元通过；send 后容量路径已由上述 56 项联测闭合 |
| CTX-05 | signal 不再重置失败 deadline；policy park 单独唤醒；删除 extractive 同义入口、字符串拒绝启发式；完整请求测量切页 | 同 deadline 三次 signal、park 唤醒新回归通过；真实分页、源围栏与 structured 摘要测试通过 |
| CTX-07 | 删除 Chat native tail 的 WebSearchSourceRepository 二次索引写；Provider 原 response/journal 保留 native 来源 | private native 集成不写索引，显式 web tool 索引保留；group native 与 explicit fallback 集成 3 passed（主会话 .cache/groupweb-final.log） |
| MEM-01 | 删除 prefetch、stage、exclusive/locator/finalizer、重复写后状态和持久一写门；权限与原 mutation/effect receipt 保留 | memory mutation 46 个库/权限用例通过；Linux pinned Monty 实际 worker 的 test_codemode_memory_authority.py 1 passed，无 skip（Runtime owner 提供），同原 effect 幂等保留 |
| MEM-02 | 原 proposal 与事实同一个 evidence write；展示 usage 独立；不确定提交先读原 proposal；纯 DB 计划有限重备，不重做 resolve | rebuild 22 项通过，含 usage 异常、finish 前失败、commit ack 丢失；原 proposal attempts=1、一个事实、resolve=1 |
| MEM-03 | 删除三个旧 get_*_memories dispatch 与专用 reader 测试，统一 search/query plane；清理旧独占写/名字解析说明 | 主会话生产只读 journal 扫描三旧名零命中；当前 search 历史权限/来源/SELF/群隔离正反例通过；上线前由主会话再次复核 |
| DB-01 | 删除 persistence 包 lazy reexport 与 MediaAnalysisRepository.save_analysis alias | 产品 caller 全部使用真实模块/主方法 |
| DB-02 | PeopleRepository 唯一身份仓库，删除 UserProfileRepository 薄壳；Artifact 写失败不加无效 BaseException 再抛包装 | User profiles 及空缓存测试通过 |
| DB-03 | media.save / emoji description save_many UPSERT RETURNING 构造本次行；事务退出后返回 | 新真实 SQL 计数：六次写均 RETURNING，无后置 SELECT；更新保留源关联、ID、hit_count |
| DB-04 | SQLite 诊断仅使用 driver 主码，不用 locked 文本猜测；ControlCommand SQLAlchemyError 吞错由工具组移除 | sqlite diagnostics/failure classification 及真实 WAL 测试通过 |
| DB-05 | web source URL normalization/dedup/标题摘要准备在 transaction 前；configured max_runs 以 DELETE 子查询严格保留 | 显式 web_search 合同保留；大配置仍可能单次原子删除较多行，未暗改为延迟保留策略 |
| ART-01 | 保留 IO 前鉴权与唯一 IO 后鉴权；text/JSON/manifest/image 共用后置 fence | 四类 IO 中 generation 重置，均恰两次授权读且拒绝泄露 |
| ART-02 | GC 持久 tombstone 后文件 IO；成功 unlink 集合一次 bounded DELETE RETURNING | 三文件一个失败，恰两 writer；失败 tombstone 保留，下轮恢复 |
| PAR-01 | tick 只 hydrate 一次、读一个有界 social window；late anchor 重用局部行并重验来源/代际 | 新 tick 调用计数和新增 dirty 不丢用例通过，既有8个反馈用例通过 |
| ID-04 | canonical conversation UUID 贯穿 DTO/assembler/rollup claim/candidate/state/active/settlement/generation_matches；删除 synthetic 整数 | 同整数前缀的两 UUID 并行摘要，不相互占用 active；作用域/代际仍用原 fence |

## 验证命令及当前联测边界

- `uv run --no-sync pytest tests/unit/test_deletion_data_contracts.py -q`：13 passed。
- `uv run --no-sync pytest tests/unit/test_memory_rebuild.py -q`：22 passed（后加原 proposal transaction 前重验后再次包含在广测）。
- owned 15 文件组广测：207 项，201 passed；6 个剩余集中 ordinary send / send 后容量，返回 `WorkConflict`，已交 Runtime owner 修复原执行边界，不能降低发送断言。
- owned 类型检查：13 source files 无错误；新接口继续参加全树 mypy/ruff。
- `test_codemode_memory_authority.py` 本机 skip pinned worker，不能当作通过。主会话 Linux 门禁需要执行。

所有回归使用合成消息与临时 SQLite，未发送生产 QQ 消息，未修改生产数据库。

## 集成回归补充

- ordinary typed ResultCapture 只有 work_id 与 effect_key 均已绑定才进入持久 Work receipt；social、Memory mutation、artifact 三个 consumer 已一致修正。普通 typed 结果仍保持结构化捕获。
- ordinary_compaction + foreground_rollup_nonblocking + work_protocol_continuity：56 passed（工具组联测）。
- participation scope lifecycle / ordinary feedback / review boundaries 已全部通过；新增 dirty 不丢、每 tick 单次 hydrate、共享有界读取仍受覆盖。
- tool_effect_audit：15 passed；原 canonical event 对应可信 ToolActor fixture 覆盖 telemetry 失败、绑定变更和原执行幂等。
- Memory full quality CLI：19 cases，38 gates 全部 passed，baseline_regressions=[]；本地证据 `.cache/deletion-memory-quality/report.json`。
- 广树失败正在逐项迁移旧 fixture：不可将未结束的全树回归写成全部通过。

## 最终反例复核

- 原子 proposal 审计增加持久 OperationalError：三次纯 DB 重备后标记原 proposal/run 失败，resolve 仅一次，第二 tick 不再调用模型；新增回归通过。
- partial summary artifact 反例现在显式包含 `observation:parent` refs；每个被选 summary 独立保有 covered parent 的 handle 才准转移，不能以另一 summary 的 union 冒充。原 complete parent DAG 的失效检测保留。
- 数据域二轮 118 项：117 passed，余下一处旧 typed error 字段迁移后单项 passed。覆盖 ASR 引用归属、旧 schema fixture 回放、privacy JSON、context source、summary artifact、semantic tick 与关系 scope。
- 额外普通搜索原 artifact 按真实分页重组，ASCII/中文两项 passed；no-work capacity 六项 passed。
- 当前源码/现行 Memory 文档没有旧 get_* reader、prefetch 会话、synthetic scope owner；历史任务书与封存证据不伪装为现行合同。

- 真实旧 producer `25cd608...` 的 0096 → 0099：1 passed。历史 seed 新增历史仓库写入的 Memory PERSON 事实与共享 tool artifact；事实比较纳入 `memory_facts`、`tool_artifacts` 全列并保留重复升级与不可逆降级拒绝回归。
- 最后八个关键源码 mypy 全过；现行 Memory/rollup 文档已复核，身份文档明确 canonical UUID 贯穿所有摘要执行键。

- 同一历史 seed 的 0081 → 0099 完整链新增保留事实验证：1 passed；历史 0081 的 ToolArtifactRepository 真 owner 位于 mcp.repository，seed 按归档模块存在性调用当时原 owner，无当前代码注入旧进程。
- 最终源码删除扫描：get_person_memories/get_group_memories/get_self_memories、exclusive_namespace/requested_exclusive_write、synthetic_scope_id/prompt_scope_id、command_plane/HOST_MEMORY_FINALIZER 均零命中。
- CTX-01/02/03/04/05/07、MEM-01/02、DB-01/02/03/04/05、ART-01/02、PAR-01、ID-04：源码与上述匹配风险定向反例已闭合；MEM-03 源码及现有 journal 读审计已闭合，上线前由部署 owner 再次确认旧未决调用零。

## 全量复验期间发现并修正

- `test_observation_payload_selection` 揭示 CTX-01 的正文 IO 回归：metadata 分页不再读 payload_json。先以完整 parent DAG/版本/来源校验 selected root，正文只读取 selected summary 的实际 refs 链；已加载 summary 复用，未覆盖原件另行有界读取。270 大 parent 反例只读 root/new note 正文，原 selection version 和 scope 隔离不变。与 context_observation_sources 联测 33 passed，mypy 通过。
- `test_derived_text_transactions` 的旧 reset 调用迁真实 canonical ingress/UOW，5 passed，包括 late audio 与 reset 竞争。
- `test_no_work_after_send_recovery` 4 passed：普通聊天原送达后结束；已接纳 Work 只有 start 报告时，空响应的现有重试后只尝试一次 completion 并保守 suspended，不把内部正文冒作 final 交付，不追加礼仪模型重试或重复发送。
- 0091 真实旧 producer 新增 Memory/artifact 保留链也 1 passed。
