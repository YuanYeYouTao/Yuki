# Yuki-QQbot 项目长期记忆

## 生产环境访问（运维任务起点）

- 主机别名：`yuki-server`（root），完整地址与密钥见本机 `~/.ssh/config`，
  **不在此文件记录原始 IP**，也不写入任何公开位置。
- 部署目录 `/opt/yuki-qqbot`；主容器 `qq-ai-bot-bot-1`；网关是 SnowLuma（非 NapCat）。
- 数据库在容器内 `/app/data/qq_ai_bot.db`（约 168 MB），SQLite。
- 只读查询套路：
  `docker exec qq-ai-bot-bot-1 python -c "import sqlite3; c=sqlite3.connect('file:/app/data/qq_ai_bot.db?mode=ro', uri=True); ..."`
  必须用 `mode=ro` + `uri=True`，避免占写锁。

## 三个必踩的坑

1. **容器与 DB 时间戳都是 UTC**；而业务上的调度小时（如 self-reflection 的 4/12/20）
   是北京时间。换算时务必区分，否则会把"刚跑完"误判成"卡了 8 小时"。
2. **PowerShell 收取 ssh 中文输出前必须设**
   `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`，
   否则 UTF-8 会被按 GBK 解码成一堆"骞?鏈?"乱码。
3. 传给 ssh 的远程脚本用单引号 here-string `@'...'@`，否则 `$f` 被本地展开成空。

## 部署形态（已核实，勿再当作配置漂移）

`docker-compose.<slug>.yml` 这一类 overlay 文件本身从未入库（远端 commit 数为 0），
但**每个 overlay 都对应一个已合并 PR**：文件只钉一行
`image: ghcr.io/yuanyeyoutao/yuki-qqbot:<slug>-<shortsha>`，
shortsha 全是仓库内真实提交，且 commit message 与 overlay 名一一对应。
overlay 里的 env/volumes 大多在仓库 `docker-compose.yml` 里本来就有。
唯一缺口：overlay 的有序合并清单本身只在服务器上；`tmp/deploy_*.py` 4 个生成脚本
被 `.git/info/exclude`（本地排除，非 .gitignore）挡在库外。

## self-reflection 运维口径

- 定时调度 4/12/20（北京时间），健康度权威读数是 `memory_self_reflection_cycles.report_json`，
  不要自己拼 SQL 推断。
- 见到 `isolated` 批次：**先判断推进是否受阻**（前沿是否越过空洞、后续周期是否 completed、
  失败类别是否只有零星几次）。推进正常就不算故障。
- 再进一步：看被拒提案本身**该不该进记忆**。若本来就该拒，隔离是系统的正确行为，
  直接结案——既不建议 retry（只会把错误提案再跑一遍），也不改代码。
  实例：run 484（能力事实标 global，违反"全局只允许抽象自我记忆"）、
  run 496（"群聊不再用 markdown"）均被用户裁定为"本就不该入记忆"。

## 配置 / 提示词热更新口径（config 目录，实测）

- `/app/config` 是 **bind mount 且 `rw=false`**（容器内只读，`mount` 显示 `ro,relatime`）。
  → 只能改**宿主机** `/opt/yuki-qqbot/config/<file>`；`docker cp` 进容器必然失败。
  → 宿主机改完容器**实时可见**（这一步不需要任何操作）。
- **但文件内容可见 ≠ 生效。** `system_prompt` 只在进程启动时加载：`main.py:45`
  构造一次 `Settings()`，`config.py:807` 的 model_validator 把文件读进内存，
  之后 `prompt_composer.py:81` 只引用实例属性。**改完必须
  `docker restart qq-ai-bot-bot-1`** 才生效。全库无 watchdog / reload 入口——
  可 hot-reload 的只有语音声线、model profile、MCP config，**不含 system prompt**。
- 动手前先 `cp -p <file> <file>.bak.<YYYYMMDD>`。
- **重启只针对 bot 容器；用户明确要求不动 SnowLuma**（网关是独立容器
  `qq-ai-bot-snowluma-1`，动它会断连）。
- 验证生效要验代码路径，不要只看文件：容器内
  `from qq_ai_bot.config import Settings; s=Settings()`，断言新文案在
  `s.system_prompt` 里、旧文案不在。
- env 里的 `SYSTEM_PROMPT` 是**死值**：`config.py:830-839` 在 `SYSTEM_PROMPT_FILE`
  非空时会被文件覆盖。想用 env 就得先去掉 `SYSTEM_PROMPT_FILE`。
- 重启关闭阶段常见 `database is locked`（embedding worker 写
  `memory_embeddings`）导致 `Application shutdown failed` ExceptionGroup——
  既有锁竞争问题，新进程启动后不影响服务，不要当本次故障报。

## system_prompt 对话示例的写作口径

- 具体示例位于 `config/system_prompt.md`「具体示例（仅供参考）」（gitignored，不入库）。
- 格式：`场景：xxx` 起头，逐行 `Yuki:` / `用户:`，场景间不空行。
- **角色不按人固定映射**：哪一侧的语气是该场景下想让模型输出的，那侧就写 Yuki，逐段判断。
  实例：日常撒娇段 Yuki = 小尤；工作模式段 Yuki = 亦难（给建议的那个人）。
- 改完只改本地不够：需同步到宿主机 `/opt/yuki-qqbot/config/system_prompt.md` 再重启 bot 容器。
- **本地版与生产旧版长期存在内容差异，且以本地版为准**（2026-09-20 用户裁定）。
  同步时 diff 会显示本地删掉了旧版若干段落，属预期，不要按 diff 回滚。
- 重启后 uvicorn 约 **45 秒**才监听 8080，期间 healthcheck 连 `/healthz` 被拒、
  `docker ps` 显示 unhealthy，属启动慢不是故障；约 1 分钟后转 healthy。
  宿主机 `curl 127.0.0.1:8080` 不通是正常的（端口未映射到 host）。

## 用户偏好

- 要求结论必须查证后再下，不接受"看着像缺口"就写进报告——被纠正过一次。
- 明确说"不用给修复方向"时，就只给诊断，不要附带整改建议。
- 对运维异常，判断标准是"主流程能否正常推进"，不是"有没有红灯"；
  已被判定为正确行为的告警不要反复当故障上报。
- 公开仓库（GitHub issue / 评论）里不得出现真实主机地址与别名、群号、QQ 号、
  `canonical_person_id`、聊天正文。可保留内部自增 id、行数比例、PRAGMA 值、代码行号。
- 写 GitHub 正文时不要用 `~` 表示范围（GFM 会渲染成删除线吞掉字符），用"至"或 en dash。
