# 个人客户端 API

个人入口使用原 Bot HTTP 服务的 `/client/v1`。operator 与个人设备使用不同凭据，
设备凭据只代表其 Person，不具有管理或设备执行器权限。公开服务沿部署提供 HTTPS。

## 配对与账号绑定

第一台设备可由 operator 执行 `qq-ai-bot-cli admin person create`，再执行
`qq-ai-bot-cli admin client pair --person <id>` 取得绑定码；已有 QQ 用户也可在自己的私聊
发送 `/ai link`。码有效期十分钟，仅存 SHA-256，一次兑换。过期码沿原维护循环清理。

| 请求 | 授权与结果 |
| --- | --- |
| `POST /pair {code, device_label}` | 只接受码，不接受 Authorization；返回独立 `device_id` 与 token |
| `GET /me` | 设备 Bearer；本人的 Person、App 会话、绑定与设备列表 |
| `POST /link-codes {request_id?}` | 设备 Bearer；为本人签发新码，明文仅首次返回 |
| `DELETE /devices/{id}` | 设备 Bearer；撤销本人的指定设备，其他设备继续可用 |

每个 Person 有一个服务器生成账号的 `app` 绑定和一个主会话，多设备共用。
设备 token 只存哈希；码、token 和密钥不写聊天账本、Main、Memory 或命令回执。
配对响应丢失时原码已使用，重新签发码；未领取的设备可从设备列表撤销。
未知设备与他人设备均返回 `not_found`；失效凭据统一返回 `unauthenticated`。

新 QQ 账号可经认证网关私聊发送 `/ai link <code>`，在普通入账和自动建 Person 前兑换。
这条控制命令不进入聊天、Main 或 Memory。已有同一 Person 绑定时消耗码并返回已绑定；
属于其他 Person 时，只转移今后的账号控制，原历史、执行与 App 设备留在原 Person。
operator 的转移与 Person 遗忘命令见[管理 API](admin-api.md)。

## 历史与隐私

迁移 0121 保留旧绑定行和原事实，新列 `history_starts_at` 对迁移前行为空。
此后每条新绑定的起点都等于自身创建时间。转移时旧行标记 `transferred`，不改 Person；
旧行的 `updated_at` 是区间终点，新行的起点是转移时刻。原主动路由若指向旧行则暂停。
账号查询排除 transferred，停用绑定仍阻止自动创建新身份。

平台私聊历史只返回严格晚于起点所在秒的记录；该秒及更早、无法确定时间的记录不返回，计入
`withheld_unattributed`。旧回执锚点失去有效绑定后返回 `binding_unavailable`。
迁移前不设界的绑定保持原读取行为。前任被遗忘后，现任起点仍在，无须保存前任或墓碑。

遗忘按 Person ID 删除作者事件、全部所属私聊、Work、Memory、设备、码和原命令回执。
`/ai forgetme` 只负责从当前认证账号解析一次 Person；operator 可按 Person ID 删除没有
可用账号的原身份。归属有界的账号不参与跨时期账号字符串清理；号码在正文、转写和
segments 中的脱敏只覆盖其拥有区间。账本的服务器创建时间为 `observed_at`，平台提供的
`occurred_at` 不用于判断归属。消息正文在写事务前准备，短事务复核来源元数据并应用。
Memory 重建按来源事件处理提案，保留有界账号的运行选择集，不按运行创建时间推断归属。

这些命令复用原审计与 `control_command_receipts`。SQLite 确认未提交的 busy 返回
`unavailable`；提交确认丢失返回 `unknown`，保留原请求号查询，不创建替代效果。
迁移 0121 提供前向升级；已转移的库不能退回旧账号唯一约束。没有执行生产迁移或部署。

消息、Work、快照、SSE、附件与参考 SDK 随 P6–P11 补充；当前配对不代表这些链路已验收。
