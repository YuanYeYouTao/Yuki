# 2026-10-23 开放 PR 合流

从 #498 的 `8bd062b7` 开始，在独立 worktree 合入 #494 原提交
`54b7f194b519a06ae354f4a6b91f97c6981e35df`。保留原 PR 的祖先，不重写它的分支。
随后顺序合入 #497 原提交 `4d64b7055fa0ee364a08286bba954d455c0b0577`，保留两项 PR
和 #498 的全部祖先。合流、本地验证、远端 CI、合并和部署分别记录。

## #494 与 SQLite 改动的整合

实际有六个冲突：架构索引、Plugin SDK facade、AgentTools、Main manifest、Social history 和
Social service。不能只解决冲突标记：自动合入的新增读路径、测试 fixture 还调用已删除的
`Database.sessions()`，新回归也有沿用退役 `send_message` 工具名的调用。

- 来源发现和历史读取保留 #494 的平台、群私聊、目标、日期与媒体条件交集，以及真实 ID 续页。
  新增入口统一用 `read_session()`；需要多次查询的路径显式 `BEGIN`，供给 session 的查询不结束
  调用者快照，不自动 flush。移除 owned reader 外层重复的 `no_autoflush`。
- Client、Social、Plugin SDK 共用原 ACP 成功发送回执谓词。平台来自原 Conversation 关联的
  Binding/Presence；停用入口仍可发现历史，不拿今日发送资格重建历史身份。
- 保留 #498 的 `send` 工具和原发送、文件发布、paid journal 恢复。只迁移新测试的调用名；
  `SocialOperation.action=send_message` 及旧已付调用的原工具名继续作为历史事实保存。
- Main manifest 的 29 已包含统一发送工具；合并来源筛选与目录 schema 后升到 30，工具数量不因
  这一页目录扩展而增加。原部署冻结声明、执行处授权和已提交协议快照的语义保留。
- 保留 `HistoryResult.source_event_ids`、`source_privacy_generation`、原请求来源和发布前核验。
  AgentTools 的数据库 phase/unknown 处理与仍有实际消费者的 `history_source_changed` 保留；
  删除退役网关历史错误说明。
- 流式读取的 SQL task 与 cursor 有自己的结束点。首个取消中断原 SQLite driver；重复取消继续
  等原 operation 和 cursor close 完成，再传播首个异常。复用现有 `_complete`，不加入重试或
  新的事务 owner。供给 reader 时也不靠调用者最终关 session 代替 cursor 退出。
- 新迁移 0148 仍只增加三个 Conversation 来源定位索引，接在 0147 后，不改旧迁移、业务数据或
  FTS 构造。部署前的升级、备份和停写按原运行手册处理。

0148 的实际新增为 `ix_conversations_space(space_id, id)`、
`ix_conversations_identity_binding(identity_binding_id, id)`、
`ix_conversations_space_binding(space_binding_id, id)`；每项只索引对应归属列非 NULL 的行。
升级回归逐表比较原行、原结构与外键，并分别检查 Space、个人 Binding、群 Binding 的 scope/display
查询计划。#497 的新迁移已排到 `0149`，接在 `0148` 后；三项来源索引、电脑凭据 purpose、
原生资源和操作/事件回执全部保留，只有一个 migration head。原 SQLite 冻结报告中的 97 个
revision 和 `0147` 是当时的快照，不把后续两项有效迁移删回旧 head。

## 验证记录

合流后的 30 个改动 Python 文件 AST、Ruff、格式与 `git diff --check` 已通过；14 个变更产品文件
按 CI 的 Linux 目标通过 mypy。Windows 默认目标会在未改动的 POSIX sandbox/CodeMode 代码上报
四个系统属性缺失，未为这次合流修改它们。

新增三项取消回归先对 #494 原 helper/cursor close 运行，三项均失败：后续取消替换了首个异常，
供给 reader 的 cursor close 还会提前结束。恢复修复后，三项全部通过；测试使用真实 SQLite
阻塞读取、原 cursor close 的结束 barrier，分别验证 owned/supplied 入口与原 reader 快照仍有效。

目标组共 13 个文件，覆盖 history query、目录、原来源与回执、SDK 的 ACP 出站过滤、0148 升级、
QQ→TG 查阅→原 QQ 发送、CodeMode 假 worker、统一 `send` 和旧已付调用连续性。首次 137 通过、
1 失败、4 项 native worker 排除；唯一失败来自新目录测试沿用旧 `merge_persons` 调用。
将其改为 reader 准备、writer 应用的现行入口后，整个目录文件 8 项复跑通过。合计 138 个不同
目标用例已验证通过，没有把后续子集复跑伪写成又一次全组结果。

上述都是本地定向验证，未运行模型或真实平台发送；最终组合全量与 native worker 仍由合流 PR
的 CI 验证。旧 PR 的绿色 CI 不代表组合后的兼容性。

## #497 与 SQLite、历史查询的整合

- Main manifest 升到 `31`，同时保留 #494 的来源目录/schema 和 #497 的固定 `external_agent`
  声明。原已付请求、执行 ID、工具配对和冻结协议保持原件；新能力不替旧请求重做声明。
- 新领域只读改用 `read_session()`，实际写入沿 `immediate_session()`。没有恢复裸 factory 或
  通用快照升级重试。传输、原生 RPC 与大正文编码在 writer 外；writer 核当前人物、原 lease、
  资源身份、pending 类型及完整 wait 来源，重要事件、原任务回执、Work 输入与 signal 同笔提交。
