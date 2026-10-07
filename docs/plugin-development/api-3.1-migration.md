# Plugin API 3.1 历史迁移

> 本文记录 MCP 退出时的 3.1 合同。当前 Host 只接受 3.3，完成这些历史步骤后继续 [API 3.2 历史迁移](api-3.2-migration.md) 和 [API 3.3 迁移](api-3.3-migration.md)。

Plugin API 3.1 删除 `ctx.mcp`、`MCPFacade`、`mcp.read`、`mcp.call` 和
`mcp.facade.v1`。Host 不再连接或调用 MCP Server，也没有空 facade 兼容层。
普通插件 HTTP、自有工具、原生媒体和 QQ 音乐卡片发送接口继续保留。

更新插件源码和 manifest：

```toml
plugin_api = "3.1"
```

- 删除 MCP 调用和权限声明；不能仅改版本号后继续调用已删除接口。
- 依赖 MCP 的网易云音乐卡片插件已退役。3.1 不自动改成 HTTP 或其他后端。
- 工具图片使用获准 `media.artifact.create` 创建的本插件句柄，并在本次
  `ToolResult.media_artifacts` 中明确选择。旧 `mcp.call` 批准不会转换成新媒体权限。
- Host 仍核原 owner、hash、TTL、工具、manifest、批准版本与自动化委托；私有归档
  不延长原句柄有效期，也不能绕过删除或跨插件读取。
- Host 只加载精确声明 3.1 的插件；旧 API 或未知权限的插件被逐个拒绝，不导入其代码。
- API、manifest 或权限变化会撤销旧批准，插件回到 disabled/pending_approval。
  管理员审核新源码和 manifest 后，按原批准流程重新批准并启用；没有自动审批。

旧安装记录、批准审计、执行回执和已拥有的合法产物按原保留规则保存。
未确认的外部效果不能因接口退役被宣布未执行、重发或重置预算。

从 2.x 升级应先阅读 [3.0 历史迁移](api-3.0-migration.md)，再完成本文。
媒体用法见[媒体与视觉](media-and-vision.md)，当前合同见[兼容性](compatibility.md)。
