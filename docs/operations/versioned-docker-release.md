# Versioned Docker Release

应用版本以 `pyproject.toml` 为源，数据库目标以随包 Alembic 单一 head 为源。
3.9.0 正式发行对应 `v3.9.0`，数据库 head 为 `0119`、Plugin API 为 `3.5`，合并范围与贡献者见[发布说明](../releases/v3.9.0.md)，配置和数据处理见[升级指南](../upgrade-3.9.0.md)。
正式镜像为 `ghcr.io/yuanyeyoutao/yuki-qqbot:3.9.0`；部署包、安装器和校验和从 [Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.9.0) 获取。3.8.4 的发布物与[历史升级说明](../upgrade-3.8.4.md)保持其原版本归属。

## 仓库与 CI

当前开发分支的 Quality workflow 检查 Python 的 Ruff、Mypy 和 pytest；旧 WebUI 前端已退役。3.9.0 发行时的检查范围保留在原发布记录中。
正式 tag 发布使用 `scripts/release_validate.py` 核对 tag、项目版本、运行时版本、锁文件、安装器及 main 祖先关系；不再运行 Memory synthetic quality/baseline/release-check 或独立迁移矩阵。迁移与启动由实际构建镜像的 release smoke 核验。

当前源码已统一为 Python 发行包／模块 `yuki`，只发布 `yuki` 与 `yuki-bot` 两个命令。后续发行 workflow 使用 `ghcr.io/yuanyeyoutao/yuki`；源码开发需本地构建，不把未发布的新名称标签当作可拉取的 3.9.0 产物。上方正式 3.9.0 的旧镜像地址继续表示原发行事实。既有生产保实际数据库、Compose 项目与网络，只按完整配置替换 Bot。

## 发布流程

正式产物由 `.github/workflows/release.yml` 发布，目标平台为 `linux/amd64` 和 `linux/arm64`，共用版本标签与部署包：

1. 将审核通过的变更合并到 main，确认 Quality 结果与当前发布说明。
2. 用对应提交创建 `vX.Y.Z` 标签；已有标签与内容不同的发行资产不覆盖，同内容附件在重跑时复用。
3. Release workflow 经 Buildx/QEMU 构建并推送包含两种架构的镜像索引，同时生成部署包、安装入口及 SHA256SUMS。每个架构单独拉取到干净部署目录，核对实际镜像版本、提交与平台，再验证迁移、启动和重建后的持久数据。
4. 发布提交同步更新 README 的正式版下载入口，并在流水线完成后核对链接、镜像和附件。只有推进开发基线时，不提前声称新镜像或安装包已发布。

默认 Bot 镜像使用 Dockerfile 的 `direct` target，`runtime` 也继承它：不包含 Monty binding、worker 或 launcher，`CODE_MODE_ENABLED=false`。安装器和默认 Compose 采用同一 direct 发行物，全部获准工具仍由原 Agent loop 直接声明，授权与恢复不变。发布 smoke 在镜像内执行 `scripts/verify_monty_packaging.py direct` 核验无 Monty 与默认关闭。

两套安装器读取 Docker Engine 的 Linux 架构，识别 amd64/x86_64 与 arm64/aarch64，并用该平台拉取、运行配置向导；不使用客户端 CPU 猜测远端 Docker 主机。Compose 不再固定 Bot 或 SnowLuma 为 amd64，删除会跳过显式 `compose pull` 的 `pull_policy: missing`，已有另一平台镜像时也能拉取目标架构。复用已有版本时也逐架构核对同一提交，缺少任一架构即失败，不覆盖旧版本资产。`latest` 用 `buildx imagetools create` 复制已验证的完整索引，不能把最后拉取的单架构镜像重新 push 成 latest。

