# 从 Yuki 3.7.1 升级到 3.8.0（预发布演练）

> 3.8.0 尚未发布。本页用于源码构建或生产等价副本演练；在 `v3.8.0` tag、GHCR 镜像与
> GitHub Release 出现前，不要把正式环境的 `YUKI_VERSION` 直接改成 `3.8.0`。

3.8.0 是停机数据切换，不是普通 `pull && up`。Alembic `0043`–`0048` 先扩展 canonical identity
shadow，随后 `identity-cutover` 把运行时从 v1 原子翻转为 v2。3.8.0 二进制遇到 v1 数据会拒绝
启动，因此必须在启动 Bot 前完成下列步骤。

本流程当前只认证“已有 3.7.1 Yuki 数据”的升级演练。全新数据库的首个 Yuki Presence
bootstrap 仍是正式发布阻断项。

## 1. 前置检查

1. 当前应用为 Yuki 3.7.1，Alembic head 为 `0042`，且只有一个主动 Bot 实例。
2. 现有账本中已经出现当前 Yuki QQ 的 `bot_user_id/self_id`，backfill 才能把它分类为 Presence。
3. 记录将要构建或部署的 3.8.0 Git commit SHA；plan 与 apply 必须使用完全相同的 SHA。
4. 排空 Memory、Reflection、Dream、Automation、Plugin outbox、rebuild 与 mutation 的 processing
   lease。
5. 确认磁盘能同时保存至少两套 SQLite DB/WAL/SHM 与 `.env`、`config/`、Compose 文件。

## 2. 停止并制作升级快照

停止 Bot 和全部 QQ Provider，确认容器已退出且数据库旁的 Application lock 不再被持有：

```bash
docker compose --profile napcat --profile snowluma stop bot napcat snowluma
docker compose --profile napcat --profile snowluma ps --all
```

复制同一时点的以下文件到工作树外的只读备份目录：

- `data/qq_ai_bot.db`
- `data/qq_ai_bot.db-wal`（若不存在，在备份目录创建零字节 companion）
- `data/qq_ai_bot.db-shm`（若不存在，在备份目录创建零字节 companion）
- `.env`、`config/`、`.mcp.json`、Compose 文件和当前镜像 digest

不要在 Bot 运行时分别复制这三个 SQLite 文件，也不要把备份放回 `data/` 后当成 live 文件使用。

## 3. 准备 3.8.0 CLI 并升级 schema

预发布阶段必须在生产等价副本中用目标 commit 自行构建等价 Bot 镜像，并让该副本的
`YUKI_VERSION=3.8.0` 指向本地镜像；正式 Release 发布后才可以在生产改版本并拉取 GHCR：

```bash
docker build --build-arg YUKI_VERSION=3.8.0 \
  --tag ghcr.io/yuanyeyoutao/yuki-qqbot:3.8.0 .
```

无论镜像来源如何，都先只运行 CLI，不启动 Bot：

```bash
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
```

验证 `alembic_version=0048` 与 `PRAGMA foreign_key_check` 为空。此时
`identity_runtime_state.state` 仍应为 `v1`。

## 4. canonical identity 回填

先 dry-run，再 apply；报告不会包含正文、完整外部 ID 或 secret：

```bash
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot \
  identity backfill --dry-run --format text

docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot \
  identity backfill --apply --format text
```

必须满足：

- `status=succeeded`
- 当前 Yuki QQ 被分类为 `yuki_presence`，不能成为 Person
- ignored bot 不成为 Person
- 所有历史 `chat_events` 都获得 `person|yuki|external_bot|system` 作者三元组
- `conflicts=0`
- 第二次 apply 为零业务 diff，且 `event_authors=0`

有冲突、未知 Yuki Presence 或路由歧义时停止，不要伪造 legacy people/groups/scope 行。

## 5. 制作 cutover evidence 快照

在 backfill 完成、Bot 仍停止时，再制作一套独立 DB/WAL/SHM 快照。plan/apply 的
`--snapshot-*` 必须指向这套快照，不能指向 live `data/` 文件。缺失的 WAL/SHM 在快照目录用
零字节 companion 表示。

为本次停机窗口生成一个随机 downtime token，并记录目标 commit SHA。token 只用于本次
plan/apply，不写进 Git 或日志。

## 6. plan 与 apply

```bash
docker compose run --rm --no-deps \
  --volume <宿主机snapshot目录>:/cutover:ro \
  --entrypoint qq-ai-bot-cli bot \
  identity-cutover --plan \
  --git-revision <目标commit-sha> \
  --expected-revision <同一commit-sha> \
  --downtime-token <本次随机token> \
  --snapshot-db /cutover/qq_ai_bot.db \
  --snapshot-wal /cutover/qq_ai_bot.db-wal \
  --snapshot-shm /cutover/qq_ai_bot.db-shm \
  --format text
```

只有 `status=succeeded` 才记录输出的 `source_fingerprint`，并在 live 数据、快照、revision 与 token
均未变化的情况下 apply：

```bash
docker compose run --rm --no-deps \
  --volume <同一宿主机snapshot目录>:/cutover:ro \
  --entrypoint qq-ai-bot-cli bot \
  identity-cutover --apply <source_fingerprint> \
  --git-revision <同一commit-sha> \
  --downtime-token <同一随机token> \
  --snapshot-db /cutover/qq_ai_bot.db \
  --snapshot-wal /cutover/qq_ai_bot.db-wal \
  --snapshot-shm /cutover/qq_ai_bot.db-shm \
  --format text
```

apply 在单个 `BEGIN IMMEDIATE` 中复核 fingerprint，最后才把 state 翻为 v2。任何失败都会回滚
事务；不要手工修改 `identity_runtime_state`。

## 7. 启动与验证

```bash
docker compose up -d
docker compose ps
docker compose logs --tail 200 bot
```

逐项确认：

1. `/healthz` 为 `status=ok`、`version=3.8.0`、`database=ok`，公开字段没有增加管理数据。
2. `identity_runtime_state.state=v2`，cutover ID、fingerprint 与完成时间非空。
3. 当前 QQ 连接复用原 Presence；Person、Space、Conversation、Memory、Relationship 和旧自动化
   没有按 Provider 或 QQ 网关分裂。
4. 路由切换不改 ConversationGeneration；Gateway 重连只增加 ConnectionGeneration。
5. Memory job 数量没有因迁移旧事件而增加。

NapCat/SnowLuma 的首次登录与安全切换见 [SnowLuma Provider 文档](deployment/snowluma.md)。

## 8. 回退

不要用 Alembic downgrade、git revert 或单独改镜像 tag 回退 v2 数据。

1. 停止 Bot 与全部 Provider，并保存故障现场。
2. 恢复 plan 使用的整套 DB/WAL/SHM 快照和对应配置。
3. 核对快照 hash 与 size，确认 runtime state 为 v1。
4. 启动与该快照匹配的旧部署。

恢复后，cutover 之后产生的消息、路由和配置变更都会丢失。更完整的前置条件与失败分类见
[永久主体 Identity Cutover](upgrade-identity-cutover.md)。
