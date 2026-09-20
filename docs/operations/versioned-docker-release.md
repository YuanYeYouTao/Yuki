# Versioned Docker Release

应用版本以 `pyproject.toml` 为源，数据库目标以随包 Alembic 单一 head 为源。
当前开发基线见 [README](../../README.md)，发布范围见[发布说明](../releases/v3.8.3.md)，
数据库与部署步骤见[升级指南](../upgrade-3.8.3.md)。历史 Release 保留对应版本的记录，不作为当前部署指令。

## 仓库与 CI

`scripts/release_validate.py` 核对项目版本、运行时版本、锁文件、安装器和当前发布文档，并检查迁移图。
普通 PR 的 Quality workflow 在构建和测试前执行该检查；正式发布复用同一检查，再核对 tag 与 main 的提交关系。
Memory release check 读取项目版本与迁移图，不另存版本常量或迁移文件清单。

## 发布流程

正式产物由 `.github/workflows/release.yml` 发布，目标平台为 `linux/amd64`：

1. 将审核通过的变更合并到 main，确认 Quality 结果与当前发布说明。
2. 用对应提交创建 `vX.Y.Z` 标签；已有标签与发行资产不覆盖。
3. Release workflow 构建镜像、部署包、安装入口及 SHA256SUMS，并执行发布 smoke。
4. 发布完成后更新 README 的正式版下载入口。只有推进开发基线时，不提前声称新镜像或安装包已发布。

首次配置 GHCR 可使用 workflow 的 bootstrap 模式；该模式只准备镜像访问，不等于正式发布。
Genie-TTS Worker 的发行镜像标签跟随应用版本，其内部组件版本独立维护。

## 现有服务器

生产热修沿用本地构建、传服务器加载的方式，明确记录代码提交、镜像与数据库版本。
运行正常且未授权部署时，仓库基线推进不触发容器重建或数据库变更。
执行部署时保留原 Compose 覆盖、挂载和可用恢复点，按升级指南只更新相关服务。
