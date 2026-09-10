# Yuki QQ 社交工具、临时工作区与 Python 沙箱任务书

## 基线、目标与交付边界

从 `ab4db69` 建立 `codex/social-workspace-sandbox`，保留记忆修复。
版本保持 3.8.2；当前数据库 head 0051，新增加法迁移 0052，不修改历史迁移。
正常完整 Main Agent 获得固定 QQ 社交工具、跨平台跨会话临时工作区和隔离 Python 执行能力。
不修改人格、不实现 Telegram、不建立永久文件库。后续授权已允许提 PR、本地构建并部署；不自动发布 Release。

依赖：Main Agent → 社交应用服务 → canonical 路由 → Provider；Main Agent → 临时工作区；
Main Agent → 沙箱客户端 → 可信宿主管理器 → 一次性容器 → 产物导回工作区。
服务保持 transport-neutral，未来 WebUI 经 Control Plane 调用；复用现有 Capability Registry。

## 1. QQ 社交合同

固定工具：`find_contacts`、`send_private_message`、`send_group_message`、`poke_person`、
`get_group_members`、`recall_own_message`。普通用户轮不要求超管。
本批全部 14 个社交/工作区/执行工具为部署级常驻，不依赖 request_tools、名称检索或消息意图。
常驻 schema 使用独立前缀席位，不被发现工具的数量预算裁掉；实际 schema 成本继续统计。
网关离线、目录为空或沙箱不可用不隐藏工具，而返回明确不可用状态。权限仍在执行时检查。
任意 `call_onebot_api` 仍限真实超管；禁言、踢人、加好友、设置和凭证不开放。

- Yuki 可自行决定联系；认识的人必须有真实历史私聊或共同群发言，名单出现不算互动。
- 目标启用、现有准入策略允许、主动路由有效；不自动加好友、解暂停、改路由或换号试发。
- 群发送遵守 enabled/autonomous；require_mention 仅管入站，不禁止主动发言。
- 目标使用 canonical ID、事件绑定 mention/reply 或名称，歧义返回候选，不猜人。
- 查询成员仅限当前 Presence 实际可访问的群，分页、有限字段，不输出网关原始资料。
- 撤回核对真实 Yuki 发送记录及原 Presence；新账号不能撤回旧账号的消息。
- 不新增唤醒调度器；插件不能借此获得普通用户身份权限。
- 用户追加要求：全部新工具也注册到现有 AutomationCapabilityRegistry，DSL 与受委托 Agent 都可使用。
  自动化复用同一服务，不模拟普通聊天身份；创建时核验可委托能力，执行时重验有效委托、目标、路由与限频。
  社交能力使用 social.*，文件使用 workspace.*，执行使用 sandbox.* 名称；不改变已有 onebot.* 任务的语义。
  直接 Agent 工具允许 scheduled_automation origin，但只有实际委托清单内能力可以执行，插件 origin 不因此开放。
  自动化幂等键由 run ID + step ID（Agent 内再加真实 call ID）构造，重试不能换 ID 绕过 uncertain。
  任务间文件仍只保留 24 小时；过期返回 artifact_expired，不续期、不复制到永久区。
  同一任务可以导入/生成文件，将 artifact ID 经现有步骤结果引用交给后续发送或执行步骤。
  sandbox.run_python 的 DSL handler 在既有步骤超时/总时限内等待任务结果；超时取消执行、不发布半成品。
  自动化也可使用 get_code_run/cancel_code_run；不得绕开全局并发、容量和任务隔离。
- 附件只接受工作区 artifact ID；不接受宿主路径、任意 URL、原始 segments。
- 两个 Provider 分别实现发送/成员/戳一戳/撤回。能力目录声明不等于实测通过。
- 文件中转目录独立且对网关只读，不挂整个工作区；远程传输未实现则 capability_unavailable。
- 成功发送记入目标 Conversation，关联源轮次与实际 Provider/Presence；不伪造目标入站消息。

专用社交回执保存操作 ID、源轮次/调用 ID、载荷哈希、canonical 目标、Presence、状态、
平台引用与脱敏错误，不保存第二份正文。状态为 prepared/executing/succeeded/failed/uncertain。
执行前持久化 executing；成功的账本与结果回执同事务提交。发出后异常或崩溃为 uncertain，
不盲重发；相同调用重放返回原结果。网络与 DB 不能原子提交，不承诺 exactly-once。

ConfigRegistry 默认限频：发送每目标 3 次/分钟、全局 10 次/分钟；戳一戳每目标 1 次/分钟、
全局 5 次/分钟。canonical 目标计数，不随 Presence 清零；正常最终回复不计入。拒绝带等待时间。

## 2. 临时工作区合同

归 Yuki 全局共享，不按用户/群/平台设置文件 ACL，也不是匿名公网服务或私人保险箱。
跨 Bot 重启保留，最后实际内容修改 24 小时后过期；读取、列举、改名不续期。
启动及每分钟清理；到期禁止新读取，已获得文件的有界任务结束后释放清理。
不自动保留所有附件，不写长期 Memory/embedding，不加入永久备份。

