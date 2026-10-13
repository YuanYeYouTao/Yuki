# com.example.echo

这是 Yuki Plugin API 3.5 的最小完整示例。它演示：

- 普通用户可用工具 `echo_text`；
- 确定性命令 `echo`；
- `reply.sent` 通知 Hook；
- `plugin_context` Prompt Fragment；
- 插件私有 KV 计数；
- `global`、`user`、`group` 三种配置作用域；
- 使用独立 Fake Facade 的无网络测试。

插件代码只导入公开的 `yuki_plugin_sdk`（以及 handler 类型边界使用的 Pydantic `BaseModel`），不导入 Yuki Host 内部模块，不访问网络、文件、数据库、NoneBot 或 QQ 网关。

到期 Main 可以沿共同工具调用同一个 `echo_text`。管理定时安排使用
[共同 TaskSpec](../../../docs/plugin-development/automation.md)，并按实际需要声明
`automation.manage_self`；定义直接提交给 `ctx.automation.create_task`：

```python
await ctx.automation.create_task({
    "name": "稍后复述",
    "goal": "一分钟后复述 hello，并发送给原请求方。",
    "trigger": {"type": "after", "seconds": 60},
    "delivery": {"target": "auto"},
})
```

在仓库根目录运行：

```bash
uv run pytest -q examples/plugins/com.example.echo/tests
```

复制到运行目录并启用插件系统：

```bash
mkdir -p plugins
cp -R examples/plugins/com.example.echo plugins/com.example.echo
```

然后在 `.env` 设置 `PLUGIN_SYSTEM_ENABLED=true`，通过插件 CLI 发现、检查并批准 Manifest 中的权限。插件目录名必须与 `id` 完全一致；Manifest 或权限发生变化后必须重新批准。

完整说明见 [插件开发手册](../../../docs/plugin-development/index.md)。
