# 本机 Agent 接入实施与验证

日期：2026-10-23。基线：main `c2ce7da6`。范围来自[任务书](../architecture/Yuki-本机Agent接入与ACP统一重构任务书-2026-10-23.md)，操作见[连接器说明](local-agent-connector.zh-CN.md)。

## 本地实现

| 任务 | 实现与实际边界 |
| --- | --- |
| T01 | Person → Device → instance → session → execution 使用内部资源 ID；原 operation 保存真实 actor、来源、原请求及回执，不另建业务状态机 |
| T02 | 提取共同 DeviceCredentials/Person 资格；chat 与 connector 明确区分，电脑配对不创建 App Conversation，同人归并保原设备 namespace |
| T03 | 在原 FastAPI 上增加 `/agents/v1` HTTPS/WSS；原操作先落意图再在事务外投递，重要事件先提交原回执/Work 输入再 ACK |
| T04 | Python 3.12 独立连接器包、锁文件、CLI、私有配置、原操作 journal、用户服务、独立 wheel 验证与三 OS CI 作业 |
| T05 | Codex 官方 proxy + 维护中的 WebSocket 库接原 daemon；目录、原 thread/turn、steer、queue、审批、定向取消与监督 |
| T06 | Claude 官方 SDK 指定实际 CLI/native preset/原生设置；new/resume、事件、正式 callback、取消；不声称任意 live attach 独立 Claude TUI |
| T07 | 通用 ACP 驱动协商真实能力，文件和终端回调在实际电脑；缺项保原结果，无服务器 fallback |
| T08 | 固定 external_agent 合同 revision 30；direct/Code/Script/能力/效果/预算/恢复同改，原已付输入不改写 |
| T09 | 重要进度/问题进入原 Work 私有输入，终态复用原 SandboxTask continuation；统一 send 不变，不镜像完整 transcript 或逐 token 写 DB |
| T10 | Work/子任务/隐私清退沿原操作，watch 不独占原生 session；detach 结算所属关系并保最后 native 事实，不取消用户 daemon |
| T11 | 删除旧 server ACP 新建/启动/配置/挂载/安装/新调用路径；仅保原活动结算和历史 reader，ACP A 保留 |
| T12 | Windows 连接器、真实 Codex、独立 wheel 与用户服务有本地证据；Desktop 独立工作树实现薄管理；其他 OS 实机范围见下文 |
| T13 | 四方向交叉终审和回归已实施；提交、PR、CI、合并与部署继续分别记录 |

新增迁移 `0148` 只添加 connector purpose、原生资源与操作/事件回执。旧凭据默认 chat，原 Work、预算、journal、已付请求、事件与发送回执不重置、不重跑。启动只核版本及必要表列，不扫描业务外键或要求加速索引。

## 四方向终审与修正

1. 身份与授权：两台电脑同名/同 native ID 不混线；实际执行处复核原 Person、当前同人 root、设备撤销和当前 socket/profile。多个目标 ID 必须指向同一原资源。只读查询不创建 Work，重复目录和相同回执无热写。
2. 原操作与恢复：派发后失联保 unknown，按原 ID 查询，不盲重发；原 native turn 接受早于 RPC 回包时仍按原 turn 关联；本地同时新建的另一 turn 不被抢作远程任务。终态不被迟到进度或回包覆盖。原队列取消保精确 queue ID。
3. Work、上下文与出口：删掉原生 cancelled/interrupted 阻止反馈的旧 blocked 门槛；真实取消仍交回活跃 Work，已终态 Work 不复活。detach 明确为关系结束，私有信号同时保 native_status/native_pending；不把它写成原生成功。重要事件不带 reasoning/完整 turn/stdout 镜像，工具声明不随目录变化。
4. 删除与产品边界：标准驱动是普通模块，不新增 runtime、PluginHost 或全局调度器。移走沙盒里的外部 Agent 执行分类，删除失效的 prepare 特判和旧安装配置。停止网络与停止本机服务分开，撤销远端凭据不能经连接清理杀掉原生执行。

Work/generation 终态和原清退入口负责收拢所属关系；不为 QQ Binding/Presence 的停用另建每两秒全域来源扫描，也不把平台入口停用当成人物电脑授权撤销。真正的新效果复用当前来源校验，并在排队等待 writer 后、首次派发前复核。

## 已取得的验证

### 服务器与迁移

