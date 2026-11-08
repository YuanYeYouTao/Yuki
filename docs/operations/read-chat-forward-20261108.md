# read_chat 恢复读取一层合并转发（2026-11-08）

## 背景

QQ 群里转发的「聊天记录」入账后只有外层 `forward` segment（SnowLuma 经 OneBot v11 反向 WS
只推送 forward id，内层节点要 `get_forward_msg` 才能取得）。生产行 chat_events 135443：
`[{"type":"forward","data":{"id":"NwRC…"}}, {"type":"yuki_context",…}]`，模型只看到
`[含合併轉發]`，无法回答用户关于这份聊天记录的问题。

这个能力在 #269（PR #315，`fd84d025`，2026-10-13）实现过：`read_conversation_history(event_id=…)`
沿外层原事件的接收 Presence 读取第一层。2026-10-16 的 `eb9bd2f7`（#299 控制面重构）在退役
「任意 OneBot action／戳一戳」时把它一并误删；作者确认为误删，要求恢复。

本次不新增工具：现役 `read_chat(around=E)` 的锚点事件 E 带 forward segment 时，结果附带每个外层
forward 的第一层节点。规则沿 #269：一层、不递归、部分失败保留成功组、全失败返回真实顶层错误、
结果不带图片、正文走共同预算／artifact 链。

## 设计

- 能力属于 Provider：`PlatformSocialOperations.read_forward(handle, locator)` 是网关原生操作；
  SnowLuma 声明 `forward_read` 能力并用固定 `get_forward_msg` 实现，把节点投影成中性的
  `ForwardNode`（作者名、时间、正文、原 segments）。Telegram、飞书、App 不声明该能力。
  `read_chat` 不判断平台：它取外层事件的 `ingress_presence_id`，按原 Presence 解析连接，再以
  `require_capability(connection, "forward_read")` 核该连接是否提供此能力。
- 内层图片／视频／文件／语音作为原外层事件下追加的观察附件槽（`conversation_media_items` 的
  `source_snapshot_json` 只保存这一 segment，与「未入账引用附件」同一机制），结果按 read_chat
  现行的 `attachments` 形状给出 `attachment` 序号，模型用 `view_file(event_id=E, attachment=N)`
  按需读取，下载仍沿原接收连接与 24 小时缓存。读取本身不下载、不读取媒体。
- 内层 forward 不展开，只在节点上计数 `nested_forwards`。

## 写代码前列出的可能失败情况

### A. 触发与参数

1. 非 around 的浏览／检索结果也去请求远端转发（列表里每条带 forward 的事件都触发网络调用）。
   只有 around 锚点自身带 forward segment 时才展开；邻文、检索结果不展开。
2. around 锚点不可读（不在获准范围、`around_status=unavailable`）时仍去请求远端：不得请求。
3. 锚点没有 forward segment 时结果出现空 `forwards`：应完全不出现该键。
4. `target.operation_id` 的回执锚点（Yuki 自己的出站消息）不应被当作转发读取。
5. 新增工具名、参数或别名；`around` 与筛选同给的原拒绝（`around_with_filters`）被改变。

### B. 来源与能力

6. 外层原 Presence 缺失（事件没有 `ingress_presence_id`）、已停用或当前未连接（离线／账号已转给
   别人后 Presence 结束）：每组返回 `forward_presence_unavailable`，不换另一个账号、不按当前路由
   或平台消息号代替来源。
7. 连接所属 Provider 没有 `forward_read` 能力（Telegram、飞书、App 或未声明的自定义 Provider）：
   每组 `forward_unsupported`，不探测、不回退。
8. `read_chat` 内出现 `platform == "qq"` 之类的硬编码判断。
9. 外层 segment 没有可用 locator（缺 `data.id` 或空串）：该组 `forward_locator_missing`，不发请求。

### C. 原生调用与响应

10. `get_forward_msg` 返回 `status=failed` 或 `retcode != 0`，或 nonebot 抛出 ActionFailed／网络
    异常：该组 `forward_read_failed`，其他组不受影响。
11. 响应中找不到实际节点列表（各家 OneBot 实现的信封不同：直接 list、`messages`、`message_list`、
    `data`、`data.list|messages|message_list`）：选第一个实际 list；都没有时该组 `forward_read_failed`。
