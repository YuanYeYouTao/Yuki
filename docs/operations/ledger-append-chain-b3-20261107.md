# 账本追加链收敛（B3）实施记录

日期：2026-11-07

依据：[删减任务书](../architecture/Yuki-文件膨胀与函数爆炸删减任务书-2026-11-07.md) §4.1、§5 B3。基线 main `69420ce8`（任务书行号基于 `5af28501`，两者在本链上一致）。

状态：本地修改、验证、提交并开 PR；**未合并、未上线**。

## 1. 改动

只动账本追加链：`persistence/scoped_event_uow.py`、`identity/receipt_compat.py`，以及共用兼容检查的另一调用方 `identity/canonical_uow.py` 的两处调用。不新增生产文件、DTO、包装层或配置项，不改权限。

- `append()` 先构造一次候选 `ChatEventModel`（沿用已有 ORM 类型），它既是新追加要存的行，也是已有事件必须相同的对象。`_append_canonical` 只接收候选行和真正不在行上的入参（scope、原 segments/payload 供返回记录、两个 binding、`source_event_id`、分词结果）。
- `_find_existing_live`、`_find_existing_plugin_external`、`_require_causal_source` 直接读候选行，不再逐字段重抄签名。
- 兼容性检查改为 `require_compatible_v2_live(existing, candidate, *, receipt, event_type, normalized_segments=None)`，直接比较两行。
- 3 处 `_reuse_identical_live` 调用（已有、回执竞争、插件外部事件竞争）合并为 1 处：三条路径只负责找到 `existing`，最后统一比较并复用。删除 `_reuse_identical_live`、`_require_compatible_live`；回执竞争改用同一个 `_find_existing_live`，删除只为它存在的 `_load_receipt_claimed_event`。
- 删除死参数 `automation_id`、`automation_run_id`、`sender_nickname`、`sender_group_card`（对应列由模型默认值写入，与原先显式传入的 `None`/`""` 相同）。
- 删除重复判断：群 Conversation 分支对 `scope_type` 的第二次判断；私聊 `person is None` 抛错后的 `assert person_binding is not None` 及其前面的 `None if person_binding is None` 分支（已由上方抛错排除）；`append()` 中三处 `assert result is not None`——删除 `read_only` 后 `_append_canonical` 返回类型不再含 `None`，mypy strict 通过。
- `read_only` 删除理由：只读 reader 仅在同一快照中已经找到原事件后才进入完整路径，完整路径在同一快照中必然再次找到并复用该行，不会走到写入。

### 死参数核查

用 AST 扫描 `src tests plugins scripts packages tools examples` 中所有 `append` / `append_external` / `_append_canonical` / `partial` / `seed_event` / `seed_scoped_event` 调用：四个参数只在 `scoped_event_uow.py` 自身的 `partial(self._append_canonical, ...)` 中出现。带 `**kwargs` 的转发点有 `tests/support/event_seed.py`（`seed_event` / `seed_scoped_event`）、`tests/unit/test_history_query_repository.py`（只走 inbound）、`tests/unit/test_app_notices.py` 与 `test_plugin_session_material_contract.py`（monkeypatch 包装原调用），均未传入这四个名字。字典键形式（`"sender_nickname": ...`）的出现均属事件视图、自动化工具与迁移，不进入追加链。

## 2. 失败矩阵（先写，再改代码）

改代码前先写 `tests/integration/test_ledger_append_chain_matrix.py`，对同一输入序列记录每步结果（created / reused / 错误类别），最后把 `chat_events`、`canonical_event_receipts`、Conversation 水位用首见标签替换随机 ID 后写入 JSON。

