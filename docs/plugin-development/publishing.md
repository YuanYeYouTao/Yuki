# 发布插件

Plugin API 6.0 没有在线市场或自动下载。发布物是管理员手动审阅并复制到本地 `plugins/<id>/` 的源码目录。

## 发布前清单

1. `plugin.toml` 的目录、ID、版本、入口和权限准确。
2. `yuki_requires` 使用支持范围，`plugin_api = "6.0"`。
3. 权限最小化，高风险权限写明必要性和目标范围。
4. 不包含 `.env`、Token、Cookie、数据库、日志或用户数据。
5. 插件代码只依赖公开 SDK，不导入 `yuki` 或 `_` 内部对象。
6. 提供无网络单元测试和 `run_plugin_contract_tests`。
7. 记录配置键、Secret 名称、网络白名单、存储容量和升级步骤。
8. `stop()`、失败和回滚行为经过测试。

```bash
uv run ruff check path/to/plugin
uv run pytest -q path/to/plugin/tests
uv run yuki plugin validate path/to/plugin
uv run yuki plugin docs path/to/plugin
uv run yuki plugin test path/to/plugin
```

## 独立插件安装

[GitHub Monitor](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor) 与
[养鲲](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game) 各自发布源码、测试和升级工具，
不随 Yuki 源码或部署包附带。以 GitHub Monitor 为例，在部署目录安装：

```bash
git clone https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor.git plugins/github-monitor
chmod -R a+rX plugins/github-monitor
```

目录直接包含 `plugin.toml` 与插件模块。Linux 部署的 Bot 用户需能读取源码并遍历各层代码目录；上面的权限设置同样适用于解包安装。保持原插件 ID，原配置、KV 和投递回执继续由 Host 保存。
插件配置、Secret 与升级步骤见独立仓库，发现与批准使用下方同一安装流程。

## 安装方流程

安装方必须审阅源码和 Manifest，复制目录，执行 `discover/inspect/permissions`，再明确 `approve` 和 `enable`。升级后 Manifest 哈希改变，旧批准自动失效。

不要宣传“可安全运行任意第三方插件”。当前版本是本地可信、同进程模型；强隔离需要未来独立进程运行时。

