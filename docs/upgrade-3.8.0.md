# 升级到 Yuki 3.8.0

Yuki 3.8.0 是 canonical-only 版本，Alembic head 为 `0049`。它不包含 v1、dual-write、
identity backfill 或 cutover 运行路径。

## 支持范围

3.8 只支持两种数据库来源：

1. **全新数据库**：无父 revision 的 `0048` canonical baseline 创建最终结构，再升级到 `0049`。
2. **历史 0048 数据库**：必须已经完成 canonical v2，并通过 `0049` bridge 的身份、所有权、
   processing lease 和 schema manifest 检查。

以下来源不受支持：

- Alembic 0047 或更早。
- 尚未完成 canonical v2 的 0048。
- 存在 open identity conflict、processing lease 或 canonical ownership 缺口的 0048。
- v1、dual-write、backfill/cutover 中间态或手工修改过 schema 的数据库。

3.8 不会猜测身份、制造旧 carrier 行或自动修复这些数据。来源不满足条件时应保留旧部署，
或建立全新的 3.8 数据库。

## 1. 记录当前部署

升级前记录：

- 当前 Yuki 版本与 Git revision。
- 当前镜像 tag 和 digest。
- `alembic_version`；历史桥接必须是 `0048`。
- 正在使用的 Compose profiles 与 Provider。
- Bot、NapCat、SnowLuma、Speech Worker 的容器状态。

不要先覆盖部署文件，也不要在 Bot 仍写数据库时复制 SQLite 文件。

## 2. 停止所有写入

```bash
docker compose --profile napcat --profile snowluma --profile speech \
  stop bot napcat snowluma genie-tts-worker
docker compose --profile napcat --profile snowluma --profile speech ps --all
```

确认 Bot 已停止、Provider 连接已注销，并且没有其他容器或本地进程持有数据库。历史 0048 bridge
会拒绝仍有 processing lease 的数据库；不要通过直接改表清除 lease。

## 3. 制作同一时点快照

将下列三个 SQLite 文件作为不可分割的一组复制到部署目录之外：

- `data/qq_ai_bot.db`
- `data/qq_ai_bot.db-wal`
- `data/qq_ai_bot.db-shm`

同时保存：

- `.env`、`config/`、`.mcp.json` 和 Compose 文件。
- 当前镜像 digest。
- Provider 登录数据目录的独立备份。

WAL 或 SHM 当前不存在时，在备份清单中明确记录“缺失”；不要拿其他时点的 companion 文件补齐。
为所有快照文件记录 size 和 SHA-256。安装器会在升级已有部署时先做同类快照与 SQLite 完整性检查，
但生产升级仍应保留一份独立、只读副本。

## 4. 更新 3.8 部署文件

使用 v3.8.0 Release 中的安装器。安装器默认版本已是 `3.8.0`，会保留 `data/`、`config/`、
`plugins/` 与 Gateway 登录数据：

```bash
./install.sh --version 3.8.0
```

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Version 3.8.0
```

如果从源码验证，必须从将要发布的同一 commit 构建镜像，并记录 image digest。不要把开发工作树
直接挂载进生产容器。

## 5. 运行 0049 bridge

在不启动长期运行 Bot 的情况下执行数据库初始化/升级：

```bash
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
```

`0049` 在应用更改前验证：

- 来源 revision 和完整 canonical v2 状态。
- 无未解决 identity conflict。
- 无 Memory、Rollup、Automation、Plugin 等 processing lease。
- Person、Space、Presence、Binding、Conversation 和扩展表的 canonical ownership 完整。
- schema manifest、关键约束和预期触发器一致。

桥接在单个 `BEGIN IMMEDIATE` 事务中转换；任何检查或 failpoint 失败都必须回滚。不要重试前
手工修改身份、所有权或 Alembic revision。

全新安装同样运行该命令：`0048` baseline 创建 canonical schema，随后 `0049` 完成最终形状，
不会创建旧 people、groups、conversation_scopes 或 cutover 表。

## 6. 验证并启动

迁移成功后至少确认：

- `alembic_version=0049`。
- `PRAGMA foreign_key_check` 无结果，`PRAGMA quick_check` 返回 `ok`。
- FTS 表和同步 trigger 存在。
- 关键 Person、Space、Conversation、Memory、Relationship、Plugin 与 Automation 行数符合
  升级前摘要。
- 没有旧 identity runtime/backfill/conflict/cutover 表。
- 没有新增 Memory job 去重放历史事件。

然后启动目标 Provider 与 Bot：

```bash
docker compose up -d
docker compose ps
docker compose logs --tail 200 bot
```

检查 `/healthz` 的公开状态、版本和数据库健康。再验证私聊、一个已启用群、Memory 召回、
主动路由和插件/自动化状态。

## Provider 切换

NapCat 与 SnowLuma 是同级正式 Provider。不同 QQ 可以同时在线；同一 QQ 不能有两条活动连接。

切换同一 QQ 时：

1. 停止并移除旧 Provider。
2. 确认旧容器停止且 Registry 中连接已注销。
3. 启动目标 Provider并完成登录。
4. 确认新连接沿用原 Presence。

安装器的 `gateway-action.json` 会按此顺序执行；停止失败时不会启动新 Provider。切换只改变
GatewayConnection 与 ConnectionGeneration，不应改变 Presence、ConversationGeneration、
RouteGeneration 或 Memory。

SnowLuma noVNC 和 WebUI 默认绑定 `127.0.0.1`。远程访问配置见
[SnowLuma Provider 部署与切换](deployment/snowluma.md)。

## 回退

`0049` 不提供 downgrade。不能通过 git revert、改镜像 tag 或手工 stamp Alembic 回退数据。

1. 停止 Bot、全部 Provider 和 Worker。
2. 保存故障现场。
3. 恢复升级前同一时点的 DB/WAL/SHM 三件套及匹配配置。
4. 恢复快照记录的镜像 digest。
5. 验证 checksum 后再启动旧部署。

升级后产生的消息、配置和任务不会出现在恢复后的快照中。Yuki 不宣称升级或更换 Provider 能
降低腾讯账号风控风险。
