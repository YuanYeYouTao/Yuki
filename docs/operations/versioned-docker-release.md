# Versioned Docker Release

应用版本以 `pyproject.toml` 为源，数据库目标以随包 Alembic 单一 head 为源。4.0.0 对应 `v4.0.0`：数据库 head `0156`、Plugin API `9.0`，变化见[发布说明](../releases/v4.0.0.md)，升级见[升级指南](../upgrade-4.0.1.md)。历史版本的发布物与说明保持各自的版本归属。

## 仓库与 CI

Quality workflow 检查 Ruff、Mypy 和 pytest，另跑有头桌面、Code Mode 与 `sandbox-compose`（用真实安装器装 `lite`，经 Manager 使用环境，再换到 `headless`）。正式 tag 发布使用 `scripts/release_validate.py` 核对 tag、项目版本、运行时版本、锁文件、安装器及 main 祖先关系；迁移与启动由实际构建镜像的 release smoke 核验。

Python 发行包／模块为 `yuki`，命令为 `yuki` 与 `yuki-bot`，镜像为 `ghcr.io/yuanyeyoutao/yuki`；沙箱环境镜像为 `ghcr.io/yuanyeyoutao/yuki-environment`。

## 发布流程

正式产物由 `.github/workflows/release.yml` 发布，目标平台为 `linux/amd64` 和 `linux/arm64`，共用版本标签与部署包：

1. 将审核通过的变更合并到 main，确认 Quality 结果与当前发布说明。
2. 用对应提交创建 `vX.Y.Z` 标签；已有标签与内容不同的发行资产不覆盖，同内容附件在重跑时复用。
3. Release workflow 经 Buildx/QEMU 构建并推送包含两种架构的 Bot 镜像索引，以及沙箱环境镜像 `yuki-environment:X.Y.Z` 与 `X.Y.Z-desktop`（同一版本已存在时复用），同时生成部署包、安装入口及 SHA256SUMS。每个架构单独拉取到干净部署目录，核对实际镜像版本、提交与平台，再验证迁移、启动和重建后的持久数据。
4. 发布提交同步更新 README 的正式版下载入口，并在流水线完成后核对链接、镜像和附件。只有推进开发基线时，不提前声称新镜像或安装包已发布。

默认 Bot 镜像使用 Dockerfile 的 `direct` target，`runtime` 也继承它；Compose 中的沙箱 Manager 使用同一镜像。镜像不包含 Monty binding、worker 或 launcher，`CODE_MODE_ENABLED=false`。安装器和默认 Compose 采用同一 direct 发行物，全部获准工具仍由原 Agent loop 直接声明，授权与恢复不变。发布 smoke 在镜像内执行 `scripts/verify_monty_packaging.py direct` 核验无 Monty 与默认关闭。

两套安装器读取 Docker Engine 的 Linux 架构，识别 amd64/x86_64 与 arm64/aarch64，并用该平台拉取镜像、运行配置向导；不使用客户端 CPU 猜测远端 Docker 主机。Compose 不再固定 Bot 或 SnowLuma 为 amd64，删除会跳过显式 `compose pull` 的 `pull_policy: missing`，已有另一平台镜像时也能拉取目标架构。复用已有版本时也逐架构核对同一提交，缺少任一架构即失败，不覆盖旧版本资产。`latest` 用 `buildx imagetools create` 复制已验证的完整索引，不能把最后拉取的单架构镜像重新 push 成 latest。

发布任务通过 `setup-docker-action` 安装当前 Docker Engine，使用 `docker image inspect --platform` 检查目标架构；[该检查](https://docs.docker.com/reference/cli/docker/image/inspect/)需要 Docker API 1.49 或更新版本，仅作用于发布验证，不改变安装器的用户端要求。

发布资产包括 `yuki-X.Y.Z-deploy.zip`、`yuki-X.Y.Z-deploy.tar.gz`、`docker-compose.yml`、`install.sh`、`install.ps1` 与 `SHA256SUMS`。独立下载的环境模板名为 `default.env.example`，压缩包内仍是 `.env.example`；校验单保留构建时的 `.env.example` 文件名。下载后比较实际校验和，不以 Release 页面或源码版本号代替附件验证。部署包含 `deploy/sandbox/prepare.sh`（创建沙箱数据卷）与 `deploy/sandbox/migrate-host-manager.sh`（从宿主 Manager 一次性迁移）。

Code Mode 是显式可选构建：使用 `docker build --target codemode`，按 [Code 运维](pi-codemode-operations.md) 通过原生隔离门禁后，显式设置 `YUKI_CODE_IMAGE` 并叠加 `docker-compose.codemode.yml`。默认发行不安装或加载 Code 专用 AppArmor/seccomp。

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

每次上线完成后清理服务器上的旧备份、部署临时文件、废弃镜像与可重建构建缓存，
本次切换准备的临时恢复副本也在新 Bot 健康核验后删除。先确认实际挂载、现用 Compose
配置与服务引用，再删除具体路径；现用配置、业务数据库、工作区、被引用媒体、原执行
和投递回执不属于垃圾缓存。清理不停止或删除其他服务、现用镜像及持久卷，记录实际回收结果。

Bot 的 Docker 探针请求 `/livez`，只检查 ASGI 事件循环能否及时响应；进程退出或
主循环停顿仍使探针失败。依赖可用性与 Worker 的基本状态保留在 `/healthz`，运维监控
须继续读取该端点的 `status`、`database` 等字段。Memory 一致性与自省积压沿原授权管理
入口按需读取；Rebuild 和 Dream 的原授权列表与单次运行状态仍可读取，不等于原公开全局
汇总。Work 与自动化的全局计数不作为公开健康字段，各自业务状态沿原入口读取。这些详细
诊断不作为每次公开健康检查的历史扫描。`/healthz` 在 `status=degraded` 时仍返回 HTTP 200，
不能仅凭 HTTP 状态码将它判定为业务健康。

公开响应不再包含 `active_automation_count`、Memory 一致性诊断字段和 `memory_rebuild`／
`memory_dream` 的全局汇总；`memory_self_reflection` 只保留 `enabled` 与 `running`，
`runtime_work` 只保留运行态。依赖旧字段的外部监控须调整，不以零值或默认健康代替原统计。
