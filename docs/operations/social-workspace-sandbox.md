# Social tools, scratch workspace and Python jobs

The normal Main Agent has fourteen deployment-stable tools: six QQ social operations,
five workspace operations, and three Python job operations. Tool schemas do not change
with contact names, directory contents or gateway availability. Execution remains
subject to canonical target policies and the actual delegated capability grant.

## Social operations

`find_contacts` resolves names or canonical IDs. Only people with actual inbound
interaction qualify; a group member-list entry alone is insufficient. Proactive sending needs
an existing enabled target and unpaused active route. Group sends also require
`autonomous_enabled`. These tools do not change account, repair routes or add friends.
NapCat and SnowLuma use the same explicit social-operation interface.

Replies to the current private sender (text, image or file) instead use the proven
inbound Presence. The adapter supplies the event reference, never model arguments;
the ledger must match the conversation, private human sender and ingress Presence.
This does not unpause the proactive route. A reconnection may use only the same
Presence; there is no fallback to another account. Other targets and automation
still require their active route. Target enablement, idempotency and tool rate
limits apply to both paths, with a final connection check before execution.
Use `subject_ref=current_speaker` for the current sender; `target_id` is a canonical
UUID, not a QQ number. Attachment IDs require `attachment_kind=image|file`.
Validation/route failures do not delete already generated workspace artifacts.
Failed core platform sends remain tool evidence for the normal final answer, rather
than replacing it with an admin-command error sentence. Remaining tool execution
is closed for that turn (including uncertain delivery); no automatic resend is added.
Administrative mutations retain their existing terminal-receipt behavior.
The same non-terminal failure policy covers social poke and own-message recall;
it does not weaken administrative mutations. A failed poke cannot replace the
normal answer with a generic admin failure sentence.

`poke_person` defaults to the backend's current canonical Space in group turns,
and to private delivery outside a group. Set `scene=private` to explicitly poke
privately; it cannot be combined with `space_id`. An explicit `space_id` is a
canonical UUID, never a QQ group number. Group pokes use the Space route and a
live member probe, not the person's private route. Paused routes remain paused;
there is no automatic fallback between private and group scenes.

The dedicated `social-transfer` host directory must be owned by Bot UID 10001,
mode 0755. Bot mounts it read-write at `/app/social-transfer`; providers mount the
same directory read-only at `/yuki-transfer`. Do not place the Bot's transfer path
under a gateway login directory whose parent cannot be traversed by the Bot UID.
Adding this mount to an existing provider container requires a planned recreation;
preserve every existing login/config mount. Do not widen login-directory permissions.
Files are published mode 0644 on the read-only gateway mount and removed after use.
SnowLuma copies the source mode into its private staging directory, then reopens
the copy read-write to sync it. Mode 0444 breaks that flow; mount-level read-only
protection still prevents gateway changes to the original transfer file.
Preparation errors report `artifact_transfer_unavailable` before contacting the
gateway; cleanup errors are logged without overwriting a confirmed send result.

`send_private_message` and `send_group_message` accept text or workspace artifacts.
Images may include text. File uploads may include a caption: upload first, then send
the text only after confirmed upload success, with separate durable receipts and
separate target-ledger events. The combined tool result reports both stages; caption
failure never erases file success. Replays return recorded outcomes without resending
either stage, including after source expiry. A crash between stages can leave the
caption unsent. Both stages count as one rate-limited tool operation, retain the same
Presence, and recheck the route before dispatch. These network effects are not atomic.
File APIs may return no retractable message ID; such uploads are
recorded in the target ledger but `recall_own_message` cannot invent a recall handle.
Recall always uses the original Presence. Poke, members and recall have dedicated
methods; no arbitrary OneBot action is available through these tools.

Receipts use source turn/call identity and a payload hash. `uncertain` means the
gateway may have acted: do not automatically resend. Database persistence cannot be
atomic with a network operation. Rate limits are global ConfigRegistry settings:
send 3/target/minute and 10/global/minute; poke 1/target/minute and 5/global/minute.
Ordinary final chat replies do not count against these new proactive-operation limits.

## Shared temporary storage

`workspace/` is independent of production databases and backups. All Yuki sessions
share it; there is **no cross-session confidentiality guarantee**. Never put secrets,
database exports, system prompts or bulk private history there automatically.
Objects expire 24 hours after an actual content change. Reading, listing or renaming
does not extend expiry. Updates/deletes require the returned revision.

Limits: 512 MiB total, 200 MiB per object, 1,000 objects. Attachment import also obeys
the stricter media-download limit. Binary reads return metadata, not base64.
An automation can import a recorded inbound attachment using `event_id` plus
`attachment_index`; the event must belong to its bound Conversation. No fresh event,
expired upstream attachment, missing stored reference or wrong Conversation produces
a clear failure rather than a fabricated user event. Importing once does not preserve
the artifact forever for a future schedule.

The gateway sees only a short-lived file snapshot in `social-transfer/`, mounted
read-only as `/yuki-transfer`. It never receives the workspace directory. Remote
gateways without this mount must leave `SOCIAL_GATEWAY_TRANSFER_DIRECTORY` empty.

## Automation

