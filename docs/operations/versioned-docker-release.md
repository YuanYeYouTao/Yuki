# Versioned Docker Release

应用版本以 `pyproject.toml` 为源，数据库目标以随包 Alembic 单一 head 为源。
当前正式版见 [README](../../README.md)，3.8.4 的合并范围见[发布说明](../releases/v3.8.4.md)，
数据库与部署步骤见[升级指南](../upgrade-3.8.4.md)。历史 Release 保留对应版本的记录。

## 仓库与 CI

`scripts/release_validate.py` 核对项目版本、运行时版本、锁文件、安装器和当前发布文档，并检查迁移图。
普通 PR 的 Quality workflow 在构建和测试前执行该检查；正式发布复用同一检查，再核对 tag 与 main 的提交关系。
Memory release check 读取项目版本与迁移图，不另存版本常量或迁移文件清单。

## 发布流程

正式产物由 `.github/workflows/release.yml` 发布，目标平台为 `linux/amd64`：

1. 将审核通过的变更合并到 main，确认 Quality 结果与当前发布说明。
2. 用对应提交创建 `vX.Y.Z` 标签；已有标签与内容不同的发行资产不覆盖，同内容附件在重跑时复用。
3. Release workflow 构建镜像、部署包、安装入口及 SHA256SUMS，并执行发布 smoke。
4. 发布提交同步更新 README 的正式版下载入口，并在流水线完成后核对链接、镜像和附件。只有推进开发基线时，不提前声称新镜像或安装包已发布。

首次配置 GHCR 可使用 workflow 的 bootstrap 模式；该模式只准备镜像访问，不等于正式发布。
默认 Bot 镜像使用 Dockerfile 的 `direct` target，`runtime` 也继承它：不包含 Monty binding、worker 或 launcher，`CODE_MODE_ENABLED=false`。安装器和默认 Compose 采用同一 direct 发行物，全部获准工具仍由原 Agent loop 直接声明，授权与恢复不变。发布 smoke 在镜像内执行 `scripts/verify_monty_packaging.py direct` 核验无 Monty 与默认关闭。

Code Mode 是显式可选构建：使用 `docker build --target codemode`，按 [Code 运维](pi-codemode-operations.md) 通过原生隔离门禁后，显式设置 `YUKI_CODE_IMAGE` 并叠加 `docker-compose.codemode.yml`。默认发行不安装或加载 Code 专用 AppArmor/seccomp。

当前 bootstrap、release 和 finalize 仅构建、拉取和校验 Bot 镜像；此前 Release 的 Genie-TTS Worker 资产保留历史归属，不作为新部署依赖。

新部署的配置向导默认开启模型搜索，不要求新增 Tavily 密钥。Gemini 主 Agent
明确选择 `search_mode="bridge"`，通过独立请求检索真实来源；支持原生搜索的
OpenAI Responses / Anthropic 主连接选择 `native` 并声明 `native_web_search`。
所选模型及接入服务仍须实际支持该能力；向导的本地验证不等于真实 API 验收。
默认 DeepSeek 示例保留 Responses 主对话，另以 `search_mode="external"`、
`search_connection="primary_agent"` 和 `WEB_SEARCH_BACKEND=deepseek_anthropic`
明确使用官方独立搜索，不宣称主对话具备原生搜索。

向导仍允许选择“关闭”；不支持模型搜索的主连接需选择 Tavily 并提供密钥，
或明确配置可用的搜索连接。Gemini 搜索桥返回服务提供的检索来源，未配置 Tavily
时网页提取不可用会如实报错。现有 `.env` 的显式关闭和模型档案的搜索选择不会
被新默认覆盖；`basic` / `flash` 重建遇到无法保留的独立搜索配置时拒绝写入。

## 现有服务器

生产热修沿用本地构建、传服务器加载的方式，明确记录代码提交、镜像与数据库版本。
运行正常且未授权部署时，仓库基线推进不触发容器重建或数据库变更。
执行部署时保留原 Compose 覆盖、挂载和可用恢复点，按升级指南只更新相关服务。

Bot 的 Docker 探针请求 `/livez`，只检查 ASGI 事件循环能否及时响应；进程退出或
主循环停顿仍使探针失败。详细依赖与 Worker 状态保留在 `/healthz`，运维监控须继续
读取该端点的 `status`、`database` 等字段。`/healthz` 在 `status=degraded` 时仍返回
HTTP 200，不能仅凭 HTTP 状态码将它判定为业务健康。
