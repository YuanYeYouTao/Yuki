# Subscription Monitor / 订阅监控

与 GitHub Monitor 一样，这是通过现有 Plugin API 3.0 运行的轮询插件。
它读取 RSS 2、Atom、JSON Feed 1/1.1，为每条订阅保存增量状态，并把新动态和判断条件
交给 Yuki 的统一主 Agent。主 Agent 决定是否通过 `send_message` 通知当前目标。

本插件不直接发送新闻正文或卡片、不调用独立判断模型、不注册动态提示词片段。
`judge_prompt` 是订阅条件，不是替换 Yuki 人格的 system prompt。主 Agent 的工具、
前缀装配、执行权限、来源身份、预算、恢复和出口过滤继续由核心负责。
关键词规则未命中时不唤醒主 Agent；语义条件由主 Agent 判断，未命中可安静结束。

## 安装

复制整个目录到生产挂载的 `plugins/subscription-monitor/`，使用已有管理入口检查、批准并启用：

```text
/ai plugin discover
/ai plugin inspect subscription-monitor
/ai plugin approve subscription-monitor
/ai plugin enable subscription-monitor
```

插件没有额外依赖、Secret 或数据库迁移；XML 解析使用 Yuki 已安装的 `defusedxml`。
批准范围包括后台轮询、插件私有 KV/配置、管理员命令、HTTP 和主 Agent 通知。
由于订阅源由管理员配置，Manifest 使用 `network.http.unrestricted` 访问不同公共域名；
Host 仍校验 DNS、每次重定向、响应大小，拒绝内网和凭据 URL。不要将 Token 放进 feed URL。
插件不包含 X/Twitter 抓取器；推文须由你已有的服务提供上述格式的 feed，不能直接填 X 网页地址。

可选短命令绑定（与已有绑定合并）：

```dotenv
PLUGIN_DIRECT_COMMAND_BINDINGS={"/monitor":"subscription-monitor:monitor"}
```

未设置短命令也可使用 `/ai plugin run subscription-monitor monitor ...`。
命令和通知目标授权与 GitHub 插件一样要求真实超级管理员上下文。

## 使用

```text
/ai plugin run subscription-monitor monitor add model-news https://example.com/updates.xml group:123456 只通知已正式发布的新模型，说明模型名称和发布日期
/ai plugin run subscription-monitor monitor list
/ai plugin run subscription-monitor monitor show model-news
/ai plugin run subscription-monitor monitor status
/ai plugin run subscription-monitor monitor pause model-news
/ai plugin run subscription-monitor monitor resume model-news
/ai plugin run subscription-monitor monitor remove model-news
```

`add` 默认每 300 秒检查该源，首次只建立基线，之后处理新条目。无新条目时不调用模型。
目标可使用 `group:群号` 或 `private:QQ号`。命令返回配置回执；运行中的监控通知只通过主 Agent。

复杂配置用 `set` 传完整订阅 JSON，或先 `show` 读取后编辑。以下是单条订阅示例：

```json
{
  "id": "quota-reset",
  "name": "额度重置公告",
  "url": "https://example.com/feed.json",
  "enabled": true,
  "targets": [{"target_type": "group", "target_id": "123456"}],
  "interval_seconds": 300,
  "initial_sync": "baseline",
  "include_any": ["reset", "重置"],
  "exclude_any": ["rumor", "传闻"],
  "judge_template": "reset_time",
  "judge_prompt": ""
}
```

把 JSON 压成一行接在 `/ai plugin run subscription-monitor monitor set ` 后即可。
`set` 会完整替换该 ID 的配置，省略字段恢复默认；不接受任意模型名、Provider 或 SDK 私有参数。

