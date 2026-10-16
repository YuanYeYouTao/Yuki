# Social delivery and persistent workspace integration

All Yuki Main Agent entrypoints share a sorted function-tool manifest, frozen after
plugin startup and before background turns for the running deployment. Normal/private/group turns, plugin wakeups,
plugin generation and scheduled turns use the same
schemas for their model requests. Core and installed plugin definitions are collected without an event identity;
work control and short-state definitions are added by the same Main Agent contract. Execution still
checks the real origin, target, current permission and delegated grant. Scheduled Main Agent calls use the ordinary social/workspace/sandbox tool names.
Direct mode declares the complete frozen deployment manifest; authority is checked at execution. Optional Code Mode exposes its fixed direct tools and invokes the remaining tools through Code Mode; execution checks current authority and availability.
Restart Bot after changing installed tool definitions to start a new manifest.

## Global short-term state

`update_short_state(slot, text)` stores up to three small records
in a separate `short_state` table in `WORKSPACE_DIRECTORY/manifest.sqlite3`. Records
have no person/group partition or access control. They survive Bot restarts, expire
24 hours after a write, and are separate from artifact files and long-term memory.
Reads do not extend expiry. Empty text clears the slot; a write returns that slot's actual content.

Each record is limited to 300 characters; there is no additional 512-byte envelope gate. Oversized record writes are rejected atomically.
A turn loads one snapshot after history, in the current input envelope immediately
before `runtime.time` where present. Empty state adds nothing. Tool results carry
subsequent writes; the turn's initial snapshot never changes inside its tool loop.
Records are untrusted data and cannot grant platform permissions. Yuki must persist
a temporary decision, such as a number to recall in another conversation, before
claiming it has been remembered. A successful write is necessary; a statement alone
is not persistence.

Responses recovery inputs follow the original paired function outputs at the request
tail when protocol continuation remains valid. Work business resumption uses current
authorized chat and original receipts; it does not restore the whole old group history.
Private provider state stays on its original valid chain until an explicit boundary,
compression or cache eviction; see the [development contract](../architecture/development-contract.md)
and [persistent workers](../architecture/persistent-subagents.md).

## Social operations

Common model tools use internal Person, Space, Binding, Presence, Conversation and
event IDs. find_contacts returns real bindings with their platform; it does not
change routes, grant authority or turn observed members into known contacts.

Replies use the original Conversation entry and actual Presence. Explicit delivery
to another entry resolves its real Binding; proactive sends use the actual target
route. Source, ownership, permission and connection checks remain at execution.
Internal Work, child execution and automation do not require an online gateway or
an unchanged connection generation, a unique human platform account or an active
original human Binding before model, memory or workspace work. Current Person
authority follows explicit human merges; original sources, request proofs and root
budgets remain unchanged. Account transfer does not revoke the original human's
accepted internal Work. Actual Person revocation and new transport qualification
remain effective.

Social owns original operation IDs, budget reservations, claim, ledger and durable
receipts. A GatewayProvider's native adapter performs protocol encoding, media
staging and result parsing. Its pure plan_send lays out actual native steps; each
step receives its own Social receipt. An already claimed or uncertain step is not
resent. A never-claimed PREPARED plan may be laid out for current protocol limits,
retaining original IDs, Work and cumulative budgets.

App sends file and text atomically. QQ upload and caption have separate receipts.
Telegram obeys its actual text/caption limits and sends each planned step once.
Confirmed file success survives caption failure. Replays project original receipts
and confirmed ledger facts, including before the final tool result was persisted.
Native message references remain in transport/receipts; public tool results use
internal operation/event IDs. Recall uses the original Presence and message locator.

An original receipt is read against its original call parameters and committed
transport facts before today's media TTL, conversation reset floor or route state.
SDK image replay verifies the immutable snapshot's original plugin handle without
reopening expired media. Fresh attachment use still requires available media.
Explicit forgetting may remove content and its Conversation while a send is in
flight. A real transport confirmation settles its original content-free success
receipt with no event link when necessary; it never restores forgotten content or
sends again. An unconfirmed operation remains unknown.

