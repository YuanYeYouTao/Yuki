# MCP Transports

## stdio

`command` 配置由 Yuki 启动子进程，官方 MCP Python SDK 通过标准输入输出通信。`lazy` 在第一次
目录发现或调用时启动，`lazy_keep_alive` 首次使用后保持；应用退出时 Session 和由 Yuki 启动的
子进程都会关闭。本地网易云结构示例见 [`examples/netease_music.json`](examples/netease_music.json)。

每个 SDK Session 由一个连接拥有者任务管理；初始化、请求取消和异步上下文关闭均由该任务协调，
因此应用生命周期任务可以安全回收连接，不会跨任务退出 AnyIO cancel scope。

## Streamable HTTP

`url` 使用官方 SDK 的 Streamable HTTP Client。Yuki 不对外开放新端口，也不会在退出时终止远程
HTTP Server。Header 支持环境变量插值，重定向默认不跟随。连接字段见配置模板。

`eager` 和 `keep_alive` 在启动时发现工具；lazy 模式不会因 `/healthz` 被连接。连接失败会保留真实
错误类别。`keep_alive` 和首次使用后的 `lazy_keep_alive` 按 Server 的
`reconnectDelaySeconds` 持续恢复；其他生命周期可由 `refresh`、`reconnect` 或下一次使用重建
Session。