12. 空列表是合法结果：该组成功，`messages: []`。
13. 节点 `message` 为 CQ 字符串、segment 列表，或使用 `content` 字段；缺 `sender`／`time`：按真实
    内容投影，缺失字段省略，不伪造作者或时间。
14. 内联 base64 图片正文进入结果或附件快照：沿入站序列化规则省略。
15. 同一调用内多个 forward segment 指向同一 locator：只请求一次，按位置复用结果；不同 locator 并行。
16. 多个 forward 部分失败：成功组照常返回，失败组带 `error`；全部失败时工具结果为失败
    `{"ok": false, "error": <首个失败组的错误>, …}`，仍带 events 与各组状态。
17. 内层 forward（节点里的 `forward`／`node` segment）被递归读取：不得；只计数 `nested_forwards`。
18. 取消：等待原生调用时取消须原样传播，不吞成组失败，不写附件槽。

### D. 内层媒体

19. 内层媒体被自动下载或自动读取（结果带图片、调用 `get_image`）：不得；只登记元信息。
20. 附件序号与外层事件已有槽位（外层附件、引用观察槽、forward 本身占的序号）冲突。
21. 同一快照重复读取时每次追加新槽：完全相同的 segment 复用原槽位。
22. 写附件槽时外层事件已被隐私清退或不再是 keeper：返回 `history_source_changed`，不写行。
23. 写附件槽在 SQLite 写事务中等待网络或扫描历史：网络在 reader 关闭后完成，writer 只查该事件
    自己的附件行并插入。
24. `view_file(event_id, attachment)` 无法读取内层图片，或改用当前工具账号而不是原接收账号下载。

### E. 授权、隐私与结果链

25. 委托 `read_target_id`、插件后台范围、隐私代次与 generation 失效的拒绝被放宽或收紧：沿 read_chat
    现有读取授权（around 的锚点必须在获准范围内），不新增权限规则。
26. 网络读取期间隐私清退：结果的 `source_event_ids` 与读取前取得的隐私代次照常交给 Host 发布围栏，
    迟到结果不发布旧正文。
27. 结果超出展示预算：沿共同 `ToolResultBudgeter`／artifact 链给出预览与 `read_more`，不另设节点数、
    正文长度或递归上限。
28. 结果暴露平台账号号码、forward locator 或 Presence ID。
29. 已存回执的重放（Work 恢复、段末未交给模型的调用）重新请求 `get_forward_msg`：按原调用键读取
    已存结果，不重派。

### F. 文档与声明

30. 现行文档仍写「默认模型已退役…远端/raw 转发历史展开」：tool-kernel、chat-media-workspace、
    canonical-runtime、platform-adapters 改写为现行规则。
31. `read_chat` 说明未告诉模型 around 会展开转发；声明变化由完整声明哈希自然产生新 revision。
32. OneBot 兼容合同（`yuki gateway doctor`）未列出 `get_forward_msg`。

## 验证

E2E：`tests/integration/test_read_chat_forward_e2e.py`，用真实 SQLite、SnowLuma Provider、
GatewayConnectionRegistry、SocialService、AgentToolService 与 FakeLLMProvider，伪 OneBot 网关
应答 `get_forward_msg`（文字节点、图片节点、内层转发节点、失败）与 `get_image`。
产物写到 `test-results/read-chat-forward/`（gitignored，UUID、写入时间与 artifact 句柄归一，记录
commit 与工作树状态）：

- `main-loop.json`：Main 调 `read_chat(around)` 后再 `view_file` 内层图片（A1、C10、C11、C13、C15–C17、
  D19–D21、D24、E28）。
- `failures.json`：空列表、缺定位、全部失败、原 Presence 离线、Telegram 无能力、外层事件读取期间被清退
  （B6、B7、B9、C12、C16、D22）；取消与不可读锚点在同一测试中断言（A2、C18）。
- `oversized.json`：60×2000 字节点经共同预算给出预览与 `read_more`，`read_result` 续读后续节点（E27）。
- `replay.json`：同一 Work 调用键重放读取原回执，网关零调用（E29）。

本地（macOS）：ruff、`mypy --platform linux src` 通过；`pytest tests/unit tests/integration -m "not native_worker"`
4822 passed、16 failed、22 skipped，16 项均为本机环境相关的既有失败（与 origin/main 对照见 PR 说明）。
没有对真实 SnowLuma 网关或真实模型验收。
