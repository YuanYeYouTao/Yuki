# 工具发现与选择

`tools/list` 结果会转为稳定的 MCP metadata 并缓存。配置哈希一致且 TTL 有效时，重启后可直接用于
目录检索；哈希变化后旧缓存不用于执行，下一次连接重新发现。

Capability Runtime 用进程内 SQLite FTS5 BM25 从统一目录做本地检索。没有
`all` / `catalog` / `hybrid` / `gateway` 选择模式，也没有 `ModelTask.TOOL_SELECTION` 或
Flash 精排。已缓存的 MCP 工具进入统一目录；`mcp_gateway` 描述符只在 Server 启用 gateway
时加入。

主 Agent 在启动准备阶段收集完整 MCP Schema，随后冻结工具名称、顺序和参数结构。
`request_tools` 查询目录和用法，不追加、删除或重排主循环中的声明，也不能扩大权限。
两个 Server 的同名工具通过 Server ID 隔离。

元数据缓存与固定请求合同是两层机制：刷新目录不等于修改运行中的主 Agent 清单。
工具定义变化后重启 Bot 形成新合同；旧自动化委托仍须通过元数据版本和权限校验。
详见 [共同架构约束](../architecture/development-contract.md)。
