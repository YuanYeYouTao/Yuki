# Yuki 3.8.2 配置、启动与升级

目标数据库版本为 **0055**。从 0051 升级会执行 0052–0055 迁移；禁止直接 stamp 跳过迁移。
更旧数据库先核对 [3.8.1 升级要求](https://github.com/YuanYeYouTao/Yuki-QQbot/blob/v3.8.2/docs/upgrade-3.8.1.md)。

## 配置向导的范围

install.sh / install.ps1 只在空目录下载、校验部署包，并运行配置向导。
已有部署不会被替换 Compose、插件或数据；确认配置后，向导备份原配置并原子写入新配置。
向导不负责停服、数据库迁移、服务启动或 QQ 网关切换。也可以手工填写配置文件。

模型生成声明需要支持思考且有效档位至少 low；embedding、TTS 不添加推理参数。
新安装需要选择并登录 NapCat 或 SnowLuma；填好网关配置不等于已经完成 QQ 登录。

## 首次启动

在已配置的部署目录运行：

```sh
docker compose config --quiet
docker compose pull
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
docker compose up -d
```

若向导选择了插件，在 Bot 健康后应用已确认的选择：

```sh
docker compose exec -T bot qq-ai-bot-cli setup apply-pending --deployment-root /app --no-color
docker compose up -d --no-deps --force-recreate bot
docker compose exec -T bot qq-ai-bot-cli setup verify --deployment-root /app --no-color
```

网关或 TTS 的配置变更需要按对应组件说明单独处理；向导留下的动作标记不会自行重建服务。
持久沙箱是可选的独立宿主组件，部署包不会安装 gVisor 或 Manager，见
[持久环境说明](https://github.com/YuanYeYouTao/Yuki-QQbot/blob/v3.8.2/docs/operations/persistent-environment.zh-CN.md)。

## 已有部署升级

每条 docker compose 命令都必须沿用原项目名及全部 -f 覆盖文件，尤其是挂载、代理和网关配置。
不要用新部署包覆盖原 Compose。先拉取 3.8.2 镜像并核对最终解析的 Bot 镜像确实是目标版本，
同时保留可用的旧镜像。自建镜像在本地构建，现有生产服务器只加载镜像。

仅暂停 Bot；使用持久沙箱时同时暂停 Manager。保存一致的 DB/WAL/SHM、全部配置、
artifact、工作区、Manager 回执及部署配置，检查副本完整性后再迁移。备份含凭据，应限制访问。
保持 QQ 网关与 Docker 运行，不能用整个 Compose down 代替单服务暂停。

在 Bot 停止写库期间，以目标镜像依次运行：

```sh
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot conversation recount-uncovered
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot conversation recount-uncovered --check
```

使用内置 GitHub Monitor 的部署还应在停库期间运行队列迁移检查：

```sh
docker compose run --rm --no-deps --entrypoint python bot /app/plugins/github-monitor/doctor.py --apply-legacy-import
```

任一检查失败时先处理失败并保留备份，不继续启动。全部通过后恢复 Manager，并只重建 Bot：

```sh
docker compose up -d --no-deps --no-build --force-recreate bot
```

从尚未采用持久环境的版本升级时，另外执行持久环境说明中的 Manager 安装、文件迁移与校验。
升级包不会自动更新既有插件目录；需要更新时单独备份插件代码、保留其配置与数据后替换。

## 验收与回退

启动后观察至少 15 分钟，检查健康、QQ 连接、队列年龄、批次结果和数据库完整性。
未自然触发的功能标记为未观察，不向真实群发送测试消息。普通文件长期保留，运行日志有界轮转。

回退镜像必须支持当前数据库 schema；0055 包含语音转写，旧版二进制不一定兼容。
保留新消息、家目录、artifact 和执行回执；没有数据损坏证据时不用旧库覆盖新数据，
也不能通过清空上下文、记忆或重建 embedding 掩盖问题。历史状态链及证据恢复不由本版自动完成。

完整变更和残余风险见 [3.8.2 说明](https://github.com/YuanYeYouTao/Yuki-QQbot/blob/v3.8.2/docs/releases/v3.8.2.md)。