Capabilities are registered as `social.<tool>`, `workspace.<operation>` and
`sandbox.<tool>`. Scripts may pass saved result fields to later steps. Existing
automation delegation and permission revalidation still apply; registration does not
upgrade old grants. Non-admin delegated social writes remain scoped to the bound
canonical target, and cannot select another group through poke parameters. A scheduled
Agent call gets its own invocation identity, not the enclosing DSL step's identity.
Sandbox jobs can outlive the five-second synchronous wait: query `get_code_run` and
use only successful returned artifacts. Polling never reruns code.

## Host execution manager (Linux only)

Build `deploy/sandbox/Dockerfile` locally for `linux/amd64`, transfer the image, and
load it as `yuki-python:3.8.2`. Install verified gVisor binaries following the official
[installation guide](https://gvisor.dev/docs/user_guide/install/). Do not fall back to
runc. Runtime registration can use Docker reload where supported; if a daemon restart
is required, arrange a maintenance window instead of disrupting SnowLuma.

Install the package source under `/opt/yuki-sandbox/src` (the manager uses only
Python 3.12 standard-library dependencies), and install the systemd unit in
`deploy/sandbox/` using `install-manager.sh` (including host group 10001). The host manager runs as
root with group 10001 and controls only its labelled containers. Neither Bot nor job
container receives the Docker socket. The Unix socket is group-restricted, not public.

Start the dedicated egress Compose stack and apply `network-guard.sh` before enabling
the manager. Check that subnet 172.30.251.0/24 does not overlap existing networks.
The internal bridge allows code only to the proxy; the proxy allows public ports
80/443. Host INPUT and DOCKER-USER rules independently block host/private destinations.
IPv6 is not enabled. Do not remove these rules while executing jobs. See
[Squid access controls](https://wiki.squid-cache.org/SquidFaq/SquidAcl).

The guard loads `br_netfilter` if absent and requires bridge filtering to be enabled.
It fails closed when the host lacks this prerequisite. The two fixed public DNS
resolver addresses may receive UDP/53 from the proxy, not from Python job containers.

Each task uses a new runsc container, UID 65532, read-only root, no capabilities or
privilege escalation, 256 MiB with no extra swap, 0.5 CPU, 64 processes and 128 MiB
host tmpfs. Selected inputs appear under `/inputs/<artifact_id>`; code writes outputs
to `/work/outputs`. Pip installs are temporary under `/work/packages`. Network access
is not permission to upload private conversation data or credentials.

Jobs run serially with four waiting slots. Timeout is 30 seconds by default, maximum
120. Combined stdout/stderr is limited to 32 KiB; output publication allows twenty
regular files, 100 MiB total. Stop the container before validating outputs. Failed,
cancelled or timed-out jobs publish nothing. The complete validated set is published
atomically in the workspace index. Job code/diagnostics expire within 24 hours.

Before production activation, require 3 GiB free disk, 512 MiB available RAM, and
actual runsc resource/network/path-attack tests. A healthy manager is not proof of
isolation. When prerequisites fail, keep the tools visible but return unavailable.

## Deployment and rollback

### Social account and group resolution

Recall resolves the original outbound ledger event's enabled Presence and rechecks
that exact connection before claiming the operation. Pausing, deleting or switching
the active send route does not prevent recall; another account cannot substitute.
Member reads use live group membership and active QQ group bindings independently
of outbound routes. Multiple group bindings require `space_binding_id`; read failure
may try another accessible connection to that same group without changing any pin.

`find_contacts` includes active QQ binding IDs. Group pokes and mentions resolve an
explicit `binding_id`, or the account behind an inbound `subject_ref`, or a unique
active binding. Ambiguous bindings are rejected. Private pokes use the binding of
the resolved private route and reject conflicting selectors. Ordinary delegated
group pokes may target known members only inside the automation's bound group;
private and other-group overrides remain forbidden.

`send_group_message.mentions` is an array of person selectors with optional
`binding_id`. Each member is verified against the selected group, then serialized
as a real OneBot `at` segment before the text. Plain `@name` text does not notify.
Text, image and separately receipted file captions preserve these segments in the
ledger. No `@all` or arbitrary raw QQ selector is exposed. Schemas remain identical
between chat and delegated automation within this deployment.

中文：撤回绑定原消息 Presence；成员查询依据真实群连接，不受主动发送路由暂停影响。
多 QQ Binding 有歧义时拒绝，须明确选择；当前事件引用保留具体 QQ 账号。
普通群自动化只能在绑定群内戳已认识且确认在群内的人，不能改为私聊或跨群。
群消息的 `mentions` 生成真实 `at` 段，图片及文件说明均保留结构化 @ 和独立回执。

Schema 0052 adds only social operation receipts. Back up the database, configuration
and current image before applying it. Build locally, replace only Bot and observe;
do not restart SnowLuma or silently erase unknown backups. Disabling the manager
leaves social/workspace functionality independent. An old binary may reject 0052:
rehearse the rollback using the pre-upgrade snapshot; never overwrite newer messages
with an old database without assessing data loss.

## 中文摘要

十四个工具常驻，不因目录/联系人变化改变 schema；自动化仍需真实委托权限。
工作区属于 Yuki，全会话共享，不是私人保险箱，内容修改后 24 小时过期。
发送只走现有 canonical 路由，超时不确定不重发；文件中转目录与工作区分开。
沙箱允许公网 HTTP/HTTPS 与临时 pip，内网、宿主、网关和密钥不可达；必须使用
runsc，不能降级普通容器。只有实际隔离验收通过后才启用，构建始终在本地完成。
