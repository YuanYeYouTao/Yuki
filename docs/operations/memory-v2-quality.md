# Memory V2 质量、审计与治理操作

## 当前自动写入合同

普通提取每 30 秒检查数据库；同一 canonical 所有者累计 12 条、8,000 字符或最老事件等待
3,600 秒即可领取（单批最多 12 条/8,000 字符）。一小时不是重试、lease、Rollup 或反思间隔；
未到期且无错误的 pending 属于正常聚合。明确的 `memory_change` 仍即时执行。

自动新内容须声明 retention、source_style、importance、confidence、value_reason，并通过
主体、来源与证据校验。长期价值最低为 3；有意义的单次经历可以达标，日常问候和无进展调侃
应正常跳过。Worker、重建和反思不能自报 explicit 获得用户权威；低价值结果不进入候选队列。
空提取和反思 noop 都应推进批次水位。已有事实的纠正、撤回、合并及证据维护不受首次写入门槛
阻挡。语义质量仍由模型任务判断，后端数值校验不是语义质量保证。

统计时区分事件 job、提取 batch 和实际模型 request（含重试）；详见
[指标口径](../architecture/memory-v2-quality-metrics.md)及
[P1 合同](../architecture/Yuki-Memory-P1治理任务书.md)。

生产诊断使用只读、内容无关的统计入口：

```bash
uv run qq-ai-bot-cli memory stats \
  --database-url sqlite+aiosqlite:///./data/qq_ai_bot.db \
  --hours 24
```

它报告正常等待与 ready owner、失败类别、零注入、归因覆盖、已评估使用率，以及主动读取的
成功/空结果/歧义/权限拒绝/重复/基础设施失败。重复读取复用同轮结果，duplicate 可与其最终
success/empty 同时出现。未绑定普通聊天 recall receipt 的 Plugin/Admin 查询不会为统计造轮次。

## 离线质量套件

### 证据审计口径

普通事件证据仍要求人类入站来源。SELF 的 `agent_reflection` 证据可引用 Yuki 出站，
但作者、非抑制状态、来源账号和摘录仍必须匹配。自省使用实际事件渲染文本（包含带有
不可信标记的图片识别摘要），摘录按原文或写入时的空白规范化结果核对，不能直接只在
原始正文列做 substring 判断。图片摘要不是用户原话，也不是独立事实核验。

工具证据检查回执、触发事件、canonical 会话、SELF 可见范围及结果摘录；已被正式记忆
引用的回执不会仅因过期而失效。审计与清理扫描复用相同证据规则，按有界批次检查，
报告只含数量和有限内部 ID。清理预案仍须显式应用，扫描本身不修改事实。历史缺少
证据或替代链时不得补造来源，更不能将审计误报修正解释为允许删除所有历史事实。

Dream CONTEST 可保留两个 active 事实，并把双方 `conflict_state` 标记为 contested；
这是保留争议的合法状态，不是矛盾状态错误。active 矛盾关系中任一方未标记仍报错。
缺失历史替代链必须单独调查；旧备份中存在链不代表可以直接覆盖现有数据库。

```bash
uv run qq-ai-bot-cli memory quality validate-dataset
uv run qq-ai-bot-cli memory quality run --suite full
uv run qq-ai-bot-cli memory quality compare
```

结果写入 `artifacts/memory-quality/report.json`、`report.md` 和 `junit.xml`。数据集全部是固定时间
和符号身份的合成数据；默认不允许真实模型或真实 Qwen。真实模型实验只能另行显式设置
`MEMORY_QUALITY_REAL_MODEL_ENABLED=true`，不得成为 CI 或发布门禁结果。

只有维护者明确接受数据集、门禁和实现变化后才运行：

```bash
uv run qq-ai-bot-cli memory quality update-baseline
```

该命令仍会先执行绝对门禁；失败时不会覆盖 baseline。

可选真实模型实验必须显式设置 `MEMORY_QUALITY_REAL_MODEL_ENABLED=true`；如还要测试真实 Qwen
Embedding，再设置 `MEMORY_QUALITY_REAL_EMBEDDING_ENABLED=true`。它仍只处理合成 fixture，
结果写入 `artifacts/memory-quality-real/`，不会覆盖 deterministic report/baseline，也不进入 CI
或发布 merge gate。不要在包含真实数据库内容的自定义 fixture 上启用。

