# Memory 来源审计与维护

以下命令必须指向明确的数据库。来源 audit 只读，不自动修复历史缺链，也不将同 key 独立事实当成冲突。

```bash
yuki memory audit --database-url <database-url>
yuki memory stats --database-url <database-url> --hours 24   # 含按周的自动提取新增与当次任务口径条数
yuki memory hygiene scan --database-url <database-url>
yuki memory hygiene apply <fingerprint> --database-url <database-url>
yuki memory hygiene rebuild-fts <fingerprint> --database-url <database-url>
```

apply 和全量 FTS 重建是明确的维护动作；先查 scan，再核对原指纹和备份。不要通过清库、重置预算或重新调用模型掩盖已提交效果和历史缺链。

自省和 Dream 的请求登记发生在实际 HTTP 派发前。payload 构造失败不产生请求计数，HTTP 重试逐次登记，累计请求账本不因重启重置。
自省只从本批新事件和工具回执创建 SELF 资料，不加载全量旧事实或裁决 SELF 候选；
Dream 沿既有正式记忆、证据和分区做整理。普通提取不再生产 SELF 候选，历史行和原检查点不自动清空、解隔离或重跑。
额度只约束真实新派发：自省已有 ReflectionCheckpoint、Dream 已有来源指纹匹配的 ready preview 时，
直接沿原 run 和原 mutation/operation 回执完成纯数据库收尾，满额不要求重新调用模型。
未保存的输出不能由 usage 重建，来源失效或提交未决仍按真实事实处理。
新自省检查点不含旧版必填的 payload/fact_map/candidate_map；镜像回退前核对未完成的原已付输出及可用 reader，
不恢复旧数据库覆盖新事实，也不重新购买输出。显式隐私擦除会按实际来源清退敏感 checkpoint JSON，
保原 run、请求账本、已提交结果和预算；迟到返回不能将已擦正文写回。
未选择事实不自动写处理 checkpoint，批准的 Rebuild 子集可提交，未决项保持 REVIEW。

Dream 模型以本簇的局部事实别名、正文、scope/visibility/kind、事实元数据与证据摘录作判断。
模型请求移除 `subject_user_id`、`group_id`、`visibility_user_id`、`visibility_group_id` 四个外部定位字段；
原 DreamInput、来源指纹与持久 preview 的历史 codec 保留，不因这次模型输入调整重算旧 preview 指纹或重写原来源证明。

维护只按事实的 valid_until 处理到期；不按来源、置信度、年龄或 scope 容量隐式遗忘。归因、强化、Activation、自动注入、synthetic quality/baseline/release-check 均已退休。统计或健康检查不证明模型实际使用或自然聊天质量。

普通提取任务的失败沿原 job 重排，保留累计 attempts 和错误，不按固定三次转终态。
无事实结果正常完成；历史失败记录不因本次策略调整改写。
完成阶段的可信 SQLite BUSY 只重备原数据库收尾，不重新执行刚完成的提取。
普通提取未新增通用的跨进程模型输出检查点，不能把该收尾修复当作所有已付输出都可恢复。

永久证据抽样删除服务及其新任务、配置和装配已退役；旧 compaction 表与历史回执继续可读。
模型输入按预算抽取摘录不删除永久来源。显式隐私删除、失效事实的状态更新与未引用临时回执清理各沿原权限和引用规则处理。

查来源、facts 与后台任务时使用原内部 Person、Space、Conversation、event/run ID；
App/TG 无 QQ 投影不构成缺失主体。新查询每次执行当前 SQL 授权和事实筛选，
纯 query embedding cache 只复用向量。查询请求、向量缓存命中、失败和实际 usage 分别看待，
缺 Token 字段不算零，也不能当成 Main 模型缓存命中率。