| 字段 | 含义 |
| --- | --- |
| `include_any` | 标题、正文、作者或标签任一位置包含任一关键词才接纳；空数组表示不限 |
| `exclude_any` | 出现任一排除关键词即跳过；优先于 include，均不区分大小写 |
| `judge_template` | `any_update`：一般动态；`release`：正式发布；`reset_time`：明确的重置时间 |
| `judge_prompt` | 自定义自然语言条件，非空时替代模板，最多 700 字符；交给主 Agent 判断和摘录字段 |
| `targets` | 最多 4 个已由管理员授权的目标 |
| `initial_sync` | 默认 `baseline`；显式 `replay_recent` 可处理首次看到的最近条目 |
| `replay_recent_limit` | 首次重放数量，默认 3，最多 20；修改该值不会重跑既有基线 |

规则过滤只是可选的关键词预筛；语义模板不保证模型判断绝对准确。
时间等字段来自原文，条件会要求主 Agent 在时区缺失时说明未知。

## 处理与恢复

源由 `id + url` 绑定；更换 URL 使用新 ID。相同 feed 条目 ID 在当前订阅中只接纳一次，
同 ID 的正文编辑不作为新动态。首次基线、被规则过滤和已排队的 ID 一起持久化。
最近 2,000 个 ID 用于快速查重，额外持久化小型已观察标记；离开缓存窗口的旧 ID
重新出现时也不会重复发布。标记写入前，条目和待提交请求已一起保存，重启可继续完成。
这些标记计入插件 100 MiB 存储额度；额度耗尽会明确报错并保留当前批次，不淘汰标记冒险重发。

一次 HTTP 响应最多 1 MiB / 200 条，超出整次失败，不静默截掉条目。正文最多保留 6,000 字符，
通知 payload 另作有界投影。插件按一次已抓取的有限快照处理，不跨页回填离线期间已从 feed
消失的历史；监控可靠性也取决于来源保留多久的动态。

Host 当前的主 Agent 唤醒输入只包含最多 1,200 字符的摘要和 1,000 字符的 intent，
不自动展开任意 payload。本插件将标题、链接、正文节选放入摘要，将条件放入 intent。
正文被截短时明确标记，主 Agent 可按需读取原文，证据不足时不通知。

默认后台扫描间隔 30 秒，每条订阅每轮最多向 Host 提交 10 个通知请求。待处理批次先排空，
之后才抓取新快照，HTTP 支持 ETag / Last-Modified / 304。轮询使用 Host 托管服务，
不实现另一套用户提醒调度器。

发布前将完整请求持久化；Host 已接纳但插件未收到回执时，重启后提交相同 `event_key` 和
字节等价参数，复用 Host 原事件/回执。修改条件不改写已经准备的请求；被移除目标的尚未
提交请求会跳过。暂停停止新的轮询和接纳；移除清除插件尚未提交的请求，保留去重游标。
Host 已接纳的主 Agent 轮次仍由原 job/event 和现有授权继续管理，不伪装成已撤回。

`status` 展示基线、待接纳数、累计接纳/规则过滤、上次成功与错误类型，以及 Host 队列统计。
“已接纳”只证明 Host 收到了事件；是否真正发信以主 Agent 的 `send_message` 回执为准。
普通管理命令回执不属于订阅动态的自主通知。

## 验证

```sh
uv run pytest -q plugins/subscription-monitor/tests
uv run ruff check plugins/subscription-monitor
uv run mypy --follow-imports=silent plugins/subscription-monitor/subscription_monitor
uv run qq-ai-bot-cli plugin test plugins/subscription-monitor
```

测试使用 SDK Fake 和真实插件生命周期契约，不连接外网、真实模型或 QQ。

## English

Subscription Monitor polls public RSS, Atom and JSON Feed sources using the existing Yuki SDK.
Optional keyword filters run locally. Matching events and per-subscription conditions wake the
normal Main Agent, which owns all visible notifications through `send_message`. There is no
separate judge model or direct delivery path. The first fetch creates a silent baseline by default;
pending requests survive restart and reuse Host notification idempotency. Copy the plugin directory,
approve its manifest, then manage subscriptions with the `monitor` administrator command.