- 当前 JSON 的固定字段更新使用 SQLite `json_set`，保留 null、数组和布尔值；不使用会删除
  null 键的 Merge Patch。permission 继续采用原 `INSERT RETURNING` 的内部整数事件 ID。
  SQLite 仍需处理这些 JSON，不能将删掉 Python 历史解码解释成没有 CPU 成本。
- detach 结束所属或监督关系，`native_status/native_pending` 仍描述原生执行。新 Work 输入、
  effect、原 journal 恢复和 wait 沿原 ID 保留这两层事实，未知查询不重新派发原任务。
- 隐私与归档保留 #498 的原来源和 closure/phantom 证明。旧 Host ACP 的清理仍需要原 close
  接纳回执；缺连接、未知和拒绝沿原未确认错误退出，原 `request_id + ':peer'` 定位保留。
  这项检查没有扩成必须停止用户电脑 daemon 或等待原生取消完成的新条件。
- 连接器初始化纳入已有关闭边界，中途取消会关闭已启动和正在初始化的自有进程。服务器先
  断开所有原 RPC，再关闭传输、join 原清理生产者，原数据库异常继续可见；生命周期在主运行时
  和维护停止后、数据库关闭前收口，不关闭用户 Codex daemon。
- 旧服务器 ACP 的新建入口、配置和 SDK 已删除，原 receipt 查询/精确退出路径保留。Manager
  连接初始化失败关闭回归继续覆盖实际 SQLite owner，退役 ACP 配置参数的测试一并删除。
- 运维文档补充已有 SSH 的 loopback 隧道：本机 HTTP/WS 由 SSH 加密远程一段，公网直连仍用
  HTTPS/WSS。未增加公开端口或地址特例；本地安装、配对、上线由后续真实回执分别记录。

本地结果：server/Work/身份/HTTP/0149 组合 97 passed；Fake bridge、旧 ACP receipt 和隐私清理
55 passed；大正文/JSON 类型与整批回滚 2 passed；关闭与传输 6 passed（其中 5 项属于前面的
97 项），补充 shutdown 原提交 ACK 丢失 1 passed。Manager 启动/构造关闭 7 passed。
独立连接器完整包共 33 passed，包括实际 CP936／CP1252 CLI 输出和 Windows 私有 ACL 子进程隐藏。
补充原 Work settlement/owner recovery/子任务 checkpoint/lifecycle 四个文件共 148 passed，
覆盖普通 Work 与新外部执行共用的原恢复、wait 和结束边界。
初始化取消使用两个真实自有子进程先红后绿，关闭故障也先红后绿；提交 ACK 测试实际完成原
SQLite commit 后抛出原异常，核 phase/unknown、原 producer 结束和已写入结果。

Bot 全部 640 个源码文件按 Linux 目标 mypy 通过，独立连接器 12 个源码文件也通过 strict mypy。
尚未运行组合后的全仓测试、远端 CI、真实模型或平台消息；上述组别有复跑和重叠，不将它们
相加冒充全局验收。

## Desktop 薄入口的组合核查

另行只读核查 Desktop [#3](https://github.com/Institute-of-Agent-Life/Yuki-Desktop/pull/3)，head 为
`ece6efd2d8cfdc8f40719a762e5c43abb4d16eb3`（base `cff30738`）。11 个变更路径沿薄 CLI/IPC
入口接入，凭据和原生 owner 留在连接器，Client API 6 与 `/agents/v1` 各认原用途。
当次五个平台构建检查均成功且对应此 head；这与本地安装和配对分别记录。

实际 Desktop `runConnector` → 本次 CLI 的 UTF-8、CP936、CP1252 三组进程探针均保留原中文
label。已修 machine JSON 在 Windows 管道中的编码差异，并隐藏 ACL 查询子进程；三项新增
回归先红后绿。探针只读取隔离 fake 配置/status，没有安装真实 Desktop、启动用户服务、
driver、模型或登录流程。Desktop 已由主执行流程独立合并为 `72c6cbf3`；本机安装、配对与
实际服务验收尚未执行。

## 最后交叉检查

合流后删去 journal 保存前重复的租约和旧媒体 refs 读取，模型前人物资格复用原有的单 SQL predicate。
reader 的公开关闭顺序改为原 Connection 后原 Session，实际 native rollback 队列从两次降为一次，
writer 的默认 pool reset 保留。原来源、身份、paid、隐私与取消边界完成独立复读；六文件定向首次
77 passed、1 项旧调用计数断言失败，修正后该文件 18 passed，公共七文件另有 47 passed。
原材料和全部速度结果见[性能记录](sqlite-writer-performance-20261023.md)。

首次组合 CI 在 Linux/macOS 上指出连接器 ACL 分支的 `CREATE_NO_WINDOW` 类型错误。
`d80b9d8c` 将原 `os.name` 判断换成与包内其他入口相同的 `sys.platform`，Windows 行为保持。
修正后 Linux/macOS 各 32 passed、1 项 Windows-only skip，Windows 33 passed；三平台类型检查、
sdist/wheel 及不安装 Bot 的独立 wheel 检查全部通过。完整主库 unit/integration 与 Native CodeMode
以 [PR #498](https://github.com/YuanYeYouTao/Yuki/pull/498) 对应源码的 CI 记录为准。

线上只读核查还确认旧 Host Manager 没有 `workspace_copy/result`，需要按现行媒体升级手册同步源码。
新 release 使用原 venv、原 unit 参数与独立状态库；切换 PYTHONPATH，保留仍被环境 bind 的旧 src。
旧停机仅关闭观察连接，新 recover 接回原容器/执行；实际切换、一致备份和四个 peer 身份核验由后续
部署记录分别说明，不能从本地或 CI 通过推断已上线。