| 组 | 情况 | 期望 |
| --- | --- | --- |
| 私聊（已有 Conversation，走只读预查） | 首次追加 | created |
| | 同 key 同内容 / 只差空白与 JSON 键序 / 不给时间 | reused 同一事件 |
| | 同 key 异内容、异 segments、异显式时间、异发送者、异方向 | `receipt_conflict` |
| 重复抑制 | 回执指向的事件已被标为 `duplicate`（无 keeper） | `receipt_conflict` |
| 回复引用 | 同 Conversation 的 `reply_to_event_id`：创建并复用；别的 Conversation 的事件 | created / reused / `receipt_conflict` |
| 插件外部事件 | 创建；同 key 同内容、payload 只差键序 | created / reused |
| | 同 key 异 payload、异摘要；缺 target | `receipt_conflict` |
| 因果来源 | 插件回复无 cause；cause 在别的 Conversation；cause 是普通消息；普通消息带 cause | `causal_source_required` / `causal_source_invalid` / `causal_source_invalid` / `causal_source_not_allowed` |
| | 合法插件回复创建并复用；system_task 以插件事件为 cause；合法 system_task；同 key 换 cause | created / reused / `causal_source_invalid` / created / `receipt_conflict` |
| 群聊（无 Conversation，SpaceBinding 证明归属） | 创建、复用；错误 SpaceBinding；带 Conversation 复用；私聊 scope 指向群 Conversation | created / reused / `canonical_owner_mismatch` / reused / `canonical_owner_mismatch` |
| 私聊（无 Conversation，IdentityBinding 证明归属） | 创建；错误 binding；不存在的 Presence | created / `canonical_owner_mismatch` / `no_presence` |
| App 入口 | App Presence + App binding，同一 `origin_key` 不与 QQ 私聊相撞 | created / reused |
| 调用方 writer | 必须给 preparation；创建、复用、冲突 | `scoped_event_preparation_required` / created / reused / `receipt_conflict` |
| 竞争 | 回执插入撞唯一键后读到同内容赢家 / 异内容赢家 | reused / `receipt_conflict` |
| | 插件外部事件插入撞唯一键后读到同内容赢家 / 异内容赢家 | reused / `receipt_conflict` |
| 插件通知形状 | preparation 携带 `yuki_context` segments、`append_external` 本身无 segments 参数，同 key 重放 | 见 §3 |

竞争用例经探针确认确实触发了 2 次回执唯一键和 2 次 `chat_events` 唯一键 `IntegrityError`（改前改后相同）。

## 3. 语义核对

- **幂等复用、同 key 异内容冲突、插件外部事件竞争、因果来源校验**：矩阵改前改后逐步结果一致，落库行、回执与 Conversation 水位逐字节一致。
- **作者不比较**：原调用中，`scoped_event_uow` 与 `_reuse_claimed_inbound` 都把 existing 自己的作者传回去比较（恒等）；只有 `record_account_fact` 传固定的 ACCOUNT / None / None。作者比较因此从共用检查移到 `record_account_fact` 原有的显式检查里，语义不变；原回执继续保有它当时接受的作者（`test_existing_receipt_keeps_its_original_buggy_author_bytes`）。
- **不给 `occurred_at` 时接受原时间**：原写法是调用方传 `timestamp or existing.occurred_at`；现在候选行 `occurred_at` 为 `None` 时跳过时间比较，等价。
- **唯一可见差异——插件通知形状**：原实现把 existing 的 segments 与调用参数 `segments`（`append_external` 恒为 `()`）比较，而实际写入的是 preparation 中的 segments；因此同一条带 `yuki_context` 的插件事件直接重放会误报 `receipt_conflict`。现在与候选行（即实际会写入的 JSON）比较，重放得到 reused。生产中该路径不可达：唯一调用方 `plugin_host/notification_repository.py` 在同一 writer 内先按 `(plugin, event_key, scope_type, conversation)` 查已有事件并 `continue`，该键是唯一索引键的超集。
- 插件外部事件竞争时，若调用方未给 `occurred_at`，候选行已在插入前补成 `observed_at`；能走到这里的只有 `append_external`，它的 `occurred_at` 为必填，所以没有可见差异。
- 写事务内只是删除重复判断和参数搬运，没有新增读取、扫描或外部等待；只读预查路径不变。

## 4. 度量（Python `ast`，参数不计 self/cls，行数 `end_lineno-lineno+1`）