默认总量 512 MiB、单文件 200 MiB、1000 对象；附件入口更严格的限制继续生效。
满额先清过期，再报 workspace_full，不淘汰有效对象。
固定工具：workspace_list/read/write/import_attachment/delete；导入只认事件附件引用。
对象字段：artifact_id、名称、类型、大小、hash、revision、创建/修改/过期时间。
更新/删除要求 expected revision；读取有界，二进制不返回整段 base64。
独立临时文件目录与 manifest 索引，暂存/校验/原子发布；重启恢复处理孤立及缺失文件。
拒绝路径穿越、链接、设备文件及越界路径；不自动整包解压。
模型看不到宿主绝对路径；目录不逐轮塞入 Prompt。文件始终是不可信资料，不是授权。
不得自动导入数据库、密钥、完整私聊或系统提示。

## 3. Python 沙箱合同

gVisor runsc（systrap）＋每任务新容器；不允许退回普通容器或 Bot 子进程执行。
可信宿主管理器使用 Unix socket，Bot/执行容器均不挂 Docker socket。
管理器固定镜像、runtime、挂载、设备和网络；调用方不能指定 Docker action 或宿主路径。
无公网管理端口。runtime 不可用明确 sandbox_unavailable。

工具 run_python(code,input_artifact_ids,timeout_seconds)、get_code_run、cancel_code_run。
同步最多等 5 秒，其后返回 run_id；查询不重执行，同请求 ID 幂等。
Python 3.12，预装标准库/Pillow/openpyxl/pypdf，构建锁依赖。
用户已允许联网：经专用代理访问公网 HTTP/HTTPS，可在任务临时目录运行 pip；
保持离开任务即销毁，不修改基础镜像。拒绝内网、宿主、云元数据和业务网关。
并发 1、队列 4、默认 30 秒/上限 120 秒、256 MiB 内存/no swap、0.5 CPU、64 进程、
128 MiB 可写空间、stdout+stderr 32 KiB、最多 20 个/100 MiB 产物。
非 root、只读根、no-new-privileges、drop capabilities；不挂数据库/密钥/账号/工作区整体。
代码容器仅接内部网络；专用代理负责出站，宿主防火墙独立限制目标与端口。

仅选定对象复制至只读 /inputs（以 artifact_id 为文件名）；/work 临时处理；/work/outputs 导出。
先停止所有执行，再检查普通文件类型/路径/数量/大小并导入工作区。
失败/取消/超时不发布半成品；诊断有界。产物导回后销毁容器和暂存区。
重启终止本系统遗留任务，不自动重跑；任务元数据/代码/诊断保留不超过 24 小时，不进普通日志。
沙箱内存/临时盘随任务结束，工作区跨任务短期保留，二者不能混淆。

## 4. 提交与验收

1. 基础合同与回执迁移。
2. 两 Provider 社交操作、限频、目标账本与 uncertain 恢复。
3. 临时工作区及附件导入、TTL、配额、并发安全。
4. Python 镜像、管理器与执行隔离、恢复。
5. 文件中转、全流程、文档及验收报告。

各领域提交同时接入自动化 registry、严格参数模型、validator、handler 和授权测试，不能仅注册名称不绑定实现。
最终包含定时任务 导入/生成→Python→产物→发送 的步骤引用验收，以及权限撤销、文件过期、重试和 uncertain 场景。

agent_tools.py 仅注册与委派，不堆 Provider/文件/容器实现。
各提交运行目标测试、Ruff、mypy、diff；最终完整 pytest 不超过 800、Memory quality、
迁移/FK、Compose、本地 Docker smoke。不能删除安全断言规避预算。

必须验证普通人自主联系/陌生拒绝/停用与暂停、各 Provider 全部社交动作、正确目标账本、
切号不改 generation、重放不重复发、超时不盲重试；跨会话工作区/不续期/重启/并发更新；
沙箱公网可达/无宿主或密钥访问/无网关访问/无限循环/fork/内存与输出洪泛/路径攻击；
导入→运行→产物→发送端到端。固定工具 schema 不随文件与人名变化。

真实 QQ 测试仅在用户指定联系人/群进行；未提供时标记未实测，不对真实用户试发。
分别报告代码验证、Provider 实测、沙箱隔离、未完成项，不用 success 代替实际交付确认。

## 5. 后续上线条件

按最新授权本地构建传输，备份 DB/config/image，验证 runsc；需要重启 Docker 时安排维护窗口，
不得顺带中断 SnowLuma。至少可用磁盘 3 GiB、内存 512 MiB，并发 1 压测通过。
不满足则沙箱不可用，不降低隔离；社交与工作区可独立运行。

官方依据：[SnowLuma](https://snowluma.github.io/api/extended/index.html)、
[runsc platforms](https://gvisor.dev/docs/user_guide/platforms/)、
[gVisor production](https://gvisor.dev/docs/user_guide/production/)。

## 实施状态

- 基础合同/0052/回执：已提交 147e834。
- Provider 社交、工作区、沙箱及自动化：已分领域提交，普通 Agent 已接入固定注册。
- 本地 800 项测试通过；真实 runsc 隔离检查见 [验收记录](../operations/social-sandbox-validation.md)。
- 实际 QQ 接收者尚未指定，真实 Provider 操作不冒充已通过。
- 已本地构建并部署 Bot；服务器 runsc 公网/隔离压力探针通过，Bot 用户经 socket 执行成功。
- 真实 QQ 操作未执行；线上观察与回滚位置见验收记录，PR #66 尚未合并。
