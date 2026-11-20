# 看门狗运维说明

看门狗周期性只读检查运行事实，发现新问题时由 Yuki 经超管的主动消息入口私聊说明。设计与决定见[任务书](../architecture/Yuki-看门狗与主动私聊任务书-2026-11-20.md)。

## 谁会收到

- 有超管资格、启用且未被合并的 Person。
- 只走本人在 Desktop「主动消息发送入口」里设置的路由（`person_active_routes`）。没设置或已暂停时不发送，回合保持 pending，健康里显示 `route_none`／`route_paused`。设置好后，下一次检查会送达。
- 失去超管资格后不再收到；新超管会在下一次检查时被告知当前仍开放的问题（每个一次）。

## 节奏（热配置，全局）

| 键 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `watchdog.check_interval_seconds` | `WATCHDOG_CHECK_INTERVAL_SECONDS` | 300 | 检查间隔，也是批次窗口；`0` 关闭看门狗（之后每 60 秒重读一次该值） |
| `watchdog.no_progress_seconds` | `WATCHDOG_NO_PROGRESS_SECONDS` | 900 | 卡住多久才说；清除后在此间隔内重现算同一次，不再说 |
| `watchdog.notify_min_interval_seconds` | `WATCHDOG_NOTIFY_MIN_INTERVAL_SECONDS` | 1800 | 同一人两次激活（含重试）的最小间隔；期间的新问题合并到下一次。最坏每人每天 48 次激活 |
| `watchdog.protocol_storage_warn_ratio` | `WATCHDOG_PROTOCOL_STORAGE_WARN_RATIO` | 0.8 | 协议存储达到总容量此比例时提醒一次；达到上限再提醒一次 |

以上只是汇报节奏，不改变任何执行、准入或容量。

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
| failed | 模型或网关失败；`attempts=1` 时会在一个检查周期后（且满足最小间隔）重试一次，`attempts=2` 后等下一批新问题一起说 |
| unknown | 进程中止时已有发送记录但结果未知；不重发 |

## 它不会做的事

- 不执行任何管理动作；通知回合里除 `send` 外的非只读调用都返回 `report_only_source`，Work 控制工具返回 `work_control_unavailable`，`send` 只能发到当前私聊（带 `target` 返回 `permission_denied`）。处理由主人在普通对话里同意后进行。
- 通知里只有内部 ID、状态、原因代码、计数和时间；不含任务目标、委派说明、原生请求参数或任何人的消息正文。
- 不检测 Bot 自身宕机（Bot 无法报告自己的死亡）。
- 不发每日摘要；没有新问题时不说话。
