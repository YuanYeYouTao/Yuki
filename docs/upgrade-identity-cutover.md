# Yuki 3.8.0 永久主体 Identity Cutover（0048 / v2）

本页描述 3.8.0 的 Alembic `0048` 与 `identity-cutover --plan/--apply` 内部合同。面向部署者的
完整顺序、容器挂载与 3.7.1 回退步骤见 [3.8.0 升级指南](upgrade-3.8.0.md)。它不替代
3.7.0 的 `0042` 破坏性升级。

当前 3.8.0 预发布流程只认证已有 3.7.1 数据的升级演练；全新数据库的首个 Yuki Presence
bootstrap 尚未完成正式发布认证。

`IDENTITY_BINARY_EPOCH` 是进程内的 v1/v2 标记，与 `YUKI_VERSION` 无关。v2 二进制遇到 `identity_runtime_state=v1` 拒绝启动；v1 二进制遇到 v2 拒绝启动。

## 升级前

1. 确认 Alembic 已到 `0047` 或可从 `0042` 连续升到 head，`PRAGMA foreign_key_check` 为空。
2. 停止唯一 Bot。不要让旧实例或第二套 Compose 继续连库。
3. 对同一时间点备份：
   - `data/qq_ai_bot.db` 以及存在的 `-wal`、`-shm`；
   - `.env`、`config/`、镜像 revision。
4. 复制上述三件套作为 cutover 快照，并记录校验和。`--plan` 只核对文件存在与大小/哈希，不会改业务真源。

不要只复制主库而漏 WAL。不要在运行中的库上做普通文件副本。

## 升级

本机构建并上传 Bot 镜像后，先让 Alembic 升到 `0048`（fresh 与 0042→head 必须等价）。fresh install 仍 seed `identity_runtime_state=v1`，**必须**再做 plan+apply，生产 v2 二进制才会启动。

未知 Yuki bot 账号不会自动注册 Presence；未知群不会自动建 SpaceBinding。apply 前用 control command / 预配置补齐 Presence 与需要的 Binding。

```bash
qq-ai-bot-cli identity-cutover --plan \
  --git-revision <生产目标 revision> \
  --expected-revision <同一 revision> \
  --downtime-token <停机证据> \
  --snapshot-db <快照 db> \
  --snapshot-wal <快照 wal> \
  --snapshot-shm <快照 shm> \
  --database-url <生产 sqlite>

qq-ai-bot-cli identity-cutover --apply <source_fingerprint> \
  --git-revision <同一 revision> \
  --downtime-token <同一停机证据> \
  --snapshot-db <同一快照 db> \
  --snapshot-wal <同一快照 wal> \
  --snapshot-shm <同一快照 shm> \
  --database-url <生产 sqlite>
```

`--plan` 会阻断：revision 不符、缺少停机/快照、未排空的 job/lease、open `identity_conflicts`、不完整 shadow、未知 identity、多 Presence 路由歧义、未解决的 `pending_cutover`、同 platform message ID 内容冲突。

`--apply` 在单个 `BEGIN IMMEDIATE` 中复核 fingerprint，写 canonical conversations / aliases / routes / event mapping / worker baselines，最后 flip `v2`。失败整事务回滚。旧事件不会重新进入 Memory worker。

公共 `/healthz` 形状不变。不新增 HTTP 端口或管理路由。secret 不得进入 manifest、报告或日志。

## 验证

1. `/healthz` 仍是现有公开字段；`database=ok`。
2. `alembic_version=0048`，`PRAGMA foreign_key_check` 为空。
3. `identity_runtime_state.state=v2`，且 `cutover_id` / `source_fingerprint` / `completed_at` 均非空。
4. `people` / `groups` / `conversation_scopes` 行数在 v2 业务写入后不增加。
5. 新人消息只出现 Person + active IdentityBinding；未知群与未知 Yuki Presence fail closed。
6. v2 发送时，旧/未知 QQ 或群号只做 provenance；指向另一个已知 Person/Space 的 ID 必须 `target_mismatch`。
7. `/ai forgetme` 仍删除该人的 memberships、aliases、relationship/speech/time 子行与可归属事件；这由应用层完成，不再依赖已拆除的 people CASCADE。

## 回退

**数据回滚只允许恢复 `--plan` 记录的 DB/WAL/SHM 快照。**

禁止把下列动作当成数据回滚：

- `alembic downgrade`（0048 downgrade 只删 cutover 簿记，并把旧 unique 恢复为 0047 形态；不恢复 carrier FK，也不退回 v1 数据）
- `git revert` / 换回旧提交
- 仅更换产品版本号或镜像 tag

步骤：

1. 停止 v2 Bot，确认没有其他实例连库。
2. 保存故障现场。
3. 用 plan 记录的三件套覆盖生产 db/wal/shm，并核对 `db_sha256` 与 size。
4. 只有快照仍是 v1 时，才能用 v1 二进制启动。v2 二进制会拒绝 v1 状态。
5. 恢复快照之后，cutover 之后产生的消息与配置不会存在。
