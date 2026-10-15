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
P3 记录见下；P4–P11 尚未完成。

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
