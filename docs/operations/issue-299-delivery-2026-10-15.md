# Issue #299 分阶段实施记录

实施分支：`codex/issue-299-implementation`。起点为任务书修订
`84d3c3a1edc0c57041a450603cecfe682a0b5513`。各阶段独立提交并推送到同一分支；
本文记录源码与验证，不表示已经合并、迁移生产或部署。

## P1：领域授权与命令回执迁出

`/ai` 依赖的主体、授权判断、目标、问题与命令类型迁到 `admin/authority/`。
原能力校验、JSON 和 token 类型、命令回执校验使用同一份定义；
旧 WebUI 的查询 DTO 与方法表仍在旧层，供 P3 退役。

`persistence/command_receipts.py` 接管原表的意图登记、结果写入、重放、审计关联、
未知结果登记、启动恢复与原领域运行的状态投影。数据库命令和外部命令均调用它。
表结构、操作 ID、领域效果和未知不重发的语义保持不变。

验证（本地）：

- 管理、群恢复、Memory 回执、Work 恢复、HTTP 与恢复测试：117 passed。
- 最终意图写入迁移后的回执专项：2 passed，覆盖重启恢复、效果已发生但确认丢失、
  同请求重放及换请求号不能绕过未知结果；外部效果期间可取得独立 SQLite writer。
- `ruff check .`、`mypy src`、`git diff --check` 通过。
- HTTP 首次检查因缺少前端资源出现 25 个 setup error；按原锁文件、使用本机已有的
  Node 24 完成构建后，39 项 HTTP 测试全部通过。构建资源为忽略文件，未纳入提交。
- 上述 SQLite 测试仍有既有循环外键排序警告；未修改相关模型或约束。

未运行真实 QQ、付费模型、生产库迁移或部署。本阶段不需要外接模型 API。

## P2：管理 API 与 CLI 接管

新增 `/admin/v1` Bearer 入口与管理 CLI，接入原 PluginManager、配置文件服务、
`manage_work` 和命令回执。operator 认证迁入共有授权目录；部署向导与在线插件 CLI
改走新 API，删除旧 Cookie 客户端。当前新旧 HTTP 入口共用同一个模型文件服务，
避免过渡期出现独立文件锁；旧入口本身在 P3 删除。

交付身份创建／停用、绑定挂接／停用、插件全流程／配置／观察／通知重试、健康、
原操作查询、轨迹读取、模型文件与新增密钥写入，以及 Work 取消／续跑。
密钥别名放宽为现有环境变量名称合同，原别名继续可读；密钥只写入原独立文件。
新 CLI 遇到未知结果会停止当前流程，保留原请求号供查询。

验证（本地）：

- 管理、旧 HTTP、operator、群恢复、Work 恢复与部署向导相关回归：132 passed。
- 补充 Work 接口场景后，最终管理 API 专项：13 passed。使用真实 ASGI、SQLite、
  插件加载／启停／schema／observe、原配置热应用与假模型连接；覆盖权限、凭据轮换、
  请求重放／冲突、绑定拒绝、未知效果查证、通知重试与尝试预算、正文单独授权、
  密钥不回显、原 Work 取消／续跑与 revision 重放。
- `ruff check .`、`mypy src`、CLI 帮助与新文档本地链接检查通过。
- 通知专项发现新适配器刷新 ORM 行会丢弃未提交的重试状态；已删除这次刷新，
  正向重试与已发送／未知拒绝场景通过。未放宽原领域重试条件。

未运行真实 QQ、付费模型、生产迁移或部署；本阶段不需要外接模型 API。
P3、P4 记录见下；P5–P11 尚未完成。

## P3：旧 WebUI 与旧控制面退役

删除前端、Cookie 会话、旧 HTTP／查询／命令适配层、旧读模型、compose overlay、
Node 镜像构建与前端 CI。当前说明改用管理 API；日期任务书和发布证据保留。
共有授权只保留实际能力核验和现用命令合同；模型文件服务只保留实际模型文件消费者。
原命令回执、审计、unknown 查询与启动恢复继续使用原表、原请求号。
Manager 删除 `workspace_upload`；原终端完成结果读取暂留，等待生产退出核验。

验证（本地）：

- 管理 API、operator 与旧入口 404 专项通过；旧 Work／账本断言改用现行领域读取。
- `ruff check .`、`mypy src`、`mypy --platform linux src` 通过。
- wheel 构建通过，包内无旧 WebUI、control_plane 或 persistence/control_* 模块。
- macOS 全量：1730 passed、67 skipped、5 failed。5 项均为未改动的平台相关测试：
  4 项依赖 GNU `script -c`，1 项依赖 Linux `/proc`。
- 对上述原测试所在的两个文件，隔离 Linux 容器验证：31 passed、4 skipped
  （缺少 PowerShell），原 5 项全部通过。
- Linux 完整测试文件集合分四组：1735 passed、67 skipped、1 failed。
  唯一失败为未改动的 `test_work_scope_contention` 的 expired 场景；
  其整个模块隔离复跑为 12 passed，代码和 0.5 秒等待断言均未修改。
  skipped 为未构建的可选 Monty worker 与未安装的 PowerShell。
- 首次并行测试临时空间不足，属于测试环境失败；扩大空间并仅保留一轮完整测试后，
  得到上述分组结果，不把空间不足的那轮计作源码验证通过。

