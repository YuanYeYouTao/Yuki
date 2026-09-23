# Yuki V6 长时静默与免 @ 续聊：宿主接入研究

日期：2026-09-23。与 `Yuki-Semantic-Participation/docs/idle-autonomy-and-no-at-continuation.md` 配套。当前文档是实施方案；未修改运行代码，未验证线上 QQ 发言。用户当前目标优先于旧任务书对“必须有近期真人消息”和“不得无来源触发”的限制。

> 以下阻断点是实施前基线。当前开发分支已增加无来源提议入库、实际发送回执到内部线程的映射、免 At SELF 锚点和长时静默记忆扫描；数据库迁移为 `0070`，独立库固定到 `b289ec94dcfd76161517fe57424e88cef0493199`。定向联动测试与临时数据库迁移已通过；真实 Jev/Main Agent 影子验证及 QQ 验收尚未完成，未部署生产。

## 宿主当前阻断点

- `services/semantic_participation.py::_seeds` 要求控制器中有最近 600 秒真人事件；`_hydrate` 也跳过更旧事件。长时静默时，群/SELF 合法记忆不会成为新候选。
- 同文件 `_event` 构造无显式引用真人消息的 `unit_options` 时只纳入近期真人事件。自主 SELF 发言通常没有 `caused_by_event_id` 指向真人事件，其 thread 也未持久绑定到被接纳的 proposal；自然的无 @、无引用续聊容易丢失 Yuki 发言锚点。
- `conversation/autonomy_binding.py::AcceptedInitiative` 强制来源非空，来源类型只有 event/memory；`conversation/autonomy_repository.py` 要求 semantic `support_refs` 非空。独立控制器的 Proposal 也依赖来源与语义 Support，所以现在不能接纳真正无外部内容来源的发言动机。
- 当前 `pyproject.toml` 和 `uv.lock` 固定独立库提交 `4d91e39070c2811032cce70c66ed8c82fced9058`；核查时独立库工作树 HEAD 为 `6bbaff3f6c04d820fc3ade9ecb5ac42ee48f9dd3`。集成验证必须使用实际锁定的相容版本，不能靠临时 `PYTHONPATH` 代替。

## 与独立库同批完成的改动

1. 独立库加入真实 Yuki 发言锚点的语义选择与 `intrinsic` 提议契约；Yuki 同步投影内部出站事件、提供候选锚点、保存 SELF run 与 thread 的关系。显式引用优先，无引用时由 Jev 在宿主提供的有限候选中选择或返回 unknown。
2. 独立库与宿主同步允许长时静默的记忆种子。群可见/SELF 记忆不再以最近真人消息为必要条件；contact 的对象依据单独核验，不能借用真人私有权限。
3. 对无外部内容来源的 `intrinsic` 提议，两个仓库同时增加明确的无焦点来源契约。宿主以 proposal 身份、generation、owner、授权、Presence、Work 占用和持久尝试状态防重；不制造假 event/memory 来源或假 Jev 结果。现有 SELF Work/Main Agent/send_message/NO_REPLY 路径复用。
4. 同步升级 Yuki 的独立库固定提交与 `uv.lock`，完成必要数据库迁移、恢复和回执测试。两个仓库在相互匹配的提交上一起通过定向测试及隔离发送的影子回放后，才能称功能完成；单仓库通过不算完成。

验收重点：实际 Yuki 发言后的无 @、无引用续聊；他人插话与转题的误接；超过一小时静默时的记忆与 intrinsic 发言机会；无内容时 NO_REPLY；明确停止、群禁用、重启、owner 切换、重复 tick、迟到回执及唯一 Work。实验发言次数按提议统计时须单独注明，生产发言以真实发送回执统计。不设置每日发言额度。