Embedding 的可恢复失败与 Rebuild 的未提交失败按原任务、item 或 proposal 重新领取，
沿配置的重试等待间隔保留累计尝试，不按次数转 FAILED，也不因提交准备完成就第一次封口。
Rebuild 已提交事实与 proposal 回执共同核对，确认已提交不再次写入；历史失败状态保留。

## 常驻记忆核心集

常驻记忆直接生效、无开关：每轮 system 消息末尾渲染常驻记忆段，Dream 运行处理完全部簇后对到期的群做核心集挑选。
人物不再有核心集（由人物画像替代，迁移 `0164`）。
下面的命令默认使用当前配置的数据库，也可加 `--database-url`。

```bash
yuki memory resident show --space <space-id> [--content]     # 或 --self
yuki memory resident render --conversation <conversation-id>
yuki memory resident set --space <space-id> --facts 12,34      # 写入并锁定（source=owner）；--facts '' 清空
yuki memory resident unlock --space <space-id>                 # 交还挑选步骤
yuki memory resident select --space <space-id> [--dry-run] [--content]
yuki memory resident status
yuki memory resident check > check.json
python scripts/verify_resident_check.py --database <sqlite-file> --check check.json
```

- 挑选沿用 `memory_dream` 模型路由。Dream 内每个到期主人至多一次结构化调用，每次运行至多
  `MEMORY_RESIDENT_MAX_SELECTIONS_PER_RUN` 个主人（默认 12），按已看水位、再按主人 ID 排队；
  没轮到的留到下次。调用次数单独计数写进 `memory_resident_selection_finished` 日志，不占 Dream 簇的
  `model_calls` 预算。每个主人的换入、换出 fact ID 写进 `memory_resident_selected` 日志。
- `select` 在 CLI 进程内按 bot 启动时的同一方式装配模型运行时（环境变量、模型档案与已激活的重启覆盖），
  调用记录写进同一数据库，不需要 bot 在跑；锁定的主人先 `unlock`。`--dry-run` 也会真实调用一次模型。
- 挑选只写 `memory_resident_sets`，不改事实，不产生 Dream 操作行，也不参与 Dream 回滚；失败记日志，
  该主人不变、留到下次，Dream 运行照常收尾。`DreamHealth.resident_pending_owners` 与
  `resident_oldest_pending_at`（`status` 同样输出）持续增长说明挑选长期没跑或一直轮不到。
- `check` 只对全部会话各渲染两次，输出渲染出的 fact ID 和两次不一致的会话；可见范围的独立复核在
  `scripts/verify_resident_check.py`，用自己的只读 SQL 逐条核对，违反条数应为 0。

## 经历目录

经历目录直接生效、无开关：每轮渲染经历目录与今日新经历，经历标题由写经历的模型调用写入。下面的命令
默认使用当前配置的数据库，也可加 `--database-url`。

```bash
yuki memory episodes render --conversation <conversation-id> [--through-fact-id N]
yuki memory episodes status                  # 水位所属运行、运行中的 Dream、有／无标题经历数、各会话条数
yuki memory episodes backfill-titles --dry-run --limit 50
yuki memory episodes backfill-titles         # 只写 title IS NULL 的 active 经历
yuki memory episodes set-title --fact <fact-id> --text <标题>
yuki memory episodes check > episodes.json
python scripts/verify_episode_check.py --database <sqlite-file> --check episodes.json
```

- 水位是最近一次已收尾 Dream 运行的快照；`status` 的 `active_run` 长时间停在 running 说明 Dream 停住，
  目录沿用上一次水位，新经历都留在当前消息里（至多 20 条）。
- `backfill-titles`：Dream recompose 产生的经历直接取 focus，不调模型；其余按 fact ID 升序、按
  `MEMORY_DREAM_MAX_INPUT_CHARACTERS` 分批走 `memory_dream` 模型路由，模型只能引用本批别名。只更新 `title`，
  不改 `updated_at` 与正文，所以不会把这些事实重新排进 Dream。`--dry-run` 也会真实调用模型。
