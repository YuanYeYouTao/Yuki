# 工具发现与选择

`tools/list` 结果会转为稳定的 MCP metadata 并缓存。配置哈希一致且 TTL 有效时，重启后可直接用于
目录检索；哈希变化后旧缓存不用于执行，下一次连接重新发现。

已缓存的 MCP 工具进入统一目录；`mcp_gateway` 描述符只在 Server 启用 gateway
时加入。没有单独的模型工具选择任务或主 Agent 运行时目录查询。

主 Agent 在启动准备阶段收集完整 MCP Schema，随后冻结工具名称、顺序和参数结构。
主 Agent 直接收到完整声明；工具可见性不能扩大执行权限。
两个 Server 的同名工具通过 Server ID 隔离。

元数据缓存与固定请求合同是两层机制：刷新目录不等于修改运行中的主 Agent 清单。
工具定义变化后重启 Bot 形成新合同；旧自动化委托仍须通过元数据版本和权限校验。
详见 [共同架构约束](../architecture/development-contract.md)。
