# Yuki 3.8.3 配置与升级

<!-- release-baseline: version=3.8.3 schema=0061 -->

本页对应正式 [3.8.3 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.3) 发行包，而非此后继续开发的源码。该包随附 Alembic head `0061`、Plugin API 2.0；使用更晚的源码或镜像时，按其随包迁移和对应升级指南处理。下一发布基线见 [3.8.4 升级指南](upgrade-3.8.4.md)。

## 版本对应

| 来源 | 数据库 | 升级处理 |
| --- | --- | --- |
| 3.8.2 正式发布包 | `0055` | 使用 3.8.3 目标镜像的迁移链升级至 `0061` |
| 后续部署 | 读取实际 Alembic revision | 与目标镜像 head 一致才可跳过迁移 |
| 更旧部署 | 核对实际 Alembic 记录与 canonical 基线 | 不承诺直接升级，不使用 `stamp` 跳过迁移 |

应用版本不能代替数据库版本检查。历史 Release 附件内容固定；当前仓库中的安装器、`.env.example` 和 README 可能属于下一源码基线。

## 部署流程

正式部署使用 [3.8.3 发行包](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.3)。
保留原项目名、Compose 覆盖文件、配置和挂载，不用新模板覆盖已有部署目录。

1. 暂停 Bot 写入并保存一致的数据库及配置备份；持久家目录、artifact 和执行回执保留。
2. 使用 3.8.3 目标镜像执行 `qq-ai-bot-cli init-db`，由 Alembic 升级至随包 head。
3. 仅重建 Bot，检查健康、QQ 连接、任务调度和固定工具合同。QQ 网关和独立 Manager 无相关改动时保持运行。

所有 Compose 命令沿用部署原有项目名及全部覆盖文件。Manager 自身更新另行处理。

麦当劳退出需同步移除部署 `mcp.json` 中的 `mcd`、连接凭据及旧工具缓存；不能只更新示例文件。保留历史审计和已结束任务。插件安装批准仍生效。

## 恢复

恢复镜像需兼容当前数据库。保留升级后新增的消息、文件、预算及执行回执，代码回退不等于恢复旧数据库。

配置向导仍只负责填写配置；持久环境部署参见[操作说明](operations/persistent-environment.zh-CN.md)，历史 3.8.2 操作记录见[旧版指南](upgrade-3.8.2.md)。