- `--dry-run`、`render` 与 `set-title` 的输出含记忆派生内容，只在部署环境内查看，不贴进仓库或 issue。
- `check` 只对全部会话各渲染两次，输出列出的 fact ID 和不稳定的会话；可见范围的独立复核在
  `scripts/verify_episode_check.py`，违反条数应为 0。

## 人物画像

每人一份公共画像、一份私人画像（迁移 `0164`，合同见
[P4 任务书](../architecture/Yuki-记忆分层P4人物画像任务书-2026-11-22.md) §4–§9）。版本表只追加：
回滚与写入都生成新版本，不改写历史。`<画像>` 指 `--person <person-id> --audience public|private`，
人物 ID 按根解析（并入的旧 ID 落到根人物的画像）。命令默认使用当前配置的数据库，也可加 `--database-url`；
输出都是 JSON。

```bash
yuki memory profile show <画像> [--content]          # 版本号、当前版本来源与原因、锁定、签名、条目小节与编号
yuki memory profile history <画像>                   # 全部版本：来源、原因、时间、条目数、所在 Dream 运行
yuki memory profile diff <画像> --from N --to M [--content]   # 按条目：新增、删除、改写（共用 fact）、不变数
yuki memory profile rollback <画像> --to N           # 以 vN 的条目生成新版本（source=owner）并锁定
yuki memory profile lock <画像>                      # Dream 挑选跳过；程序清理照常
yuki memory profile unlock <画像>
yuki memory profile generate --displayed --out plan.json        # 或 --person <id> [--audience …]
yuki memory profile apply plan.json
yuki memory profile members --space <space-id> [--pin <id1,id2,…> | --unpin]
yuki memory profile check                            # 有违规时退出码 1
yuki memory profile status
```

- `rollback`：vN 的条目先按渲染时校验（§7.4）过一遍：任一编号沿后继解析后不再可引用的条目整条删除，
  输出的 `dropped` 列出位置与失效编号；后继可引用的换成后继（`replaced`）。新版本原因写
  `owner rollback to vN`，画像自动锁定，要交还 Dream 维护就 `unlock`。版本不存在时退出码 1，不写库。
- `generate` 只写计划文件，不写库：对每份画像在 CLI 进程内用 `memory_dream` 模型路由跑一次结构化调用
  （装配方式同 `resident select`）。`--displayed` 取最近一次已收尾 Dream 运行的窗口（上一次已收尾运行的
  `started_at` 至该次 `started_at`；只有一次时取其前 24 小时）里的维护集合：各群名单（所有者固定的群用固定名单，
  窗口内没人发言的群沿用已存名单）与窗口内私聊过的人都生成公共画像，私聊过的人另生成私人画像，同一人公共在前。
  `--person` 省略 `--audience` 时两份都生成。可用事实为空的画像不调用，列在 `skipped`；输出违反 §8.4 的
  （未知别名、引用 `ref_N`、超长、空正文等）整份作废，列在 `rejected`，退出码 1。
- 计划文件每份画像记录 `base_version`（生成时的当前版本号）、`signature`（可用事实签名）、`reason`、
  `generated` 与 `entries`；所有者直接改 `entries`（或删掉整份）后再 `apply`。计划含正文派生内容，只在部署环境内
  查看与编辑，不贴进仓库或 issue。
- `apply` 逐份校验：当前版本号变了拒绝 `version_changed`，可用事实签名变了拒绝 `signature_changed`，结构或
  编号校验不过拒绝（列出失效编号）；其余照常写入，一份被拒不影响其他份。写入是 `source=owner` 的新版本并记下签名；
  `entries` 与 `generated` 不同的那份自动锁定，相同的不改锁定状态；与当前条目相同的不加版本。有拒绝时退出码 1，
  重新 `generate` 后再 `apply`。同一份计划不能应用两次（第二次全部 `version_changed` 或 `signature_changed`）。
