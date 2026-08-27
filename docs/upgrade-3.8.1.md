# 升级到 Yuki 3.8.1

> 本文只对应已发布的 3.8.1/`0049`，保留为历史部署事实。当前源码的后续未发布修复将 schema
> 推进到 `0050`，并把插件主动点评收口到完整稳定上下文的正常 Main Agent；不要把本文的
> `0049` 步骤当作当前源码的完整升级矩阵。

Yuki 3.8.1 没有数据库迁移；Alembic head 仍为 `0049`。本次升级改变外部事件的 Prompt/Rollup
计量，因此不能只换镜像后直接恢复写入，必须在停写状态执行一次 offline recount。

## 支持范围

- 推荐来源：已经正常运行的 Yuki 3.8.0 canonical 数据库，`alembic_version=0049`。
- fresh install：仍执行无父 revision 的 `0048` baseline，再升级到 `0049`。
- historical 0048：仍必须满足 3.8 canonical bridge 的全部前提。
- pre-3.8、v1、dual-write、backfill/cutover 中间态不受支持。

3.8.1 不修改 Person、Space、Presence、Conversation、Memory、Relationship、Plugin 或 Automation
所有权，也不会重建 Yuki 主体。

## 1. 记录并停止

记录当前 Git revision、3.8.0 镜像 tag/digest、Compose profiles、Provider 连接和 GitHub Monitor
`accepted/committed/pending/inflight/gap` 状态。然后停止 Bot 与全部写库 worker：

```bash
docker compose --profile napcat --profile snowluma --profile speech \
  stop bot napcat snowluma genie-tts-worker
docker compose --profile napcat --profile snowluma --profile speech ps --all
```

确认没有本地进程或第二个 Bot 实例持有 SQLite。Provider 必须停止到 Registry 连接注销；不要让
新旧 Bot 同时写同一数据库。

## 2. 制作同一时点快照

把以下文件作为一组复制到部署目录之外，并记录 size、SHA-256 与“缺失”项：

- `data/qq_ai_bot.db`
- `data/qq_ai_bot.db-wal`
- `data/qq_ai_bot.db-shm`
- `.env`、`config/`、`.mcp.json`、Compose 文件和镜像 digest
- Plugin KV、GitHub pending/inflight、`data/plugin_artifacts/` 与对应未过期媒体资产
- 当前 `plugins/github-monitor/` 代码与 `plugin.toml`
- Provider 登录数据目录的独立备份

WAL/SHM 不存在时不要从其他时点补齐。快照不得提交到 Git，也不得把 token、Cookie、QQ 号或
数据库内容写进升级日志。

## 3. 准备精确的 3.8.1 Bot 镜像

生产机不得构建或运行 `uv sync`。从最终、已验收 commit 在本机创建 linux/amd64 镜像并保存：

```bash
VERSION=3.8.1
REVISION="$(git rev-parse HEAD)"
IMAGE="ghcr.io/yuanyeyoutao/yuki-qqbot:$VERSION"

docker buildx build \
  --platform linux/amd64 \
  --load \
  --build-arg "YUKI_VERSION=$VERSION" \
  --build-arg "VCS_REF=$REVISION" \
  --label "org.opencontainers.image.version=$VERSION" \
  --label "org.opencontainers.image.revision=$REVISION" \
  --tag "$IMAGE" \
  .
docker save --output "yuki-qqbot-$VERSION-amd64.tar" "$IMAGE"
sha256sum "yuki-qqbot-$VERSION-amd64.tar" \
  > "yuki-qqbot-$VERSION-amd64.tar.sha256"
```

把 tar 与 checksum 传到 rehearsal/生产机，验证后加载：