发布任务通过 `setup-docker-action` 安装当前 Docker Engine，使用 `docker image inspect --platform` 检查目标架构；[该检查](https://docs.docker.com/reference/cli/docker/image/inspect/)需要 Docker API 1.49 或更新版本，仅作用于发布验证，不改变安装器的用户端要求。

[PR #431](https://github.com/YuanYeYouTao/Yuki/pull/431) 已将双架构构建、安装器选择和索引保留纳入 3.9.0 发行链；旧 Release 的镜像与附件不追溯改写。多架构构建与索引行为见 [Docker 文档](https://docs.docker.com/build/ci/github-actions/multi-platform/)。

发布资产包括 `yuki-3.9.0-deploy.zip`、`yuki-3.9.0-deploy.tar.gz`、`docker-compose.yml`、`install.sh`、`install.ps1`、`Yuki-3.9.0-Upgrade.md` 与 `SHA256SUMS`。独立下载的环境模板名为 `default.env.example`，压缩包内仍是 `.env.example`；校验单保留构建时的 `.env.example` 文件名，核对独立下载附件时对照这一项的 SHA256。下载后比较实际校验和，不以 Release 页面或源码版本号代替附件验证。

Code Mode 是显式可选构建：使用 `docker build --target codemode`，按 [Code 运维](pi-codemode-operations.md) 通过原生隔离门禁后，显式设置 `YUKI_CODE_IMAGE` 并叠加 `docker-compose.codemode.yml`。默认发行不安装或加载 Code 专用 AppArmor/seccomp。

当前 tag Release workflow 仅构建、拉取和校验 Bot 镜像；此前 Release 的 Genie-TTS Worker 资产保留历史归属，不作为新部署依赖。

## 3.9.0 执行与升级边界

主 manifest 为 19，子 Agent 使用 Main 的同一完整声明；Code 控制 API 为 `yuki.codemode.api.v2`，Plugin API 仍独立使用精确的 `3.5`。合同或模式变化沿合法新链生效；旧已付请求、已配对回执和累计预算保留。旧 `code_stop` 不复活原 VM，不兼容的未配对 Code 快照按 `snapshot_unavailable` 保留事实，不能通过换镜像重新派发旧效果。

普通结束、等待、提问和交接决定在原批次及 Code 程序收拢后结算，仍获准的尾部发送与保存继续执行。真实当前目标撤权、来源失效和有限预算继续生效；真实子问题沿原父子输入恢复，终态父不被通知复活。升级保留原 Work ID、原执行与发送回执，重启、compact 或跨架构迁移不重置预算。

3.8.4 的 `0072` 数据库沿随包迁移升级到 `0119`；开发部署从实际 revision 继续，新部署在空库初始化。0119 保留合法公共检查点并删除旧 Rollup 派生链；正式包也包含旧空摘要窗口的 reader 修复，保持原 epoch 与冻结历史。升级无需重做历史 generation reset 或 `/ai new`。旧 NapCat 配置和 Genie 服务按[升级指南](../upgrade-3.9.0.md)处理，QQ 登录、用户插件、ASR 和被引用媒体保留；不以清理旧组件为由删除私密资料。

本轮 Work 状态使用 `<runtime_context>` 标签，固定系统约束明确它是程序提供的本轮资料；当前触发正文仍位于其后。该状态不冻结到公共历史或公共 compact 检查点。缓存与成本观察使用实际 usage，未知覆盖单列；请求前缀哈希和历史样本不代表此次发行的总体命中率。

## 新部署配置

新部署的配置向导默认开启模型搜索，不要求新增 Tavily 密钥。Gemini 主 Agent
明确选择 `search_mode="bridge"`，通过独立请求检索真实来源；支持原生搜索的
OpenAI Responses / Anthropic 主连接选择 `native` 并声明 `native_web_search`。
所选模型及接入服务仍须实际支持该能力；向导的本地验证不等于真实 API 验收。
默认 DeepSeek 示例保留 Responses 主对话，以 `search_backend="model"` 和独立 Messages
搜索连接共用原 DeepSeek 密钥引用，不宣称主对话具备内联原生搜索，也不需要 Claude 账户。
安装向导与 Admin API / CLI 共用搜索默认配置生成；Gemini 默认桥接自身连接，Anthropic
Messages 默认内联原生搜索。自定义网关沿其真实地址与凭据配置，不按品牌转发密钥。

向导仍允许选择“关闭”；不支持模型搜索的主连接需选择 Tavily 并提供密钥，
或明确配置可用的搜索连接。Gemini 搜索桥返回服务提供的检索来源，网页读取使用直接
HTTP 提取，不需要 Tavily 密钥。缺省后端为 `model`；旧 `WEB_ENABLED=true` 表示模型搜索，
`false` 仍关闭，显式 Tavily 后端继续有效。现有 `.env` 的显式关闭和模型档案的搜索选择不会
被新默认覆盖。已有模型档案由管理 API／CLI 或文件入口调整；再次运行向导保留该档案，不因选择 `basic` / `flash` 重建全部连接。

## 现有服务器

生产热修沿用本地构建、传服务器加载的方式，明确记录代码提交、镜像与数据库版本。
运行正常且未授权部署时，仓库基线推进不触发容器重建或数据库变更。
执行部署时保留原 Compose 覆盖、挂载和可用恢复点，按升级指南只更新相关服务。
普通 Bot 更新保留 QQ 网关、代理和持久环境服务；保存一致数据库、配置、插件与引用文件后，沿原完整 Compose 链替换 `bot`。只有涉及其他真实写者的数据迁移才暂停对应写入，不要求所有 Manager 任务终态。正式发行不会自动替换服务器上的开发热修镜像，生产切换及自然回执按实际运维记录另记。

Bot 的 Docker 探针请求 `/livez`，只检查 ASGI 事件循环能否及时响应；进程退出或
主循环停顿仍使探针失败。详细依赖与 Worker 状态保留在 `/healthz`，运维监控须继续
读取该端点的 `status`、`database` 等字段。`/healthz` 在 `status=degraded` 时仍返回
HTTP 200，不能仅凭 HTTP 状态码将它判定为业务健康。
