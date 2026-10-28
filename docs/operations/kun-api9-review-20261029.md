# 养鲲 Plugin API 9.0 配套复审

工作区：`D:/Code/My_Github_Repo/Yuki-Kun-Game-api9-20261029`。分支：`codex/kun-plugin-api9-20261029`。基线：现行 main `4605d394d5b34e161021da758b4c9544dc715fad`，API 8.0、产品 1.1.2。原本机工作区停在旧 API 7 分支，已通过 fetch 核查现行 main，再创建隔离 worktree；原工作目录未改。仓库与父目录没有附加 AGENTS.md。

## 变更

产品版本升为 1.1.3，manifest/README/开发资料声明 Plugin API 9.0，Host 最低要求仍为 `>=4.0.0`。开发依赖和 lock 固定真实公开 SDK 9 源码归档 `8720d929057cb91290c911d09b282bcd8fa7e394`。当前消息 fixture 使用内部 `event_id` 和可空 `native_message_id`。

共六个文件：`plugin.toml`、`pyproject.toml`、`uv.lock`、README，以及两份 Host/manifest 契约测试。游戏实现、引擎、状态 schema 和离线迁移脚本无需修改。

## 三轮复审

1. 逐个核查实际 SDK 调用。`plugin.py` 从当前 Person/Space 选择存档，成员目标取原 mention 快照。实现没有读取退休 `message_id`，也不自建消息 dedup；去重仍由 Host 原事件和命令回执负责。没有为升级新增执行别名、ID 转换或新去重层。
2. 独立空环境 `D:/Code/ValidationEnvs/yuki-kun-api9-20261029` 通过锁定依赖安装真实归档，SDK 实际从该环境 site-packages 导入。完整 105 项测试通过，包含真实 Host manifest/权限/entrypoint/注册/启动/停止六阶段契约；QQ 空 native、129 字符 native、App 私聊均能沿内部 Person/Space 保存和读回。重复成员提及仍只选择一次；App 原生字段保持为空，重启后原存档仍在原作用域。
3. 审查最终 diff、锁定依赖和权限边界。插件 ID、entrypoint、六项权限、资源限制及 Host 最低版本与原 main 一致；只有版本和 API major 改变。锁文件只跟随新归档及其固定 Participation 依赖，未放宽依赖规格。SDK 8 Host 即使满足产品最低版本也被正确拒绝；没有给旧字段加 alias、没有 native 长度门槛。生产批准、KV、grants 和游戏经济状态均未读写。

## 验证

- 全部 **105 passed**，最终 0.66 秒，JUnit：`kun-api9-tests.xml`。
- Ruff check 通过，12 个 Python 文件 format check 通过，git diff --check 通过。
- 离线迁移脚本 `--help` 正常；没有运行迁移或访问生产 Host doctor。仓库没有单独 doctor 实现，离线 SDK 生命周期契约已覆盖插件装载、权限、注册、启动与停止。
- 安装使用现有真实 SDK 9 归档构建缓存；没有复制 SDK、增加本机 Host 源码路径或读取部署密钥。

## 组织插件覆盖

已枚举组织五个现行仓库，并核公开 main 的 manifest 路径。独立 Yuki Host 插件只有 Monitor 与 Kun，两者 main 都声明 API 8，均已准备 API 9 配套。Desktop、Participation 与 benchmark 仓库没有 plugin.toml。没有发现第三个需本轮适配的现行 SDK 8 独立插件。版本以 manifest 为准，未采用仓库描述中旧 API 文案。

配套已提交推送为 `d766ff9`，见[PR #6](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game/pull/6)，CI已通过。没有执行线上批准、数据迁移、重启或真实消息发送。
