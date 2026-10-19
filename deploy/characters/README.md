# 独立角色部署

每个长期角色运行自己的 Bot、数据库、工作区、人格文件和持久环境。镜像可以相同，
身份、历史、HOME、Manager 数据和执行回执分别保留。部署主机需要 Linux、systemd、
Docker Compose v2、已安装的 gVisor 与 `deploy/sandbox/` 宿主组件。

## 登记已有 Yuki

先登记当前部署，不能用新建命令重新创建已有角色。登记只读取 C1 身份并写入私有清单，
不停止服务、不复制数据库、不授予权限。完整保留原 Compose 项目及所有覆盖文件：

```sh
python3 deploy/characters/adopt_existing.py yuki \
  --project-root /opt/yuki --compose-project yuki \
  --compose-file /opt/yuki/compose.yaml \
  --compose-file /opt/yuki/compose.gateways.yaml \
  --env-file /opt/yuki/.env --manager-root /var/lib/yuki-sandbox \
  --home /var/lib/yuki-sandbox/home --environment-name yuki-environment \
  --socket /run/yuki-sandbox/manager.sock \
  --upstream http://127.0.0.1:8080 --public-url https://assistant.example/yuki
```

上面路径和地址是命令参数示例，必须替换为当前部署的实际值。清单默认位于
`/srv/yuki-characters/<slug>/instance.json`，由 operator 管理，不能交给模型修改。
URL 的公开入口和私有上游必须返回同一个 Character ID。

## 准备新角色

使用已验证的固定 Bot 镜像和环境镜像，明确选择该角色的人格、系统提示和模型 Profile。
新建目录必须不存在，不覆盖已有角色。脚本仅准备配置；添加 `--start` 才会部署。

```sh
deploy/characters/new-character.sh mika --name Mika \
  --image local/yuki:verified-release --environment-image local/yuki-environment:verified-release \
  --port 18081 --public-url https://assistant.example/mika \
  --persona /secure/mika/persona.md --system-prompt /secure/mika/system_prompt.md \
  --models /secure/model_profiles.toml
```

准备结果包括独立 Compose 项目、私有 `.env`、operator 配置、Manager 配置、ACP 程序文件
和 `public-proxy.conf`。把代理片段放入现有 TLS server，审核并执行 Nginx 配置检查；它会
去除公开路径前缀再转发到对应 loopback 端口，不需要另一个 DNS 名称或证书。
镜像构建时应设置 `VCS_REF`，环境中的客户端安装清单记录源提交与依赖摘要。

确认配置后，root 执行 `new-character.sh mika --start`。它只使用已存在的本地镜像，
安装该角色的 systemd 模板配置，创建独立 HOME 文件系统与 Manager，启动 Bot，
在原 API 创建本人 Person、授予该 Person 角色拥有者，再打印首次绑定链接。
在授予或打印代码前核对公开 URL 的 Character ID。中断后重复原命令沿原请求 UUID
查询或恢复，不另建 Person。原绑定码只能显示一次；需要新码时明确执行
`new-character.sh mika --pair-code`。配置、绑定链接和 `.env` 都是私有材料。

`yuki-sandbox@<slug>.service` 的 root、HOME、workspace、socket 和环境容器名称彼此独立。
不要给已有 Yuki 套用新模板替换原 Manager。既有未挂载且非空的 HOME/runtime 目录会被
保留；先按原存储迁移程序处理，再挂载文件系统，不能直接把旧文件隐藏在挂载下。

## 升级与备份

`upgrade-all.sh` 和 `backup-all.sh` 是顺序调度入口，调用 operator 已审核的单实例发行／
备份程序，任一实例失败立即停止，后面的实例不执行。必须明确提供该程序，脚本不猜测
生产部署操作，也不以一个空目录代替完整备份：

```sh
deploy/characters/backup-all.sh --procedure /opt/operations/backup-one-instance
deploy/characters/upgrade-all.sh --procedure /opt/operations/upgrade-one-instance -- --image local/yuki:next
```

程序在原项目目录运行，通过 `COMPOSE_PROJECT_NAME`、`COMPOSE_FILE`、`COMPOSE_ENV_FILES`
保留原上下文。`YUKI_INSTANCE_COMPOSE_ARGV` 是包含所有 `--file` 和 `--env-file` 的 JSON argv；
`YUKI_INSTANCE_METADATA`、`YUKI_INSTANCE_REGISTRY_ENTRY`、`YUKI_INSTANCE_OPERATION`、
`YUKI_INSTANCE_SLUG` 提供当前实例信息，不含解密后的凭据。可重复 `--instance <slug>`
选择顺序，默认按登记名称排序。旧实例不能丢掉网关覆盖文件或被替换为新实例模板。

备份程序须遵循 [Work 证据备份](../../docs/operations/work-evidence-backup.md)：停止 Bot 写入、
SQLite backup、完整 data/work-protocol/tool_artifacts/workspace/config、原镜像和迁移版本，
运行原 `verify_work_backup.py`。持久环境的 HOME、Manager 数据和容器可写层还须按原环境
停机一致性程序保存，不能把数据库备份当成完整环境快照。升级只替换原 Bot，保留网关、
环境、原 Work ID、预算和回执；有新事实时不能直接覆盖为旧快照。

实际用量用 `docker stats --no-stream <原Bot容器> <原环境容器>` 记录，不能用配置额度作为
测量结果。原生部署、资源用量、故障恢复和备份恢复应在目标 Linux 主机验证。

角色协作入口见 [collaboration.md](collaboration.md)。本目录脚本不会向 Git 远端推送、
发布镜像或自动拉取未固定的镜像。