| 函数 | 改前 参数 / 行 | 改后 参数 / 行 |
| --- | --- | --- |
| `ScopedEventLedgerUnitOfWork.append` | 29 / 105 | 25 / 91 |
| `_append_canonical` | 33 / 349 | 9 / 217 |
| `_find_existing_live` | 9 / 41 | 4 / 26 |
| `_find_existing_plugin_external` | 5 / 30 | 3 / 24 |
| `_require_causal_source` | 6 / 40 | 2 / 35 |
| `_reuse_identical_live`（3 处调用） | 19 / 51 | 删除；复用 1 处 |
| `_require_compatible_live` | 21 / 60 | 删除 |
| `_load_receipt_claimed_event` | 5 / 24 | 删除 |
| `receipt_compat.require_compatible_v2_live` | 17 / 61 | 5 / 47 |

文件行数：`scoped_event_uow.py` 1202 → 892，`receipt_compat.py` 204 → 190，`canonical_uow.py` 839 → 844。生产代码 `git diff --numstat` 合计 +184 / −503，净删 319 行（任务书推测 150–200）。

## 5. 验证

- `uv sync --all-extras --managed-python`
- `uv run ruff check .`：通过。`ruff format --check` 报告的 11 个待格式化文件均为基线已有，与本次无关；本次改动文件已格式化。
- `uv run mypy --platform linux src`：698 个源文件无问题。
- 引用 `scoped_event_uow` / `ScopedEventLedgerUnitOfWork` / `canonical_uow` / `receipt_compat` 的全部 45 个测试文件，加 `test_history_interfaces`、`test_confirmed_outbound_identity`、`test_plugin_notification_idempotency`、`test_message_transport_identity`、`test_app_notices`：565 passed。
- `uv run pytest tests/unit tests/integration -q -m "not native_worker"`（macOS 本机，一次完整运行 20 分钟）：4758 passed、22 skipped、**15 failed**。15 个失败都不在追加链：`test_manager_deployment`（2）、`test_workspace_copy_receipts`（2）、`test_sticker_library`、`test_sticker_publication_recovery`、`test_workspace_publish_versions`、`test_plugin_frozen_binding` 报 `AF_UNIX path too long`（worktree 路径过长）；`test_release_platforms`（4）调用 GNU `script -c`，macOS 的 BSD `script` 不支持；`test_shared_media_cache_mount` 读 `/proc/meminfo`；`test_terminal_environment` 与 `test_work_execution_receipt_regressions[False-terminal_exec]` 的原生终端超时状态。把三个源文件换回基线 `69420ce8` 后单独重跑这 10 个文件，同样 15 failed / 93 passed，与本次改动无关；本次未在 Linux CI 上验证。

## 6. 产物

`test-results/` 已在 `.gitignore`，产物留在本地，可按下列命令重现：

- `test-results/ledger-append-chain-b3/matrix-before-69420ce8.json`：基线源码 + 本测试文件的运行结果（该次测试在最后断言处失败，失败点即 §3 的插件通知形状一步，JSON 在断言前写出）。
- `test-results/ledger-append-chain-b3/matrix-after.json`：本次源码的运行结果（测试通过）。
- 两者比较：51 步中仅 `external_prepared_context_reuse` 一步不同（`receipt_conflict` → reused 同一事件）；`chat_events` 15 行、回执 12 行、Conversation 5 个逐字节相同。

重现：

```bash
# 当前源码
uv run pytest tests/integration/test_ledger_append_chain_matrix.py -q
cp test-results/ledger-append-chain-b3/matrix.json /tmp/ledger-after.json
# 基线源码：只换回追加链三个文件，测试文件保持不变
B3_FILES="src/yuki/persistence/scoped_event_uow.py src/yuki/identity/receipt_compat.py src/yuki/identity/canonical_uow.py"
git checkout 69420ce8 -- $B3_FILES
uv run pytest tests/integration/test_ledger_append_chain_matrix.py -q  # 预期只在 §3 那一步断言失败
git checkout HEAD -- $B3_FILES
diff <(python3 -m json.tool --sort-keys test-results/ledger-append-chain-b3/matrix.json) \
     <(python3 -m json.tool --sort-keys /tmp/ledger-after.json)
```

## 7. 风险

- 共用兼容检查签名改变，另一调用方 `canonical_uow` 的两处已同步；其余仓库无调用（AST 与 grep 核对）。
- 插件通知形状由误报冲突改为复用，生产不可达，见 §3。
- 候选行在复用路径上是未加入 session 的临时 ORM 对象，不写库。