生产退出核验尚未执行：没有检查生产库的 running 控制回执，也没有更新宿主 Manager。
本记录只说明代码退役与本地验证，不表示生产退出条件已满足或已经部署。

## P4：私聊会话按绑定拆分

新增迁移 0120 与私聊所属绑定列，私聊唯一索引改为绑定。旧会话按原 primary alias
回填，事件、Work、原预算、回执、检查点和 primary alias 不迁移；不能确定的会话
列出 ID 并中止。新私聊以 `binding:<id>` 为 primary，冲突的旧 transport alias 留在旧会话。
未拆分的 schema 可降回 0119 并重新升级；出现多个入口会话后拒绝降级，不合并历史。

入站、出站、提示词快照、账本窗口／搜索／读版本与隐私范围共用绑定解析；
已有 turn／读版本按原 Conversation ID 读。作者解析使用入口 Presence 的平台。
自动化优先沿原创建会话，缺失时沿主动路由绑定；插件后台完成恢复从原事件取传输身份，
不把新的 primary alias 当传输地址解析。Self Reflection 与工具结果原已按会话 ID 关联，保持。
Person 遗忘遍历全部私聊，包含停用绑定，并在删除绑定前清理原 Work 与会话。

验证（本地）：

- 新增契约测试：6 passed，覆盖四绑定共用 Person 但历史与 generation 独立、
  旧 alias 冲突后的连续收发／搜索／快照、另一入口重建不改变原 Work、
  停用绑定的隐私清退、原迁移事实保留、无法定位时回滚与 schema 往返／拒绝合并。
- 首轮全量暴露的历史 alias 假设和旧迁移夹具问题，修正后相关模块：72 passed。
  旧迁移夹具构造事实时临时提供当前 ORM 所需的绑定列，关闭种子库前删除；
  实际 Alembic 升级仍从原结构开始，不在生产代码加入旧 schema 分支。
- 最终 Linux 完整回归（四组均退出 0）：1742 passed、67 skipped。
  skipped 为可选 Monty worker 未构建与未安装 PowerShell；未新增跳过条件。
- `ruff check .`、`mypy src`、`mypy --platform linux src`、`git diff --check` 通过。
  随后把“另一入口原 Work 不变”断言加强为完整持久行相等，该专项 3 passed。

未在生产副本运行迁移或统计多绑定／alias 冲突；未部署。App Provider、个人 API、
完整模型请求与跨设备验收继续由 P5–P11 实现和验证。

## P5：个人设备、绑定码、账号转移与归属区间

迁移 0121 新增一次性码、独立设备哈希凭据与 App Presence，重建绑定的状态与部分唯一索引，
重装原路由触发器。旧行和旧事实不变；所有新增绑定路径写自身创建时刻作为历史起点。
App 配对创建服务器账号与唯一主会话，多设备共用。operator、设备与执行器权限保持分开。
QQ `/ai link` 在普通入账与自动建 Person 前核验真实连接并处理，码不进入聊天、Main 或 Memory。
新增配对、签发码、me、撤销、operator 转移与按 Person ID 遗忘入口，复用原命令回执和审计。

转移保留旧绑定的 Person、会话、作者和执行来源；另建目标绑定，暂停指向旧绑定的主动路由。
按账号查询排除 transferred，停用的当前绑定仍阻止自动新建身份；停用历史行不改写其转移终点。
平台私聊历史按当前行起点过滤，时间缺失或早于起点计入 withheld_unattributed，旧锚点失效。
遗忘按 canonical 作者／全部私聊与绑定自身区间处理，正文准备放在写事务前；群清退及命令门锁
不再沿账号命中原持有人群消息。Memory 重建按来源事件清理有界提案，运行选择集保持。
设备、绑定码和原个人回执随 Person 清理；原不设界绑定的账号脱敏行为继续保留。

验证（本地）：

- 最终相关回归在 macOS 与隔离 Linux 上各 104 passed。覆盖身份隔离、一次消费／到期、
  并发配对、两个设备共用会话与独立撤销、管理能力、转移重放／冲突、无 QQ 的配对／遗忘、
  网关未认证拒绝、原 Work 来源失效、双向遗忘和第三任账号、两时期 Memory 重建提案、
  前任行删除后仍按现任起点过滤、旧平台历史锚点拒绝与原 Social／Memory 回归。
- SQLite 实际 busy 确认未提交后返回 unavailable，码可原样兑换；注入“提交成功但确认丢失”
  返回 unknown，原码保持已用，未伪称回滚，也未创建设备两次。
- 额外 Linux 全量四组：1757 passed、67 skipped、3 failed。失败均为旧迁移夹具：
  0105／0115 在旧表调用当前 ORM，0117 的跨后续迁移比较未计入 0121 的预期表变化。
  修正夹具后，实际 Alembic 仍从原 schema 升级；相关迁移模块 7 passed，且包含在上述
  最终 Linux 104 passed 中。没有把第一次全量描述为全绿；最终完整回归留到 P11。
- ruff、macOS／Linux 目标 mypy（588 source files）、diff 静态检查与新增 CLI help 通过。

未使用外接付费模型、生产数据库或部署。完整 App 收发、文件授权与跨入口端到端验收
由后续 P6–P11 完成；本阶段的配对与归属测试不代替那些验收。

P5 秒级边界补验：平台历史整秒等于绑定起点时也不返回，避免起点恰好为整秒时开放该秒。
前任仍存在／已遗忘两种场景与原 Social 契约合计 25 passed，ruff 与 mypy 通过。