```bash
sha256sum --check yuki-qqbot-3.8.1-amd64.tar.sha256
docker load --input yuki-qqbot-3.8.1-amd64.tar
docker image inspect ghcr.io/yuanyeyoutao/yuki-qqbot:3.8.1 \
  --format '{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

输出必须是 `amd64 3.8.1 <最终 commit SHA>`。不得给 3.8.0 tag 重新打标签或覆盖其镜像。

Bot 镜像不包含插件源码，Compose 使用宿主 `./plugins:/app/plugins:ro`。因此还必须从同一
3.8.1 Release 包受控替换 GitHub Monitor；只换 Bot 镜像会继续运行旧插件。保持 Bot 停止，
先校验 Release 包 SHA-256，然后分阶段替换：

```bash
tar -xzf yuki-3.8.1-deploy.tar.gz
test "$(sed -n 's/^version = "\([^"]*\)"/\1/p' \
  yuki-3.8.1-deploy/plugins/github-monitor/plugin.toml)" = "1.2.0"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p ".yuki/backups/pre-3.8.1-$stamp/plugins" plugins
cp -a plugins/github-monitor ".yuki/backups/pre-3.8.1-$stamp/plugins/"
cp -a yuki-3.8.1-deploy/plugins/github-monitor \
  "plugins/.github-monitor-3.8.1-staged-$stamp"
mv plugins/github-monitor ".yuki/backups/pre-3.8.1-$stamp/github-monitor-active"
mv "plugins/.github-monitor-3.8.1-staged-$stamp" plugins/github-monitor
```

不删除其他插件，也不覆盖用户私有目录。任一移动失败时保持 Bot 停止，从备份目录
恢复 GitHub Monitor，不要继续启动。

## 4. 在停写副本上 rehearsal

在隔离的部署副本中加载 3.8.1 Bot 镜像，但不要接入 QQ 或启动后台 worker。依次执行：

```bash
YUKI_VERSION=3.8.1 docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
YUKI_VERSION=3.8.1 docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot \
  conversation recount-uncovered
YUKI_VERSION=3.8.1 docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot \
  conversation recount-uncovered --check
YUKI_VERSION=3.8.1 docker compose run --rm --no-deps --entrypoint python bot \
  /app/plugins/github-monitor/doctor.py --apply-legacy-import
```

recount 只重算 canonical Conversation 的 `uncovered_event_count` 与
`uncovered_character_count`：事件计数覆盖所有未覆盖 keeper，字符只使用 message 投影。它不会
改写事件、摘要、Memory 或 generation。任一 schema、coverage、generation 或一致性检查失败时
整次事务回滚；不要手工改计数绕过。

随后以只读模式再验证：

```bash
YUKI_VERSION=3.8.1 docker compose run --rm --no-deps --entrypoint python bot \
  /app/plugins/github-monitor/doctor.py
YUKI_VERSION=3.8.1 docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot \
  plugin doctor github-monitor
docker compose config --quiet
```

还应检查 SQLite `quick_check`、`foreign_key_check`、Alembic `0049`、FTS/trigger 和关键业务表行数。

## 5. 对 live 停写数据库执行 recount

rehearsal 完全通过后，保持生产 Bot 与 Provider 停止，对 live 数据执行同一命令：

```bash
YUKI_VERSION=3.8.1 docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot \
  conversation recount-uncovered
```

保存无正文 JSON 报告。报告应为 `ok=true`；失败时停止升级并保留现场，不能启动 3.8.1 写入。
在 live 库上紧接着执行同样的 `recount-uncovered --check`和 GitHub Monitor 只读
doctor；两者都必须 `ok=true`。

## 6. 替换 Bot、恢复 Provider 并验证

把 `.env` 中 `YUKI_VERSION` 改为 `3.8.1`，然后禁止依赖重建，只替换 Bot：

```bash
docker compose up -d --no-deps --no-build --force-recreate bot
```

只用 `docker compose start` 恢复升级前确实运行的 Provider 和可选 Speech Worker，例如：

```bash
docker compose start snowluma
# 或：docker compose start napcat
# 若升级前启用了 Speech：docker compose start genie-tts-worker
```

不要同时启动登录同一 QQ 的 NapCat 与 SnowLuma，也不要重建它们的登录目录。

启动后至少验证：

- `/healthz` 返回版本 `3.8.1`、database `ok`，公开字段形状不变。
- 私聊和一个已启用群可正常回复，Conversation generation 未变化。
- Memory 召回、关系、自动化、Plugin state 与路由保持原值。
- 外部通知不会出现在普通聊天历史中；后台通知 turn 仍可独立发送。
- `/github status` 的 cursor 连续，无未知 gap；已有 pending 按 oldest-first 排空。
- 离线 queue doctor 的 `legacy_import_pending_count`、`activation_pending_delivery_count`、
  `legacy_queue_conflict_count`、`cursor_gap_count`、`cas_conflict_count`、`active_diagnostic_count`、
  `receipt_conflict_count`、`outbox_pending_count` 和 `turn_pending_count` 均为 0。
- GitHub safe batch 没有聚合卡片，Push/Release/PR/Issue/评论/Review 保持单例。

## 回退

在尚未产生新外部副作用时，可停止全部写入并恢复同一时点 DB/WAL/SHM、配置与匹配镜像。

一旦 3.8.1 已发送平台消息、提交 Agent turn 或推进 GitHub committed cursor：

1. 先停止所有 worker；
2. 对账 Host receipt、Outbox、pending/inflight、accepted/committed cursor；
3. 只重试结果未知且请求字节完全一致的工作；
4. 已确认成功的目标不得再次发送；
5. 禁止盲目恢复快照后自动重试。

3.8.0 会把 3.8.1 已落账的 external event 再投影为普通 system 历史，因此不是安全运行时回退
下限。紧急代码回退至少保留 3.8.1 的外部事件隔离与 Host 幂等修复。`0049` 仍不提供 downgrade。
