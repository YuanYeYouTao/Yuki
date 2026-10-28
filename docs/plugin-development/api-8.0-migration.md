# Plugin API 8.0 自动化合同迁移

> 本页保留 8.0 的历史变更；当前合同见 [Plugin API 9.0](api-9.0-migration.md)。

8.0 是公开 TaskSpec 的破坏性变化。Host 仅加载同 major 且 minor 不高于自身的插件；旧 7.x 声明需先适配实际调用，再将 `plugin_api` 改为 `8.0`，重新发现并按真实 manifest 批准。
产品版本、Plugin API 与持久定义格式分别演进，不保留旧创建执行器。

- 自动化 task 必填 goal/trigger，name 可选。把原 constraints 完整合入 goal；收件人、渠道和明确静默等真实要求写入 goal。
- 删除 version、delivery 及 task 级 timezone。once.local_datetime 必须带 UTC offset；daily/weekly 的唯一 timezone 在缺省时采用共同默认并固化。
- create_task 保留稳定 request_id，新增可选 max_runs；update_task 的 max_runs 省略保留、null 解除、整数修改。name 省略保留、null 清除标签；不从私有 goal 派生显示名。
- Host 与 SDK Fake 都返回 data.automation；task_id 是整数型字符串。owner 列表保持本人 active/paused 范围，模型的全局目录不授予插件私有读取。

没有调用自动化的插件也须审阅真实接口/manifest后更新 major 声明；不能把更改版本号当作已完成适配。7.1 增加的历史检索仍沿原功能合同，详见[历史迁移](api-7.1-migration.md)。

原版本、已接纳 run/Work、已付调用和 Social 回执继续认原 ID/参数/hash。升级声明不重跑原调用，不将旧未知当作失败重发。数据库的冻结迁移只发布未来定义，旧原件保持历史读法。

完整接口与示例见[定时安排](automation.md)。
