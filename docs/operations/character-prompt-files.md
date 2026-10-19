# 可写角色提示词

角色使用现有 `SYSTEM_PROMPT_FILE` 和 `BOT_PERSONA_FILE`。桌面与 operator 编辑同一份文件，
热套用沿运行中 Settings；原身份、记忆和 Work 不迁移。Compose 将 `./character` 可写挂载到
`/app/character`，`./config` 保持只读。

新部署经过 `yuki setup` 确认后，向导从实际所选来源准备 `character/system_prompt.md` 与
`character/persona.md`，设置文件变量并移除旧 SYSTEM_PROMPT 字符串。取消确认时不准备文件。

现有部署使用包含本功能的 Bot 程序，显式准备：

```sh
yuki setup prepare-prompts --deployment-root /absolute/deployment/root
```

仅使用部署映像时，可以沿安装器的方式执行：

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  --entrypoint yuki --volume "$PWD:/deploy" --workdir /deploy \
  "$IMAGE" setup prepare-prompts --deployment-root /deploy
```

命令读取该部署 `.env` 中的文件来源；没有 SYSTEM_PROMPT_FILE 时取 SYSTEM_PROMPT 的实际
值。支持多行、UTF-8、dotenv 插值及旧人格路径别名。容器 `/app/…` 路径映射到所选部署根目录。
启动环境另有来源覆盖时，显式加 `--prompt-source process`，并在具有同样来源变量的环境中
执行；准备后的启动环境也应改为输出中的文件路径。

复制保留原字节，文件权限为 0640；同目录临时文件 fsync 后发布，不覆盖已经存在且不同的
角色文件。两份文件核对原哈希与实际渲染结果后才更新 `.env`，其他字段及多行值保留。
中断可重跑：完整文件复用，不产生第二套来源。输出只有路径、版本和渲染哈希，不含提示词
正文。ENV 字符串的边界空白或 CRLF 若无法沿既有文件 strip 规则无损保留，准备会说明冲突，
由维护者先明确处理来源内容。

原 config 文件保留，更新后仅 character 路径参与运行。准备动作写 restart-required 提示，
不启动、停止服务，不执行数据库升级，也不请求模型。实际重启按部署操作授权执行。
启动脚本把目录及两个规范文件交给容器 Bot 用户；只读部署可继续使用 API 的只读呈现。
重启后以 `yuki admin character prompt show` 核对 active 哈希，并检查新激活的实际前缀。

`character/` 是私人运行数据，Git 忽略、Docker context 以 `character/**` 排除；发布归档只
带空目录和本说明。包检查同时保留 `src/yuki/character` 产品模块。Docker pattern 规则见
[官方构建上下文说明](https://docs.docker.com/build/concepts/context/#dockerignore-files)。
