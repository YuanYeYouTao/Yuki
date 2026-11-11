# Yuki 4.0.0 升级指南

<!-- release-baseline: version=4.0.0 schema=0156 -->

[English](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/upgrade-4.0.0.en.md) · [4.0.0 发布说明](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/releases/v4.0.0.md) · [全部 PR 目录](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/releases/v4.0.0-prs.md)

本指南适用于两类部署：正式 **3.9.0**，以及 3.9.0 之后运行过 4.0 开发镜像的部署。新安装不需要看这里，直接用 [README](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/README.md#安装) 里的安装器。

按顺序做。标着“仅 3.9.0”的步骤，4.0 开发部署通常已经做过，核对后跳过即可。

## 变化一览

| 项目 | 3.9.0 | 4.0.0 |
| --- | --- | --- |
| 镜像 | `ghcr.io/yuanyeyoutao/yuki-qqbot` | `ghcr.io/yuanyeyoutao/yuki`，另有沙箱环境镜像 `ghcr.io/yuanyeyoutao/yuki-environment` |
| Python 包 / 命令 | `qq_ai_bot` / `qq-ai-bot-cli`、`qq-ai-bot` | `yuki` / `yuki`、`yuki-bot`，不保留旧名 |
| Compose 项目 / 网络 | `qq-ai-bot` / `qq-ai-bot-network` | `yuki` / `yuki-network` |
| 数据库文件 | `data/qq_ai_bot.db` | `data/yuki.db`（Compose 文件里写定） |
| 数据库 revision | `0119` | `0156`，单条前向迁移链 |
| 沙箱 Manager | 宿主机 systemd 服务、loop 挂载的 ext4、Squid | Compose 里的 `manager` 服务，数据在 Docker 命名卷 |
| Plugin API | `3.5` | `9.0`，精确匹配 |
| 管理员 | `.env` 的 `SUPERUSERS`（QQ 号） | 人物（Person）上的超级管理员资格 |
| 人格 | 系统提示词、`persona.md` 等多份文件 | 一份 `character/SOUL.md`，角色名在数据库里 |
| 管理界面 | 管理 WebUI | Yuki Desktop、CLI、`/ai admin` |
| 个人客户端 API | — | revision 10 |
| 本机连接器 | — | 0.6.2，设备协议 4 |

## 1. 确认现状

在旧部署目录里记下这些事实，后面每一步都要用：

```sh
cd /path/to/yuki
docker compose ls                                   # 实际 Compose 项目名
docker compose exec bot printenv DATABASE_URL       # Bot 实际使用的数据库
docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}/{{index .Config.Labels "io.yuki.sandbox.project"}}' yuki-environment
systemctl status yuki-sandbox --no-pager            # 宿主 Manager 是否在运行
```

最后两条输出 `persistent-v1/` 且 `yuki-sandbox` 在运行，说明沙箱由宿主 Manager 管理，需要做第 7 步。没有 `yuki-environment` 容器的部署跳过第 7 步。

另外核对：用了哪些插件和它们声明的 Plugin API；人格来自哪些文件（`SYSTEM_PROMPT`、`SYSTEM_PROMPT_FILE`、`BOT_PERSONA_FILE`、`YUKI_PERSONA_FILE`、`BOT_DISPLAY_NAME`）；`.env` 和 Compose 文件里有没有自己加的改动；4.0 开发部署还要看部署目录里是否有 `compose.yaml` 或其他覆盖文件。

## 2. 提前准备插件和人格

这两项可以在停机前慢慢做。

**插件。** 4.0 只加载声明 `plugin_api = "9.0"` 的插件。产品版本 4.0 和 Plugin API 9.0 是两回事；先改真实调用，再改 manifest 里的数字。从 3.5 起要依次处理：

| API 版本 | 需要改的地方 |
| --- | --- |
| [4.0 身份](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/plugin-development/api-4.0-migration.md) | 人物、记忆、消息、配置等目标改用内部 Person、Space、Conversation，不再传 QQ 号 |
| [5.0 表情](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/plugin-development/api-5.0-migration.md) | 删除 `ctx.emoji` 及相关权限、Feature、Hook；媒体走现行 artifact 和消息接口 |
| [6.0 提及](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/plugin-development/api-6.0-migration.md) | 使用 `CurrentMessage.mentions` 和 `message.current.mentions.v2` |
| [7.0 独立会话](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/plugin-development/api-7.0-migration.md) | 使用 `plugin.agent_session.v2`，删除已退出的创建参数和返回字段 |
| [8.0 自动化](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/plugin-development/api-8.0-migration.md) | 按当前 TaskSpec 和原 request 身份定义自动化 |
| [9.0 消息身份](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/plugin-development/api-9.0-migration.md) | `native_message_id` 可为空；任务、回复和历史沿 `event_id` |

`ctx.agent.run()` 显式给出的 `max_model_requests` / `max_tool_calls` 现在是整项任务的总额度，不再是每段上限。养鲲和 GitHub 监控插件不再随主仓发布，改用 [Yuki-Kun-Game](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game) 和 [Yuki-GitHub-Monitor](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor) 的当前版本。插件原有的 KV、任务、outbox 和未完成回执会保留。

**人格（仅 3.9.0）。** 4.0 只读一份 `character/SOUL.md`，角色名在数据库的角色资料里。比较旧的系统提示词和 `persona.md`，挑出真正想要的人格内容，整理成一份 SOUL；不要把两份文件直接拼起来。工具、权限和任务规则由程序提供，不用写进 SOUL。SOUL 可以为空。细节见[单 SOUL 升级说明](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/operations/soul-deployment-20261022.md)。

## 3. 让任务结束，停止旧服务

先让 Yuki 手上的任务做完，或明确取消。沙箱迁移脚本发现还有排队或运行中的执行时会拒绝继续（登记的常驻服务不算）。

然后在旧部署目录用**旧的** Compose 文件停止服务：

```sh
docker compose down
```

这会停止 Bot 和 SnowLuma。沙箱容器 `yuki-environment` 不属于这个 Compose 项目，会继续运行，留给第 7 步处理。同一个数据库任何时候只能有一个 Bot 在写。

## 4. 导出旧管理员名单（仅 3.9.0）

4.0 的管理员资格属于人物，数据库迁移不会根据 `SUPERUSERS` 自动授予任何人。迁移会删除旧的账号绑定表，所以**必须在数据库升级之前**用 4.0 镜像里的只读脚本导出候选名单：

```sh
docker pull ghcr.io/yuanyeyoutao/yuki:4.0.0
docker run --rm -v "$PWD:/deploy" --entrypoint python ghcr.io/yuanyeyoutao/yuki:4.0.0 \
  /app/scripts/prepare_person_authority.py --deployment-root /deploy \
  --database /deploy/data/qq_ai_bot.db --output /deploy/person-authority-candidates.json
```

脚本只读数据库，不授予资格。名单里含身份资料，按部署数据保管。导出后，从 `.env`、Compose 文件和进程环境里删除 `SUPERUSERS`。

## 5. 备份

服务停着的时候做一份完整备份：

- 部署目录：`data/`（数据库连同 `-wal`、`-shm` 文件）、`.env`、`config/`、`webui-config/`（含 `model_profiles.secrets.json`）、`character/`、`plugins/`、`workspace/`、`social-transfer/`，以及 QQ 登录数据 `snowluma-data/`、`snowluma-qq-config/`、`snowluma-qq-data/`、`snowluma-extra-accounts/`。
- 宿主 Manager 的数据：`/etc/yuki-sandbox/`，以及沙箱根目录（`/etc/yuki-sandbox/deployment.env` 里的 `YUKI_SANDBOX_ROOT`，默认 `/var/lib/yuki-sandbox`）。

数据库迁移只能向前，部分迁移不可逆，旧镜像不能运行升级后的数据库。出问题时优先向前修复；确实要回到备份，先把升级后产生的新消息、文件和已经发生的效果另行保存，不能直接用旧库覆盖。

## 6. 放入 4.0.0 发布文件

沙箱迁移脚本和新的 Compose 文件都在发布包里，所以先把它们放进部署目录。这里不能直接运行安装器：它发现宿主 Manager 的旧环境时会停下，要求先做第 7 步。

```sh
cd /path/to/yuki
curl -fsSLO https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.0/yuki-4.0.0-deploy.tar.gz
curl -fsSLO https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.0/SHA256SUMS
sha256sum --ignore-missing -c SHA256SUMS
tar -xzf yuki-4.0.0-deploy.tar.gz --strip-components=1
```

这会替换 `docker-compose.yml`、`.env.example`、`install.sh`、`deploy/sandbox/` 等发布文件，不动 `.env` 和数据目录。之后只用 `docker-compose.yml`，自己的改动（端口、额外挂载、环境变量）写进 `docker-compose.override.yml`。4.0 开发部署如果有 `compose.yaml`，Compose 会优先读它，请把其中需要保留的改动移到 `docker-compose.override.yml`，再移走 `compose.yaml`。

**数据库文件（仅 3.9.0）。** 新的 Compose 文件在 `environment` 里写定了 `DATABASE_URL=sqlite+aiosqlite:////app/data/yuki.db`，它的优先级高于 `.env`，所以在 `.env` 里改 `DATABASE_URL` 不起作用。二选一：

- 改名（确认 `data/yuki.db` 还不存在）：

  ```sh
  mv data/qq_ai_bot.db data/yuki.db
  for suffix in -wal -shm; do
    [ -f "data/qq_ai_bot.db$suffix" ] && mv "data/qq_ai_bot.db$suffix" "data/yuki.db$suffix"
  done
  ```

- 或保留原名，在 `docker-compose.override.yml` 里指定：

  ```yaml
  services:
    bot:
      environment:
        DATABASE_URL: sqlite+aiosqlite:////app/data/qq_ai_bot.db
  ```

无论哪种，都不要让 Bot 在 `data/yuki.db` 新建一个空库。4.0 开发部署按第 1 步查到的实际路径核对。

**`.env` 和人格（仅 3.9.0）。**

- 把第 2 步整理好的人格保存为 `character/SOUL.md`；不想写人格也可以从发布包里的 `config/SOUL.md` 模板复制一份。删除 `SYSTEM_PROMPT`、`SYSTEM_PROMPT_FILE`、`BOT_PERSONA_FILE`、`YUKI_PERSONA_FILE`、`BOT_DISPLAY_NAME`；`BOT_ALIASES` 只保留还想用的唤醒别名。
- 删除已经退出的设置。多余的变量会被忽略，不会导致启动失败，但留着容易误会：

| 设置 | 处理 |
| --- | --- |
| `SUPERUSERS` | 删除（第 4 步已导出） |
| `AGENT_MAX_MODEL_REQUESTS`、`AGENT_MAX_TOOL_CALLS` | 删除；每段上限已取消 |
| `CONTEXT_WINDOW_TOKENS`、`CONTEXT_WORK_WINDOW_TOKENS` | 删除。提前整理水位是 `CONTEXT_COMPACT_TRIGGER_TOKENS`（默认 96000），硬容量来自 Profile 的 `max_input_tokens` / `context_window_tokens` |
| `CONVERSATION_ROLLUP_*`、`CONTEXT_COMPACTION_WINDOW_TOKENS`、`WORK_COMPACTION_*` | 删除；Memory evidence compaction 的设置保留 |
| `WEB_EXTRACT_MAX_RESULTS`、`WEB_SOURCE_MAX_RUNS_PER_CONVERSATION` | 删除；网页正文改为显式读取 |
| `VISION_MAX_RETRIES`、`emoji_replacement` 路由 | 删除 |
| `SNOWLUMA_NOVNC_*` | 删除；QQ 登录画面改在 Yuki Desktop 里查看 |
| 代理 | 使用标准 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`，见[代理说明](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/operations/http-proxy.zh-CN.md) |
| 语音转写 | 明确选择 `chat_completions` 或 `audio_transcriptions` 协议 |

## 7. 把沙箱迁进 Compose

3.9.0 和 4.0 开发部署的沙箱 Manager 跑在宿主机上（systemd、loop 挂载的 ext4）。4.0.0 把它放进 Compose，迁移只做一次。在 Docker 宿主机上以 root 运行：

```sh
sudo sh deploy/sandbox/migrate-host-manager.sh --deployment-root /path/to/yuki
```

脚本做这些事，第一次运行只复制、不删除：

1. 确认没有排队或运行中的执行，停止宿主的 `yuki-sandbox` 服务，并让它和 `yuki-environment-storage` 不再开机启动（存储挂载保留到 `--cleanup`）。
2. 停止 `yuki-environment`，把它的完整 rootFS `docker commit` 为 `yuki-environment:migrated-<时间>`。这个镜像就是新环境的起点，你装过的软件都在里面。
3. 创建 Docker 卷，把 HOME、运行记录、媒体缓存和 `jobs.sqlite3` 复制进去，并让新 Manager 从上面的检查点镜像创建环境。原环境若挂了宿主的 `resolv.conf`（gVisor 用），复制为 HOME 里的 `.config/resolv.conf` 并写入 `YUKI_SANDBOX_RESOLV_CONF`。
4. 把旧容器改名为 `yuki-environment-retired`，删除旧的 Squid 出口容器、内网 bridge 和相关防火墙规则。
5. 把旧设置写进部署的 `.env`：原来的浏览器模式换算成档位（有头 → `desktop`，无头 → `headless`，否则 `lite`），原运行时写入 `YUKI_SANDBOX_RUNTIME`，内存、CPU 等资源和额外软件包写成 `YUKI_SANDBOX_*`。`.env` 里已有的值不覆盖。

原来的 loop 镜像、`jobs.sqlite3` 和媒体缓存目录原地保留，直到你运行 `--cleanup`。

注意：

- 宿主机需要 `python3`（宿主 Manager 部署原本就有）。
- 用 gVisor 但 Docker 里的 `runsc` 没有带 `--overlay2=none` 时，环境里安装的软件不在 Docker 层中，脚本会拒绝并提示先用 `runsc tar rootfs-upper` 导出，见 [Mac 迁移记录](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/operations/mac-arm64-migration-20261107.md)。
- 检查点镜像里有旧的 execd 凭据，只作私有保管，不要推送到公开仓库，也不要删除——环境重建时要用它。

没有宿主 Manager 的部署跳过这一步。4.0 的媒体缓存在 `yuki-media-cache` 卷；脚本会把旧的 `data/conversation-media-cache` 复制过去，确认后可删除旧目录。

## 8. 启动 4.0.0

在部署目录重新运行安装器：

```sh
sh install.sh
```

`.env` 已存在，所以不会再走配置向导。它会把 `YUKI_VERSION` 改为 `4.0.0`，没有档位时询问档位，确定运行时，创建缺少的卷，拉取镜像，`docker compose up -d`，并一直等到 Bot 能连上沙箱。Bot 启动时自动把数据库迁移到 `0156`。不使用安装器也可以：把 `.env` 的 `YUKI_VERSION` 改为 `4.0.0`，确认有 `YUKI_SANDBOX_PROFILE` 和 `YUKI_SANDBOX_RUNTIME`，运行 `sh deploy/sandbox/prepare.sh` 后 `docker compose up -d`。

关于数据库迁移：

- 从 `0119` 到 `0156` 是一条链，`0124` 直接接 `0126`、`0145` 直接接 `0147`，不需要补缺号。已经运行过 4.0 开发版的库从自己的实际 revision 继续。
- 不要用 `alembic stamp` 跳过迁移，也不要为了升级重置会话或任务预算。
- `0153` 把原有向量原样移进同库的 Vec1 表，不重新调用 Embedding。
- 升级保留原人物、会话、任务、累计预算和发送回执。

## 9. 迁移旧的模型配置

如果 3.9.0 在 `.env`（`LLM_*` 等）或管理 WebUI 里配置过模型，用一次性离线命令把它们迁进 `webui-config/model_profiles.toml`。它不调用上游、不产生费用，但 Bot 运行时不能执行：

```sh
docker compose stop bot
docker compose run --rm --entrypoint yuki bot admin model migrate-legacy
docker compose start bot
```

检查退出码和输出里的 `ok`、`revision`、`consumed_env_fields`。成功后，从 `.env` 删除 `consumed_env_fields` 列出的旧变量；命令只报告变量名，不改你的文件。结果为 `partial` 或无法确认时，先核对已保存的 revision，不要直接重跑。细节见[模型配置迁移](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/upgrade-model-config-20261031.md)。

## 10. 管理员、角色名和插件

**operator 凭据。** 管理命令需要 operator 凭据。没有 operator 配置的旧部署，可以在部署目录重新运行配置向导补上，它会备份原配置：

```sh
docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/deploy" -w /deploy --entrypoint yuki \
  ghcr.io/yuanyeyoutao/yuki:4.0.0 setup --deployment-root /deploy
```

**授予超级管理员。** 按第 4 步的名单逐个核对真实的人，再授予：

```sh
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person list'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person show <PersonID>'
docker compose exec bot sh -c 'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin person superuser grant <PersonID> --revision <当前revision>'
```

没有账号的人可以先 `admin person create --display-name "名称"`。账号转交不带走资格，解绑不撤销资格，普通配对不授予资格。之后用 `admin client pair --person <PersonID>` 配对 Yuki Desktop；超级管理员可以用自己的 Desktop 凭据管理。完整说明见[人物授权](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/operations/person-admin-control-plane-upgrade.md)。旧的角色 owner 记录保留但不再授权。

**角色名（仅 3.9.0）。** 数据库里还没有角色资料时初始化一次；已有角色不会被改名：

```sh
docker compose run --rm --entrypoint yuki bot init-db --character-name "Yuki"
```

**插件。** 放入第 2 步改好的插件后，用 `yuki plugin list` 等命令（同样带上 operator 凭据）重新发现插件，审阅实际 manifest，按需要的权限重新批准。不要为了绕过接口变化扩大授权，也不要重发旧通知或已付费的模型请求。

**管理入口。** 管理 WebUI 已退出，改用 Yuki Desktop、`yuki admin` CLI 和聊天里的 `/ai admin <领域> <操作>`。`/ai help` 和 `/ai admin help` 分别列出普通和管理操作。

## 11. 客户端、QQ 和连接器

- **Yuki Desktop** 使用与 4.0.0 配套的版本（个人客户端 API revision 10）。远程连接需要给服务一个 HTTPS 地址，见[桌面部署与远程入口](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/operations/desktop-local-deployment.zh-CN.md)或 [Tailscale 指南](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/operations/tailscale-connection.zh-CN.md)。
- **QQ 登录画面** 不再发布在 6081 端口，改在已配对管理员的 Yuki Desktop 电脑窗口里选择「QQ 登入」查看和扫码；SnowLuma 管理页仍在 `127.0.0.1:5099`。QQ 登录数据目录不变。见 [SnowLuma](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/deployment/snowluma.md)。
- **本机连接器** 由 Desktop 管理时随 Desktop 更新；单独安装的按[本机连接器](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/operations/local-agent-connector.zh-CN.md)说明更新到 0.6.2，原设备、配对和 journal 保留。设备协议仍是 4，旧版本连接器可以继续执行命令和读写文件，但不支持屏幕和浏览器操作。

## 12. 核对

```sh
docker compose ps
docker compose exec bot yuki setup verify
docker compose exec -T --user 10001:10001 bot yuki setup environment-check
```

然后分别确认：

- 数据库在 `0156`，原人物、账号归属、会话、任务和发送记录都在。
- 沙箱：终端里能看到原来的文件和装过的软件，登记的服务按原设置运行，环境能访问公网。
- 超级管理员和普通用户的权限符合预期，Desktop 和已配对设备能连上。
- 实际启用的聊天平台收发正常，插件按新的批准运行。

健康检查只说明对应服务在运行，不代表模型调用、聊天平台和插件都已经可用。

## 13. 清理

确认一切正常后，删除宿主 Manager 留下的东西：

```sh
sudo sh deploy/sandbox/migrate-host-manager.sh --deployment-root /path/to/yuki --cleanup
```

它删除 `yuki-environment-retired` 容器、宿主的 systemd 单元、loop 卷和沙箱根目录（`/var/lib/yuki-sandbox` 或你配置的路径）、`/opt/yuki-sandbox`、`/etc/yuki-sandbox`。新 Manager 还没创建环境时它会拒绝执行。旧媒体缓存目录（默认 `data/conversation-media-cache`）需要你确认 `yuki-media-cache` 卷正常后自己删除。

其他可以清理的：3.9.0 的网络 `qq-ai-bot-network`、旧镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot`、发布包 `yuki-4.0.0-deploy.tar.gz`。不要删除 `yuki-environment:migrated-<时间>` 检查点镜像，也不要删除 `yuki-sandbox-*` 和 `yuki-media-cache` 卷；`docker compose down -v` 不会删除它们。

## 参考

- [持久工作环境](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/operations/persistent-environment.zh-CN.md)：档位、资源、网络、迁移细节
- [4.0.0 发布说明](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/releases/v4.0.0.md)
- [架构索引](https://github.com/YuanYeYouTao/Yuki/blob/v4.0.0/docs/architecture/README.md)
