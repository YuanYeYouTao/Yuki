# 工具结果交付与查询接口实施记录

日期：2026-10-31。范围见[任务书](../architecture/Yuki-工具结果交付与查询接口修复任务书-2026-10-31.md)。

用户已授权实施全部 14 项、循环复审至没有已知实质缺口、提 PR／等待 CI／合并和备份后的 Bot-only 部署。基线为 `34167dd4c158478ef89f3ac7892ac9d51cfaafa0`，分支为 `codex/tool-result-delivery-20261030`。旧草稿保存在本地 stash，本次未重新套用。

## 当前交付状态

- 本地实现已覆盖任务清单，三轮交叉复审发现的实质缺口已修复，未留已知阻塞缺口；完整 CI 与真实 worker 仍待验证。
- 最后修正后 Ruff、Linux 目标 Mypy（690 个源文件）与 diff 检查通过。
- 新增真实 Main／Plugin、历史与 Memory 续读、原发送回执、来源失效场景已在本地运行并产生 evidence 文件。Code 真 worker 的 8 项场景已写，等待 Linux Native CI，Windows 离线替身不作为其通过证据。
- PR、完整 CI、合并、生产部署尚未完成。

## 实现与删除

- 单次默认展示预算 64000 JSON 字符，显式 token 配置保原四倍覆盖。Main、reader、Memory 展示与 Code 采用同一有效来源，管理端只保正整数语义。
- 研究原件继续归档，小结果直接展示；超长实际 data／preview 与准确 read_more 分开，数组完整子项、字典稳定序及字符串原坐标保持。
- 删除 Memory 共同预算前的删尾和长度否决、Code 的半预算预裁剪、公开 reader 的 8000／32000 重复帽及隐式 text 默认。
- 分离 find_contacts 与 find_conversations，贯通原 namespace／权限／直调／固定声明及旧 accepted 恢复，未增兼容执行别名。
- 参数反馈指出实际错误字段；send 说明符合 Conversation／Binding 实际选择；around 不伪称应用日期或请求排序。
- 测试先写场景，再改实现；旧调用与失效断言同步处理。现有 direct CI 上传接入实际 evidence 目录；native 复用原上传及 worker 检查，无新增工作流或全局 skip 门槛。

## 复审发现与修复

1. 真实 App 显式 Conversation 发送被误套原入口回复证明：按实际目标与原入口 Conversation 关系放宽；原入口冷恢复、原事件删除和归属校验保持。
2. 首个数组项超大仍可能看不到正文：保 primary 数组原子性，并在明确子路径输出实际 partial preview；真实网页正文按原字符坐标续读已通过。
3. 数值叶可逃出展示预算、数组内部 item_limit 未生效：真实 Main 边界场景先复现再修复，未把大数值或超限子集合冒充完整正文。
4. 容量拒绝未进入可读失败分支：仅处理实际 tool_artifact_capacity 与 OSError，其他来源拒绝不降级；保真实内容、原 ID、committed 和存储失败事实，不造句柄。
5. 长发送结果可能丢分步事实：投影每步原 operation／status／event 与不可变产物身份，保确认丢失及原 accepted 重读不重发。
6. 非 Work 结果归档失败可带出旧隐私代次正文：提取原 Artifact generation／privacy 判定，在模型结果发布处复用，涵盖 inline、关闭归档及存储失败；不新增全局状态、锁、权限或历史扫描。
7. Code 子回执丢掉新 preview／read_more：现有投影只在原结果存在时保这两个可选字段，保持 data 原类型与旧快照默认。
8. Artifact disabled 在 active Work 中被隐式找回全局 store：删除这条 fallback，真实 accepted Work 场景确认无虚构句柄与归档行，保原回执。
9. 合法非字符串 JSON 键在预览焦点解析中触发 KeyError：复用原 JSON 键编码函数，预览与原件路径一致，相同编码键沿原件的最后值。
10. 参数错误新加了未知字段名 64 字符／8 项裁剪：删除这层没有合同依据的限制，继续不回显字段值。
11. Code 新 preview／read_more 可能绕过既有 VM 字节收缩：在原超字节分支一并去掉可选正文，保原引用与身份；不提高独立 VM 容量。先复现 300381 bytes 被真实 strict_json 拒绝，修后 227 bytes 可接受；真 worker 场景仍待 CI。

上述来源检查只证明本次结果交付边界核对了原版本，不宣称后续所有网络等待与未来变化都被原子锁住。

## 验证与产物

本地原始输出位于 `test-results/`，本轮业务证据位于 `test-results/tool-result-delivery/`，已忽略 Git 跟踪，由 CI artifact 收集。每次执行使用独立 attempt 目录，保留失败时已经取得的事实，不用重跑覆盖失败记录。

场景保存实际模型工具消息、原件摘要／文件、原来源、执行和发送回执及复现信息。Host 请求、真实 Provider 序列化 HTTP 和真实 worker 各按实际范围解释，未调用付费模型或主动向真实 QQ 发送测试消息。

定向批次的数字有重叠，不相加冒充独立总数。最终完整 CI、确切提交及各产物链接在取得后记录。

最终本地批次：四个普通 E2E 文件共 33 passed，受修改旧场景 93 passed／17 native deselected，来源／分页／恢复 42 passed。真 worker 文件的 8 项仅留给 Linux native job 执行，不把 collect 或离线替身计作通过。早先中途改源码时的 Windows 全量批次已取消，不能作为最终验收证据。

## 生产准备

只读预检：Bot 当前运行 `c728bb19`，CODE_MODE_ENABLED=false，环境显式配置 AGENT_TOOL_RESULT_MAX_CHARACTERS=24000，健康。上线需在新 Bot overlay 明确上调为 64000，再核实际 runtime override，不能把源码默认修改当成生产已生效。

Compose overlays、当前容器／同伴、挂载、源版本与备份在实际部署前重新读取；不把这份预检快照当部署回执。部署仅重建 bot，保 SnowLuma、持久环境、网关、数据库和原执行事实；后续健康、自然流量及清理结果分别记录。
