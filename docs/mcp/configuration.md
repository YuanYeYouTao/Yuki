# MCP 配置

Yuki 只读取 `MCP_CONFIG_PATH` 指定的 UTF-8 JSON，不扫描或导入其他客户端配置。本机运行复制
`.mcp.json.example` 为 `.mcp.json`；Docker 使用已有只读 `config/` 挂载，复制为
`config/mcp.json` 并设置 `MCP_CONFIG_PATH=/app/config/mcp.json`。然后设置
`MCP_ENABLED=true`。

支持 `command`、`args`、`cwd`、`env`、`url`、`headers`、`disabled`、`lifecycle`、
`connectTimeoutSeconds`、`requestTimeoutSeconds`、`reconnectDelaySeconds`、`includeTools`、`excludeTools`，以及
`yuki.scope/summary/tags/toolAnnotations`。`command` 与 `url` 必须且只能填写一个。

`toolAnnotations` 按远端工具名覆盖 MCP 标准提示字段：`readOnlyHint`、`destructiveHint`、
`idempotentHint` 和 `openWorldHint`。旧本地扩展 `finalizeAfterCommit` 仅保留配置读取兼容，
不再关闭 Agent 工具或强制最终回复。操作结果进入原循环；是否继续由 Agent 根据目标和回执决定，
授权和未知效果重放限制仍在执行层核验。
这些提示只影响 Tool Kernel 的调度元数据，不会改写远端 Schema 或绕过工具本身的鉴权。配置变化会
使旧工具缓存失效。

Secret 只能写成 `${ENV_NAME}` 并放在 `.env` 或宿主环境中。Yuki 不把解析后的 Header、Cookie、
环境变量值写入日志、数据库、Prompt 或状态接口。Server 的 URL 和鉴权方式以对应服务提供者为准。
麦当劳连接已退出默认配置，不再提供其工具、Bundle 或自动化委托。

`reconnectDelaySeconds` 控制 `keep_alive` / `lazy_keep_alive` 断线后的重试间隔。重试没有
代码内固定次数上限；停用 Server 或关闭应用会取消恢复任务。