- 真实 HTTPS/WSS + SQLite + Work 集成覆盖配对、身份、目录分页、重要事件提交后 ACK、原问题回答、断线、原操作查询、精确目标和迟到回执。测试对端明确为协议替身，不用它冒充真实模型。
- 0147 → 0148 四项真实迁移回归通过：旧 chat 凭据仍可用；已付请求/预算/原回执不变；设备命名空间与实际 FK 约束成立；启动不扫业务表，缺必要新结构会拒绝。
- 主项目 Ruff 与 Linux 目标的完整 mypy 通过。后者在 Windows 执行类型检查，不等同 Linux 实机验收。
- 新链及受影响身份、Work、角色、工具与生命周期的定向回归 96 项通过；排队后来源失效的真实竞争回归另 1 项通过。后续 detach 消费者补修仍单独验其影响。
- Windows 扩展全库测试截至首次停止为 2425 passed、13 skipped、93 deselected、5 failed；五项失败均在原有 `test_shared_inbound_media` 调用 Linux `fcntl`，此分支不改该模块或伪造其文件锁。Linux CI 与剩余用例另核，不称全库已绿。

### Windows 本机真实 Codex

实际版本 `codex-cli 0.163.0`，连接既有官方 daemon，连接器 `0.1.0`；TLS/WSS 服务与临时数据库在本地隔离环境，**不是生产 Linux 服务器联调**。

最终完整验收用新测试会话执行临时工程中的两个指定 PowerShell 命令，写入唯一 marker、真实读回文件；审批使用原问题的内部 permission ID，经公开 respond 回到原生正式接口。只批准精确测试命令与目标目录，不改全局审批配置，不自动批准其他问题。

结果：`1 passed`，34.98 秒；9 条重要事件、3 条工具里程碑、2 条成功命令回执、1 条初始 sandbox 失败回执，原 terminal 成功转入同一 Work 的 completion 输入。初始失败没有抹掉。文件内容和 final marker 均核对；测试 thread 已归档，未控制原用户会话、重启或关闭 daemon。

首轮在真实审批处按预定拒绝自动放行；第二轮实际执行成功，但验收误要求 pwsh 包装后的命令包含原始未转义字符串。修正为核真实成功写/读回执及目标路径后，最终完整链通过。没有放宽正式审批的精确命令匹配。

原始最终报告位于本轮临时文件 `yuki-local-agent-bridge-live-20261023.json`，不是部署文件。上游缓存 usage 未提供，仍记 unknown；不由事件数量、字节或本地耗时声称缓存收益。

### 独立包与 Desktop

连接器 28 项协议回归通过，使用真实 stdio ACP 对端、官方 Claude SDK 协议解析和可控 Codex WebSocket；与付费模型验收分开。独立 wheel 已在干净环境安装 39 项锁定依赖，CLI 和三驱动可导入，没有安装 Bot 包。Windows 与 Linux 目标的包级 mypy、Ruff 通过。

最后一轮 detach 全消费者补修后，9 项终审与 147 项既有 Work 回执/等待/查询回归通过，4 项 POSIX 场景在 Windows 跳过。监督关系的 detached 状态与 native 最后事实经过 completed_children、原 effect 对账和 owned_run 等待唤醒仍保留，不能被简化成原生 succeeded。

另在 Windows 使用实际安装的 Claude Code `2.1.283`、SDK `0.2.172` 完成隔离目录初始化：显式 cli_path、system/tool 的 claude_code preset 与 user/project/local 设置来源传入后正常握手，未提交模型 prompt、未消费编码模型，随后关闭本测试 SDK owner。此证据不证明真实账户、新建/resume 编码或任意 TUI 接管。

Windows 用户服务烟测实际完成：当前用户 InteractiveToken/LeastPrivilege 注册、启动、连接 loopback WSS、读取 running、停止原进程、卸载。没有配置 native 实例或调用模型；原测试任务/进程已清理。停止按原进程句柄退出核对，不用固定 sleep 冒充结束。

Desktop 在独立分支 `codex/local-agent-connector-20261023`、基线 `cff3073` 实现设置中的“編程 Agent”。仅固定 CLI IPC：配对码经 stdin，状态白名单，不读取 token，不依赖 Conversation/旧 executor，不随窗口退出停服务。9 项单测、1 项隔离浏览器交互、全库 typecheck/lint 和正式构建通过；未启动真实已配对的本机服务。

## 尚未取得的验收与交付

- Claude 的真实账户/模型和 native preset/config 完整编码尚未实测；Windows 实际 CLI 已完成初始化，不能把它或协议回归称为完整编码或接管用户正在运行的独立终端。
- Linux/macOS 用户服务已实现并加入对应 OS 的 CI，尚无本轮 Linux/macOS 原生模型与服务实机验收。CI 的 fake 协议不替代真实账户。
- 尚未进行生产服务器到用户电脑、真实 QQ/TG 自然流量或真实 Social 消息验收；测试未向真实群发送消息。
- wheel 构建不等于 PyPI 发布；Desktop 构建不等于安装包 Release。PR、CI、合并、镜像与部署分别记录，本轮没有生产上线。

## 合流记录

另一会话正在处理 SQLite writer 排队。此分支不改 `Database` 的 writer 机制，只复用现有短事务和 snapshot writer；唯一新增数据库实例属性用于服务注入。当前仍需在对方 PR 出现后核迁移 head、公共接口和真实竞争回归，不凭“不太可能冲突”当成已验收。
