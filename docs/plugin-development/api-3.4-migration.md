# Plugin API 3.4 迁移

旧人物评分关系系统已移除。删除 `ctx.relationship`、RelationshipFacade、相关 Fake、`relationship.current.read/read/write` 权限、`relationship.changed` 事件和关系 PromptStage 的使用；不提供空值兼容接口。

将 manifest 的 `plugin_api` 更新为 `3.4`，按现有发现和批准流程重新批准。Host 仍精确匹配版本，旧批准不因插件未调用关系接口而自动保留。人物资料、历史共同群授权、Memory 和实际发送权限保持原合同。

更早版本先完成 [API 3.3 迁移](api-3.3-migration.md)，再完成本页。插件测试直接使用当前 FakeContext，不构造已删除的关系服务。
