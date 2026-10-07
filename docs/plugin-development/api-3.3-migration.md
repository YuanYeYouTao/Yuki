# Plugin API 3.3 迁移

Host 仅加载精确声明 `plugin_api = "3.3"` 的插件。先完成 [3.2 语音退役](api-3.2-migration.md)，再移除 `ctx.llm`、`LLMFacade`、`llm.generate` 和 `llm.generate_with_context`。主 Agent 统一使用 `ctx.agent.run(instruction, context_profile="none", ...)`，返回 `PluginResult`；当前人物或群资料分别选择 `current_user`、`current_group` 并核对对应读取权限。

实际权限取当前批准与显式能力参数的交集。修改 manifest 后须按精确新版本重新批准；旧 `llm.generate` 权限不能自动换成范围更大的 `agent.run`。没有批准的插件保持禁用，不能借升级扩大权限。

`automation.create/update` 按 SDK 的任务合同传参，不传 Host 的 `conversation_key`、平台消息 ID 或内部执行权限。在线管理使用经认证的 Control HTTP；CLI 只在应用完全停机且持有应用锁时提供 bootstrap。

升级保留原 Work、调用 ID、累计预算和效果回执。已接纳的旧调用按原执行读取或明确退役，未知效果不重发。主工具合同独立升级到 16，Code Mode 与 direct 模式各自生成固定声明；SDK 版本不替代主工具合同版本。