- `members --pin` 写所有者名单（`source=owner`，最多 5 人，按根去重），Dream 收尾不再重算该群；`--unpin`
  改回 `dream`，名单不变，下一次 Dream 收尾重算。
- `check` 对全部有条目的画像做渲染时校验，输出每份画像的条目编号（`rendered`）与违规（失效条目的位置与编号、
  后继映射）。只读，不写库；版本快照里的历史条目不在检查范围。
- `status` 输出最近一次窗口、各群显示名单、维护集合、到期画像（维护集合内、未锁定、可用事实非空且签名与上次
  处理时不同）、各群名单行与每份画像的版本号、当前版本来源、锁定、上次尝试时间与签名是否最新。
  作废次数不入库，看 `memory_profile_rejected` 日志。

## 提取收紧与当次任务指令清理

自动提取与 Rebuild 只有一份合同 v8（`memory-v2-extraction-v8` / schema 8，直接生效、无开关）：当次任务指令
不提取、同一批同义只出一条，提取输出不带 `category`，写入时 `category` 取 kind（fact / preference / episode），
经历的 `title` 照常给出。Rebuild 运行在规划时把合同版本记进 `extraction_fingerprint`。合同不影响主 Agent
提示词、恢复哈希与工具清单，存量事实的 `category` 不改。

Dream 每次运行的簇上限 `MEMORY_DREAM_MAX_CLUSTERS_PER_RUN` 默认 24，与调用预算
`MEMORY_DREAM_MAX_MODEL_CALLS_PER_RUN` 相同（每簇至少一次调用），P1 挑选步骤另计。积压看
`DreamHealth.unreviewed_facts`：active / 争议、且没有 checkpoint 或 checkpoint 签名已变的事实数。
需要更快消化时可临时调高这两个配置几天再改回。

存量清理用同一条提取规则分类，所有者审阅计划后才失效，可撤销；不按时间、重要度或使用次数选：

```bash
yuki memory hygiene task-scoped scan --output plan.json [--limit N]
yuki memory hygiene task-scoped show --plan plan.json [--content]   # --content 只在部署环境内使用
yuki memory hygiene task-scoped apply --plan plan.json
yuki memory hygiene task-scoped revert --plan plan.json
```

- `scan` 只读：候选是 active、`source_type=automatic`、非经历、Person / PersonGroup / Group、
  `authority` 不是 explicit 的事实，按 fact ID 升序每批 40 条交给 `memory_dream` 模型路由（CLI 进程内
  装配，与 `resident select` 相同；结构化输出只引用 `item_N` 别名）。明确事实、SELF 事实与经历不进候选。
  计划文件只有被判为当次任务指令的 fact ID、Dream `fact_signature` 与扫描时间，没有正文；日志只有 ID 与计数。
  每批在读会话里读完再调模型，不持有写事务。
- 所有者可直接删掉计划里不想失效的条目；`apply` 只认文件里的 ID。任一条已不 active 或签名变了，
  整批拒绝（退出码 1，列出变化的 fact ID），需要重新 `scan`；全部一致时逐条以
  `administrator_invalidated` 失效，每条一个短写事务，写时再核一次签名。
- `revert` 只恢复计划里仍以 `administrator_invalidated` 失效的事实；用户撤回等其他原因失效的不动。
  单条也可用管理入口的 `restore_fact`。
- 被清理的事实若在常驻核心集里，下一轮渲染自然不再出现。

当前合同的离线重放（不写库，调用生产提取模型）：
`python scripts/replay_extraction_tightening.py --source <sqlite-file> --events sets.json --self-name <角色名> --output replay.json`。
源库只读打开并备份到临时文件，结果只有事件 ID、claim 条数、kind 与正则口径计数。

当前合同见 [Memory](../architecture/memory-v2.md)与[来源审计](../architecture/memory-v2-quality.md)。
