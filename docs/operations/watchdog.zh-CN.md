# 看门狗运维说明

看门狗周期性只读检查运行事实，发现新问题时由 Yuki 经超管的主动消息入口私聊说明。设计与决定见[任务书](../architecture/Yuki-看门狗与主动私聊任务书-2026-11-20.md)。

## 谁会收到

- 有超管资格、启用且未被合并的 Person。
- 只走本人在 Desktop「主动消息发送入口」里设置的路由（`person_active_routes`），包括它设置的那个 Presence。没设置、已暂停或该 Presence 不可用时不发送，回合保持 pending，健康里显示 `route_none`／`route_paused`／`route_presence_unavailable`。恢复后，下一次检查会送达。
- 遗忘（删除）某人时，他的通知回合和问题行一并删除。
- 失去超管资格后不再收到；新超管会在下一次检查时被告知当前仍开放的问题（每个一次）。

## 节奏（热配置，全局）

| 键 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `watchdog.check_interval_seconds` | `WATCHDOG_CHECK_INTERVAL_SECONDS` | 300 | 检查间隔，也是批次窗口；`0` 关闭看门狗（之后每 60 秒重读一次该值） |
| `watchdog.no_progress_seconds` | `WATCHDOG_NO_PROGRESS_SECONDS` | 900 | 卡住多久才说；清除后在此间隔内重现算同一次，不再说 |
| `watchdog.notify_min_interval_seconds` | `WATCHDOG_NOTIFY_MIN_INTERVAL_SECONDS` | 1800 | 同一人两次激活（含重试）的最小间隔；期间的新问题合并到下一次。最坏每人每天 48 次激活 |
| `watchdog.protocol_storage_warn_ratio` | `WATCHDOG_PROTOCOL_STORAGE_WARN_RATIO` | 0.8 | 协议存储达到总容量此比例时提醒一次；达到上限再提醒一次 |

以上只是汇报节奏，不改变任何执行、准入或容量。

## 运行时异常停住的 Work

根 Work 因不可重试的运行时异常进入 failed（例如原生搜索请求回复传到一半断开得到的 `LLMNativeToolError`、重启打断在途请求的 `work_response_not_persisted`）时，那一轮没有任何交付，看门狗开 `work_failed:{work_id}:{revision}` 告诉超管停在哪一步（`stage`、`code`）、能否沿原执行链恢复（`resume_blocker` 为空表示可以）。模型自己用 `manage_task(fail)` 结束的、子任务和委派记录都不报；管理端 fail 的通常不报，但若原因文本恰好等于该 Work 之前的失败码（例如 resume 后、尚未有新响应时被结束），会被误报为运行时失败。处理方式：本人的任务可以让 Yuki 继续；也可以放弃恢复（#666/#673）。上线后第一次检查会把当时所有这样的 failed Work 合并成每人一次通知；之后只报新出现的失败，同一 Work 恢复后再失败会再说一次。

只读核对哪些 failed Work 会被报告：

```sql
SELECT w.id, w.revision, r.stage, w.reason, datetime(w.updated, 'unixepoch')
  FROM runtime_work w JOIN runtime_work_recovery r ON r.work_id = w.id
 WHERE w.state = 'failed' AND w.parent_work_id IS NULL AND r.exit_reason = 'failed'
   AND w.reason = json_extract(r.failure_json, '$.code');
```

## 看状态

- `yuki admin health` 或 `GET /admin/v1/health` 的 `watchdog` 行：`running`、`last_check_at`、`error_category`、`check_errors`（哪项检查读取失败）、`open_issues`、`open_turns`（pending／running／可重试 failed 的数量与原因）、`last_turn`。
- `/healthz` 的 `watchdog` 只给内存字段（不含 ID）。
- 只读 SQL：

```sql
SELECT person_id, issue_key, datetime(opened, 'unixepoch'), turn_id FROM watchdog_issues;
SELECT id, person_id, state, reason, attempts, datetime(started, 'unixepoch')
  FROM proactive_turns ORDER BY created DESC LIMIT 20;
```

## 回合状态

| state | 含义 |
|---|---|
| pending | 等待送达：路由缺失／暂停、会话正忙或未到最小间隔（`reason` 说明） |
| running | 正在运行；进程中止后启动时按 Social 回执结算 |
| finished | 已结束；`reason=cleared_before_start` 表示问题在开口前已恢复，没有发消息 |
| failed | 模型或网关失败，或回合结束却没有成功发送（`no_delivery`）；`attempts=1` 时会在一个检查周期后（且满足最小间隔）重试一次，`attempts=2` 后等下一批新问题一起说 |
| unknown | 进程中止时已有发送记录但结果未知；不重发 |

## 它不会做的事

- 不执行任何管理动作；通知回合里除 `send` 外的非只读调用都返回 `report_only_source`，Work 控制工具返回 `work_control_unavailable`，`send` 只能发到当前私聊（带 `target` 返回 `permission_denied`）。处理由主人在普通对话里同意后进行。
- 通知里只有内部 ID、状态、原因代码、计数和时间；不含任务目标、委派说明、原生请求参数或任何人的消息正文。
- 不检测 Bot 自身宕机（Bot 无法报告自己的死亡）。
- 不发每日摘要；没有新问题时不说话。

## 每天一次的“看一看”（同一入口的第二个来源）

`SelfLookaround`（#541）复用同一个主动私聊入口：每天一次，在每位超管私聊里只读地看一看 Yuki 在意的事（`category='concern'` 的 SELF 记忆），值得说才用 `send` 说一次。合同见[主 Agent](../architecture/main-agent-runtime.md#每天一次的看一看self-lookaround)。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SELF_LOOKAROUND_ENABLED` | `false` | 打开后才登记回合；关闭时零调用 |
| `SELF_LOOKAROUND_HOUR` | `6` | 每天几点起登记（按 `MEMORY_DREAM_TIMEZONE`，排在 Dream 的 `MEMORY_DREAM_SCHEDULE_HOUR` 之后） |
| `SELF_LOOKAROUND_MAX_MODEL_REQUESTS` | `8` | 一次回合最多付的模型请求；到上限结束回合（原因 `model_request_cap`），不重试 |

这三项是启动配置，不是热配置。路由、最小间隔与“同一人同时只跑一轮”沿上面的看门狗规则；健康里是 `self_lookaround` 行（`enabled`、`running`、`attempts_running`、`last_error_category`），回合行在 `proactive_turns`（`source_kind='lookaround'`）。没有可见的在意的事时回合记 `cleared_before_start`，不调模型。这一轮不写 SELF 记忆、不执行任何写入或管理操作。
