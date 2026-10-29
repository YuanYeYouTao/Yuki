# 本机完整操作接入实施记录 · 2026-10-29

本记录对应[原任务书](../architecture/Yuki-桌面端本机完整操作统一接入任务书-2026-10-29.md)。任务书保留调研时的基线；下面按实际实现、验证和交付分别更新。本地实现与独立复审已完成，已提交[PR #533](https://github.com/YuanYeYouTao/Yuki/pull/533)；合并、安装和上线另认下文实际记录。

## 实现选择

沿原 Connector、同一 WSS、同一 journal 和原主 Agent 工具接入，没有新建 Main、Work、任务数据库、守护服务或逐命令审批。普通本机 Shell 不需要 Agent instance、项目目录或 Work。完整访问是本机配置中的一个默认关闭开关，按实际系统账号执行；云端不能替用户打开。

初次 exec/environment_status 用可选 device_id 选择电脑，后续 read/control/恢复认原 run 和原来源。本机目标存在原 SandboxTaskRunModel.source_json；原生 Agent 仍认原 operation/instance，旧服务器终端记录仍属于 Manager。原目标查不到就保未知，不去另一个执行器试一次。主库 migration head 保持0155，没有数据库迁移；Connector 复用既有 operations/events schema。

文件传输共用设备连接，以原文件句柄、版本和内容hash确认实际字节。私有观察进入原 tool_handle，发送成果才沿原 artifact/source_key 冻结。附件先核原内部event/index，再查原写入回执；不因为源缓存过期就否定已完成保存。内部64KiB分片不会进入模型文本。

用户在实施中明确收缩浏览器边界：本机Shell已经能直接运行OpenCLI等程序，截图和文件只需共用传输管线。已撤掉本期新增的browser.py/browser_actions.py、浏览器专属journal/终端字节范围/环境变量、CLI入口、profile发现、打包与专属测试；原Linux yuki_computer源恢复到既有实现。Connector此项减少848行（生产/打包约562、专项测试251、文档35）；这是撤回候选的减量，不是整个任务diff净减少。第三方工具安装、扩展授权与会话由原工具负责，Yuki不写adapter、不内置OpenCLI/Node。

协议4、Connector0.4.0、Desktop1.0.0-beta.8配套；Main固定工具合同34，Client API仍为兼容扩展的revision7。工具名称没有增加，只给原终端/文件入口增加必要目标字段及查询语义。Desktop的OpenAPI、生成类型、hash和最终精确backend commit必须一起交付。

## 任务对应

| 项目 | 实现与证据 | 当前边界 |
| --- | --- | --- |
| T01 | 无instance真实Connector/TLS/本机Shell与文件原回执合同 | 浏览器候选专项已按用户决定撤回 |
| T02 | 原Config/CLI/桌面开关；旧配置缺字段默认false，保设备、token与Agent grant | 关闭不撤销既有脚本OS权限，取消与整设备停止各用原入口 |
| T03 | WSS资源分派；原request幂等；本机completion事务后ACK | 丢失确认不重新派新命令 |
| T04 | PowerShell/POSIX Shell、stdin、字节日志游标、定向Job/进程组取消 | 无完整PTY/ConPTY；各OS实际结果以对应测试/CI为准 |
| T05 | 原SandboxTask/continuation；Main、Work及清理回原目标 | 本机完成只输入原Work，不自行发聊天消息 |
| T06 | inspect真实PNG；send原快照恢复；附件授权原字节与传输回执 | 不为观察图建立永久公开快照 |
| T07 | 通用Shell直接运行已有CLI，截图/下载/上传源文件共用文件管线；自研浏览器专项全部撤回 | 不内置OpenCLI/浏览器CLI，不为它另加daemon、RPC或网页状态 |
| T08 | 这台电脑入口；无Agent连接；原终端状态/增量输出/停止；旧实例历史与结构化回执 | 实际用户界面验收与自动化UI测试分别记录 |
| T09 | Codex/Claude/ACP原生查询/问题/恢复定向回归，原文件与Social回归 | 本轮无未授权付费模型或QQ测试消息 |
| T10 | 协议/版本/合同/资源准备；精确pin和五目标构建沿原发布流程 | PR、CI、包、安装尚未记录为完成 |
| T11 | 原模块合同、操作说明、README、工具schema与清退重复CDP动作 | 历史研究报告保当时基线 |
| T12 | 三名同级子代理分工实现并交叉复审；实际复现问题随修复回归 | 最后复审与交付结果继续写本记录 |
| T13 | 原active wait保决定；真实新wait替换；原兑现信号与结算同writer核事实 | 受控缺陷已复现，不宣称历史现场唯一根因 |
| T14 | 原生近期页/原游标/旧execution；Claude近期窗口；ACP缺历史明示；Work原获准近步 | Claude SDK本地全解析成本仍存在，不冒充常数IO或跨源瞬时快照 |

## 已有本地证据

- 已撤回的浏览器候选曾完成Windows已安装Chrome真实闭环：隔离profile保预先打开的target与登录cookie；snapshot、中文输入、PNG、file-input和loopback站点收到的真实字节、下载实际guid文件。丢页面返回后原control查询三次，页面副作用仍一次；取消CLI保原Chrome/tab。未触碰用户日常Chrome。该候选实现与专项测试随后撤回，不作为现交付功能。
- 真实TLS WSS与HTTP：tests/integration/test_local_device_transport.py两项通过。无Agent实例运行真实进程、增量UTF8游标、completion入库/ACK、目录/详情、停止原控制及真实文件字节均贯通。
- tests/unit/test_local_device_bridge.py与test_local_device_workspace.py初轮共12项通过，新增源文件变化/并发身份/未知写入后已扩充，覆盖普通无Work接入、原目标、不可变完成、未知控制查证、其他Person拒绝、原文件版本释放、显式拒绝终态和私有媒体/发送。
- tests/unit/test_local_device_supervision_review.py通过：原本机run→原Work owned_run wait→原continuation→同一Work queued；重复完成不重复输入或重派。
- T13受控before：正常waiting结算丢accepted_control；合法新条件被旧wait冲突拒绝；原顺序Mainloop提前completed；writer覆盖已经兑现的原wait信号。相同fixture after均通过，普通合法空final/无send完成保留。
- T13/T14相关主组92项、原设备/监督组45项、原HTTP/WSS及Workspace桥26项、来源/私有结果/恢复/原生终审44项、schema/security/查询组28项通过。这些组有重叠，不相加冒充独立总数。
- backend Ruff通过；mypy --platform linux以独立cache检查690源码通过；Windows实际wheel构建通过，共享CDP抽取及其wheel/Docker接线已撤回，最终产物须核无浏览器模块。
- Desktop源码typecheck/lint/build通过；相关单测75项、Connector UI18项、设置4项、发布/资源管线24项通过，跳过项沿原平台条件保留。Workspace/Social新增6项及相关回归共56项通过。

## 复审修正

1. 原终端父进程先退出时仍需取消原Job/进程组内后代；不扫同名进程。增量UTF8不消费尚未完整的字符字节。
2. 原控制request_id不被原exec回执字段覆盖，查证继续指同一控制；未知原run不默认落Manager。已确认完成在离线时保完成事实，单列输出不可读。
3. 原文件保存回执核journal原命令目标，不把规范后的路径当作用户换目标。文件释放失败不能抹掉已经确认的发送/观察事实。
4. 已撤回浏览器子CLI及专属回执，不留另一套browser数据库、命令解析或打包残件。
5. 进度短投影保真实行动/结果与读取范围，长用户原请求不遮盖最近动作；完整原件仍按原权限和Artifact预算读取。

## 交付与验收

截至本次源码记录：后端PR #533已提交并推送，e099c06的11项CI全部通过；最终读取写入删减另认新HEAD的CI。Desktop仍待固定后端合并提交，Release、安装与部署尚未完成。V01–V32按上述对应路径继续核对；OS睡眠/重启、外部工具自行安装与浏览器授权、五目标产物与线上自然流量不由隔离fixture代替。不得把静态代码、打包管线存在或单OS通过写成全部平台验收。


本机闲置前后原样本见[采样记录](local-device-idle-2026-10-29.md)。实际数据仅覆盖所列机器、解释器、短暂闲置和应用payload，不代表线上负载或资源SLA。


### 源码冻结前终审

Connector最终包验证133 passed/1 skipped（Windows打开文件不能rename的真实平台差异），Ruff/Mypy及干净环境wheel安装通过；wheel无browser模块或额外入口；CI平台边界修正后须按最终提交重建，旧候选包哈希不作发布标识。终端输出保两段各4MiB，原byte游标/output_lost/旧日志回读有真实大输出证据；这不是进程完成门槛。

已补原startedAt/completedAt透传、真实work_control capture→私有Artifact回读、合法长原执行键的cursor续页、显式空device_id不改到服务器。预分配run ID与首次任务记录在同一writer提交，去掉prepare/bind分开导致目录暂时出现空run的窗口。实际本机PNG走core→私有media tool_handle后，源删除仍可读原像素；其他Person及换代后读拒绝，未建立长期Workspace快照。

线上只读还发现两条旧waiting_external任务已有activewait但accepted决定缺失。仅凭这两个状态不能推断恢复：合法新输入也可能撤回旧决定。部署前须核原wait调用、最新paid响应和后续业务输入；只对原回执能证明的旧损坏作一次原事实修复，不加常驻猜测恢复或重置任务。此项及备份/升级后的实际结果另记。

一次中断采样遗留的11MiB假日志目录清理被自动审批以blocked by policy拒绝，命令未执行，未重试或绕过；不影响最终两份正常收拢的闲置样本。


### 首轮CI修正

PR533首轮Ubuntu/macOS Connector在mypy阶段发现Windows专用API未隔离的9项类型错误；Windows本地未暴露，其他平台不能按Windows结果代验。修正将WindowsJob实现放入真实平台分支，以只有cancel/close的通用进程组接口跨层，subprocess flags沿平台正常分支装配；无额外执行限制、兜底或测试削减。同源码linux/darwin/win32类型目标均通过，真实Shell/子孙取消/旁路保留/日志/文件10 passed、1平台skip，随后按新HEAD重跑CI。


第二轮CI三OS Connector、三平台原生库、后端类型、集成、headed环境与Native CodeMode均通过；完整单元测试3849 passed/5 skipped，仅原POSIX取消替身未接收source参数而TypeError。替身按已存原work/conversation/generation来源更新并核同一来源，产品取消链继续携带真实source，不回退实现；Windows本地12 passed/4 POSIX skips，实际POSIX结果继续认更新后CI。

用户随后要求再收范围且“别收过头”。任务书经三名同级子代理复核，仅收外部工具维护、全电源矩阵、额外性能平台和语义监督/汇报门槛；保普通授权Shell安装配置、跨目录文件、既有Agent/头像/媒体/远端页/设置、原五目标交付及T13/T14已证修复。此项只改任务书，不追加运行时机制。

最后核对V19时，发现本机terminal_read将每页输出/游标重复写主库progress_json，相同ack和已存completion也会进入空writer并发布提交提示。已删普通read/control视图写入；exec/get保存原接纳事实，任何路径首次发现completion仍沿原事务结算。相同原回执只读返回，冲突仍由原仓库拒绝，不新增状态或恢复机制。三个真实DB回归before全部失败、after通过，连同原桥与真实TLS WSS/HTTP共18项通过；未将不必要通知推断为已发生的死循环。
