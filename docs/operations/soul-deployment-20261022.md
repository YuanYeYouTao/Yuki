# 单 SOUL 部署与升级

本页对应 #478 的单人格合同。仓库的 `config/SOUL.md` 是可选初始模板，实际用户原件是
`character/SOUL.md`，`SOUL_FILE` 指向这一个文件。角色名称在数据库角色资料中维护，
文件中的 `{{name}}` 在新请求准备时展开；正文也可以不使用变量或保持为空。

## 新部署

运行现有 `yuki setup --deployment-root <部署目录>` 并确认保存。向导只在
`character/SOUL.md` 不存在时从模板初始化，已存在的正文原样保留；取消不会创建人格文件。
配置与安装不会启动服务或替用户执行数据库升级。

在部署目录显式初始化自己的角色名称，再启动服务：

```sh
docker compose run --rm --entrypoint yuki bot init-db --character-name "你的角色名称"
docker compose up -d bot
```

先按现有持久环境文档准备 Manager 和网关等实际依赖。普通 `start.sh` 只执行原数据库迁移，
不从模板复制人格、不改角色名称；重复初始化也不覆盖已有角色资料。

源码部署使用同一份 `character/SOUL.md`。先创建目录并将自己选定的模板或正文复制到该文件，
在 `.env` 设置 `SOUL_FILE=character/SOUL.md`，再执行
`uv run yuki init-db --character-name "你的角色名称"`。不要把当前工作目录中的其他文件当作默认来源。

## 既有部署升级

按现有数据库升级流程停 Bot、保存一致备份并验证迁移来源；本次不覆盖旧数据库或原执行回执。
原 Main 的系统文件与后台 `persona.md` 可能不是同一份设定，维护者先比较实际原件，
选定需要的人格内容并整理为 SOUL。工具、权限、Work、发送与事实处理规则由程序合同提供。
不要自动拼接两份正文或全文替换姓名。

1. 将已审阅的正文保存为 `character/SOUL.md`，保存转换前原件到受保护的临时升级备份。
2. `.env` 只设置 `SOUL_FILE=character/SOUL.md`；删除旧 `SYSTEM_PROMPT`、`SYSTEM_PROMPT_FILE`、
   `BOT_PERSONA_FILE`、`YUKI_PERSONA_FILE`、`BOT_DISPLAY_NAME`。`BOT_ALIASES` 仅保留希望继续使用的显式唤醒别名。
3. 使用新的 Compose 文件；它设置容器内 `SOUL_FILE=/app/character/SOUL.md`，并将
   `./character` 作为可写目录挂载。不要只挂载单个文件，编辑器需要原子替换文件。
4. 按实际迁移谱系升级数据库。尚无角色资料的实例使用自己的名称执行上面的显式初始化命令；
   已有角色保留原身份和名称，通过角色资料接口改名。
5. 核实 Bot 启动及编辑入口读到实际原件。保存或显式 reload 仅影响后续合法新激活；
   原已付请求、私有协议检查点、历史名字、工具回执和公共历史保原事实。

当前运行时不读取旧人格变量、占位符或旧文件，不提供旧双文档 API。备份只供核对，
升级完成后按既有部署清理流程处理，不成为运行时后备来源。

## 编辑与文件权限

owner 或获准 operator 使用现有角色入口编辑单 SOUL，或直接修改文件后显式 reload。
编辑版本和原请求回执用于报告真实写入与生效结果；空 SOUL 不会自动填回模板。
文件读取失败如实报告，不改读其他路径。

容器启动将 `character` 目录交给 Bot 的 UID/GID `10001:10001`，目录可写以便发布新正文。
PowerShell 安装器沿既有私人配置 ACL 处理该目录。`character/` 不进入 Git、Docker 构建上下文或
发行包；包内只带 `config/SOUL.md` 模板与空目录。不要把导出的私人草稿放进公开源码或普通诊断。

源码验证、CI、PR、合并、镜像和上线各自记录；本文不是生产已升级的证明。