## 历史性能基线

baseline 仍保存 100 用户、10,000 facts、10 个群和 100,000 条事件的既有合成性能快照，
供 release-check 确认规模合同没有丢失。3.8 canonical-only 收口时，依赖已删除 carrier 表的旧
`memory quality performance` 生成器已一并退役，不能再把文档中的旧命令当成现役入口。
当前变更使用完整 `quality run --suite full`、全量测试和 release smoke 作为执行门；未来若恢复
大规模性能命令，必须先以 canonical Person/Space/Conversation 重写生成器，不能复活旧表。

## 生产审计

生产数据库命令从不隐式读取开发数据库，必须显式给出 URL：

```bash
uv run qq-ai-bot-cli memory audit \
  --database-url sqlite+aiosqlite:///./data/qq_ai_bot.db
```

审计只读、无模型，只输出 `issue_code / severity / count / sample_ids`。它不会输出事实正文、证据
摘录、聊天内容、QQ、群号、向量、密钥或数据库路径。

## 显式 Hygiene

```bash
uv run qq-ai-bot-cli memory hygiene scan \
  --database-url sqlite+aiosqlite:///./data/qq_ai_bot.db
uv run qq-ai-bot-cli memory hygiene apply <fingerprint> \
  --database-url sqlite+aiosqlite:///./data/qq_ai_bot.db
```

apply 会重新扫描，fingerprint 不一致即拒绝。它只能：

- 将来源明确无效的 automatic/rebuild fact 版本化为
  `invalidated / administrator_invalidated`，状态事件原因记录为 `invalid_provenance`；
- 重建缺失或孤立的 FTS 派生索引；
- 为当前 embedding profile 补建缺失 job；
- 清理 terminal rebuild run 的 proposal/item staging，保留 run receipt。

它不会自动处理 explicit 事实、contested 事实、跨目标关系、歧义第三方陈述或需要人工判断的
冲突，也不会物理删除 fact/evidence。启动、健康检查和发布检查都不会隐式执行 apply。

## 正式发布检查

```bash
uv run qq-ai-bot-cli memory release-check
```

该命令组合版本、Alembic head、dataset/baseline/gate hash、最新质量报告和契约快照。加上显式
`--database-url` 时才读取指定数据库并执行 `PRAGMA integrity_check`、外键检查和内容无关审计；
不传时会给出 warning。发布检查永远只读。

## 隐私边界

- fixture 只能使用 manifest 中的符号身份和合成 ID；loader 会拒绝疑似真实 QQ、Secret、浮动
  时间、未知字段和 hash 不一致。
- report、baseline、audit 与 release-check 不保存聊天正文、事实正文、证据摘录、向量、密钥或
  数据库路径；生产 audit 最多显示 20 个内部行 ID。
- 真实模型实验必须显式启用且仍只读取合成 fixture；CI 永远使用 Fake Model/Fake Embedding。
- hygiene 不物理删除事实/证据，不修改 explicit/ambiguous/contested 数据，也不会由启动、
  healthz 或 release-check 自动触发。

## 故障排查与发布清单

- `dataset hash mismatch`：不要改 expected 掩盖失败；审阅 fixture 后重新计算 manifest hash。
- `baseline regression`：延迟必须同时超过配置的相对比例和 20ms 绝对增量才阻断；仍应先重复
  运行排除调度噪声。确认数据集或实现变化后才可显式 `update-baseline`，禁止降低污染、权限或
  行为门禁。
- `contract snapshot changed`：审阅领域/Pydantic/Plugin API 差异后显式刷新快照；Plugin API
  主版本必须仍为 `2.0`。
- `fingerprint changed`：数据库在 scan 后已变化，重新 scan 和人工审阅，不要复用旧 fingerprint。
- `production audit` 失败：先备份数据库，只对确定可治理项执行 hygiene；其余保留为人工问题。

发布前还必须完成 Ruff、mypy、全量 pytest、Alembic、Compose 配置、Bot 镜像构建、质量套件、
baseline compare、显式生产 audit 和 release-check。任何 warning 都要如实记录，不能当成已执行
的 pass。