The public history tool reads authorized canonical ledger events, even while the
platform is offline. Explicit Conversation IDs disambiguate multiple entries.
The member directory returns observed people with coverage=observed. Neither tool
implicitly fetches remote history or a complete platform member list. Default model
tools no longer expose arbitrary OneBot actions, pokes or remote forward expansion.
An independently approved QQ SDK transport extension retains its explicit boundary.

QQ file delivery alone uses the dedicated social-transfer mount: Bot writes it,
SnowLuma reads the matching directory, and staged files use mode 0644. Preserve
gateway login/config mounts and do not place transfer files behind inaccessible
login-directory parents. Telegram uses native multipart bytes; App uses its local
artifact path. No platform-specific file handle is reused on another platform.

Emoji lookup and usage take the actual internal recipient Person or destination
Space. Actorless plugin group deliveries do not invent a human speaker. Current
authorization and source checks remain independent of media preparation.

See [platform adapters and upgrades](../architecture/platform-adapters.md) for
entry isolation, protocol boundaries, SDK 4.0 and the migration chain.

## Shared persistent storage

The current deployment uses the [persistent environment](persistent-environment.md).
`/home/yuki` and `/workspace` persist globally across conversations, Bot restarts and
container recreation. Artifacts no longer expire after 24 hours. The private artifact
index remains separate from the writable home, and existing artifact IDs still read/send.

Path writes use actual content versions; published artifacts are immutable. Gateway
transfer still uses only selected snapshots in `social-transfer/`, mounted read-only as
`/yuki-transfer`. Attachment import still requires a real current/replied event, or an
inbound ledger event belonging to the delegated Conversation. The global home does not
change social send permissions or automatically expose files in model prompts.

## Automation

Each due occurrence activates the shared Main Agent with its complete task requirements
and original run identity. It uses the same fixed tools and current owner authority as
ordinary chat. Accepted Work uses the existing scheduler, recovery and original receipts.
Sandbox jobs can outlive the five-second synchronous wait: query `get_code_run` and
use only successful returned artifacts. Polling never reruns code.

## Host execution manager (Linux only)

See [persistent environment deployment and recovery](persistent-environment.md) for
current limits, the pinned OpenSandbox execd, persistent home, package checkpoints,
PTY sessions, services, and resource/network acceptance. `PersistentManager` is the
sole execution owner. New `run_python` submissions and the ephemeral `Manager`
engine are retired; original terminal job IDs, historical results and completion
acknowledgements remain readable.

## Deployment and rollback

### Social account and group resolution

Recall follows the confirmed event and original Presence. Pausing or switching an
unrelated proactive route does not substitute another account for that operation.
Public member/history reads use observed canonical data, without live connection
probes. Actual mentions resolve a real same-platform Binding and check membership
through its native adapter before sending; plain @name text is not a notification.

Private Conversations use Binding + Presence; group Conversations use SpaceBinding
+ topic. Linked Person facts may be shared, while histories stay entry-local.
Scheduled Main calls retain their original creator, Conversation and source.
The schema is fixed for the deployment and authority is checked at execution.

中文：公共历史和成员目录读取已观察的内部账本；真实发送、引用及撤回由原生适配器完成。
人物使用内部 Person，群使用内部 Space，私聊与 topic 按真实入口隔离。
未知发送不重发；原 Work、预算、已确认文件及部分失败回执继续保留。

Social receipts originated in migration 0052; current deployments must apply the
complete migration chain required by the running binary. Follow the
[versioned release procedure](versioned-docker-release.md): take a consistent backup,
build locally and update the required components without restarting the QQ gateway.
Code rollback must preserve newer files, messages and execution receipts. Restoring
an older database is a separate recovery operation with explicit data-loss assessment.

## 中文摘要

完整工具声明在部署时固定，不因目录/联系人变化改变；自动化仍需真实委托权限。
工作区属于 Yuki，全会话共享并持久保存；路径文件与终端实时共享，发布快照不可变。
发送只走现有 canonical 路由，超时不确定不重发；文件中转目录与工作区分开。
沙箱允许公网 HTTP/HTTPS，pip/npm 依赖长期保留；宿主、网关和密钥不挂载，必须使用
runsc，不能降级普通容器。只有实际隔离验收通过后才启用，构建始终在本地完成。
