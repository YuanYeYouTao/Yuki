# WebUI 状态、收发消息、Provider 选择与 Token 展示

本页记录 2026-09-29 对 Diana 固定提交 `682d652` 的设计比较，以及 Yuki 的实施边界。参考的是 Diana 的交互和统计口径，未复制其源码；Diana 的 Limited Redistribution License 限制修改后代码的公开发布。当前实现位于 `codex/webui-diana-interaction`，合并、部署和真实页面验收需另行核对。

## 对照和决定

| 范围 | Diana 的做法 | Yuki 的对应实现 |
| --- | --- | --- |
| 状态与收发消息 | 事件详情将收到的消息、处理决定、发出的回复、投递和调用链放在一个上下文中；中途 `say` 是真正发出的消息 | 状态卡先显示当前阶段、最近步骤，再列出与该轮轨迹可信关联的收到/发出账本事件；出站消息仍按实际发出时间留在聊天时间线。不能从文本长度推测“进度消息”或“最终答案” |
| Provider 与模型 | 接入配置与按用途模型分配分开；选 Provider/分组后选模型，可有显式后备 | 保留 Yuki 的 `ModelTask → ModelProfile → Provider` 合同。先编辑配置档，再按任务选择已配置的 Provider 和相应模型/Profile。现有合同只有每任务一个 Profile；界面不虚构分组、自动降级或热切换 |
| Token 用量 | 按时间窗口汇总调用，输入、输出、缓存命中和未报用量分别呈现；缓存命中包含在输入 | 从 `model_invocations` 账本按最近 24 小时、7 天、30 天计算服务端汇总，并按 Provider/模型拆分；调用明细保留。当前轮次另按真实 `runtime_turn_id` 汇总。未报告总 Token 的调用单独计数，不当作 0 消耗 |

## 具体数据边界

- “正在执行”只来自进程内实际 Runner span。诊断中有一条未配对的开始记录，不足以声明仍在运行。
- 轮次收到的消息只认同一原会话的入站 `source_event_id`；发出的消息只认 `social_delivery.delivered_event_id` 指向的 Yuki 出站账本事件。跨会话发送显示目标会话。两类事件按内部事件 ID 排序，不能用 QQ 平台消息号反查或推断归属。
- 状态卡最多读取最近 32 个轨迹步骤，因此消息清单可能不完整；截断时显式提示。消息正文最多显示 500 字，只在 `control.chat.content.read` 授权后查询；未授权时仍可显示内部事件编号和方向。轨迹过期后不从聊天内容重建轮次。
- 用量统计覆盖 Yuki 已写入 `model_invocations` 的模型调用。独立 Jev 服务、语音、图像或未接该账本的外部服务不在此数值内。未报 Token 的调用计入次数，数值不等于供应商账单、费用或剩余额度。历史清理后的数据不补造。
- 配置编辑仍使用文件版本检查和原有校验；保存只代表磁盘文件更新，重启后才在新请求中加载。密钥值不从服务器读回。

## 参考

- [Diana 事件视图](https://github.com/SuInk/Diana/blob/682d6522f1b03f66adb2a54f73de924d4b90530f/frontend-next/src/views/EventsView.vue)
- [Diana Provider 配置](https://github.com/SuInk/Diana/blob/682d6522f1b03f66adb2a54f73de924d4b90530f/frontend-next/src/views/LLMView.vue)与[按用途模型分配](https://github.com/SuInk/Diana/blob/682d6522f1b03f66adb2a54f73de924d4b90530f/frontend-next/src/views/AssistantView.vue)
- [Diana 用量说明](https://github.com/SuInk/Diana/blob/682d6522f1b03f66adb2a54f73de924d4b90530f/docs/llm-usage.md)与[许可证](https://github.com/SuInk/Diana/blob/682d6522f1b03f66adb2a54f73de924d4b90530f/LICENSE)

## 验收清单

| 项 | 结果 |
| --- | --- |
| 状态卡呈现本轮可信关联的收到、发出消息及跨会话目标，正文按权限限制 | 本地实现；定向后端和前端测试通过，待真实页面核对 |
| 状态先给当前阶段摘要，详细步骤与原始轨迹按需展开 | 本地实现；前端测试通过，待真实页面核对 |
| Provider 配置档与按用途 Provider/模型选择分开，保存沿用版本核验 | 本地实现；前端测试通过，待真实页面核对 |
| 24 小时、7 天、30 天 Token 汇总、缓存口径、未报总量提示及本轮用量 | 本地实现；聚合测试通过，待真实页面与数据库查询性能核对 |
| 迁移、完整 CI、PR、合并、生产部署和真实 QQ/页面验收 | 待进行；不能从本地测试推断已上线 |
