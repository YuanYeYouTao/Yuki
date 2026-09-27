import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import {
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
  Table,
} from "./components";
import type { Column } from "./components";
import { stamp } from "./format";

const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const columns: Record<string, Column[]> = {
  states: [
    ["canonical_person_id", "Person"],
    ["canonical_space_id", "Space"],
    ["last_event_id", "已处理事件水位"],
    ["latest_event_id", "最新事件"],
    ["pending_events", "待处理事件"],
    ["pending_since", "最早等待", stamp],
    ["last_policy_reason", "策略原因"],
  ],
  runs: [
    ["id", "批次"],
    ["cycle_id", "周期"],
    ["canonical_person_id", "Person"],
    ["canonical_space_id", "Space"],
    ["status", "状态"],
    ["source_kind", "来源类型"],
    ["processed_events", "聊天事件"],
    ["first_event_id", "首事件"],
    ["last_event_id", "末事件"],
    ["initiative_run_id", "自主轮"],
    ["first_receipt_id", "首工具回执"],
    ["last_receipt_id", "末工具回执"],
    ["proposal_count", "提议"],
    ["committed_count", "已提交"],
    ["retry_state", "重试状态"],
    ["attempt_count", "尝试次数"],
    ["next_attempt_at", "下次尝试", stamp],
    ["error_category", "错误类别"],
  ],
  cycles: [
    ["id", "周期"],
    ["trigger", "触发"],
    ["status", "状态"],
    ["created_at", "登记", stamp],
    ["completed_at", "完成", stamp],
    ["delivery_state", "报告投递"],
  ],
  requests: [
    ["id", "实际请求"],
    ["run_id", "批次"],
    ["created_at", "时间", stamp],
    ["attempt_kind", "请求类型"],
    ["status", "状态"],
    ["output_tokens", "记录的输出 Tokens"],
  ],
  results: [
    ["id", "结果"],
    ["run_id", "批次"],
    ["fact_id", "原记忆"],
    ["result_kind", "类型"],
    ["result_index", "结果位置"],
    ["created_at", "提交", stamp],
  ],
  receipt_cursors: [
    ["initiative_run_id", "自主轮"],
    ["canonical_space_id", "Space"],
    ["last_receipt_id", "已处理工具回执水位"],
  ],
};

export function Reflection(props: PageProps) {
  const health = useQuery<Row>(
    "read_self_reflection_health",
    {},
    props.refresh,
    props.allowed("read_self_reflection_health"),
  );
  const fields = health.data?.fields as Row | undefined;
  const [section, selectSection] = useState("runs");
  const [ownerType, setOwnerType] = useState("");
  const [owner, setOwner] = useState("");
  const [cycle, setCycle] = useState("");
  const [run, setRun] = useState("");
  const [scope, selectScope] = useState<Row>({});
  return (
    <>
      <Section title="Self Reflection · 原运行统计">
        {health.error != null && <ErrorNote error={health.error} />}
        {health.loading && <Empty>正在读取…</Empty>}
        {fields && (
          <>
            <Table
              rows={[fields]}
              columns={[
                ["calls_today", "今日实际请求"],
                ["daily_limit", "每日原预算"],
                ["ingress_events_total", "累计流入事件"],
                ["processed_events_total", "累计处理聊天事件"],
                ["oldest_actionable_age_seconds", "最久可处理等待秒数"],
                ["ingress_events_per_hour", "流入每小时"],
                ["drain_events_per_hour", "排出每小时"],
              ]}
            />
            <Table
              rows={[
                "actionable",
                "waiting_retry",
                "isolated",
                "policy_ineligible",
                "recent_not_due",
                "processing",
              ].map((name) => ({ name, ...(fields[name] as Row) }))}
              columns={[
                ["name", "类别"],
                ["events", "聊天事件"],
                ["conversations", "范围数"],
              ]}
            />
            <p className="small">
              同一主体的不同范围可落入多个类别；范围数不能相加当作去重会话数。观察不足
              60 秒，速率保留未知。工具证据按独立回执水位统计。
            </p>
            <JsonNote
              value={{
                initiative_tools: fields.initiative_tools,
                rate_window_seconds: fields.rate_window_seconds,
                three_cycles_without_decrease:
                  fields.three_cycles_without_decrease,
                last_24h: fields.last_24h,
                provider_requests: fields.provider_requests,
                report_deliveries: fields.report_deliveries,
              }}
              title="实际请求、工具积压和最近 24 小时统计"
            />
          </>
        )}
      </Section>
      <Section title="自省水位与完整历史">
        <form
          className="schema-grid"
          onSubmit={(e) => {
            e.preventDefault();
            const next: Row = {};
            if (ownerType && owner.trim()) next[ownerType] = owner.trim();
            if (!["states", "receipt_cursors"].includes(section)) {
              if (cycle.trim()) next.cycle_id = cycle.trim();
              if (run) next.run_id = Number(run);
            }
            selectScope(next);
          }}
        >
          <label className="form-group">
            历史集合
            <select
              className="form-control"
              value={section}
              onChange={(e) => {
                selectSection(e.target.value);
                selectScope({});
                if (
                  e.target.value === "receipt_cursors" &&
                  ownerType === "person_id"
                ) {
                  setOwnerType("");
                  setOwner("");
                }
              }}
            >
              {[
                ["runs", "自省批次"],
                ["cycles", "后台周期"],
                ["states", "聊天事件水位"],
                ["receipt_cursors", "自主工具回执水位"],
                ["requests", "实际 Provider 请求"],
                ["results", "已落盘结果"],
              ].map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="form-group">
            主体类别
            <select
              className="form-control"
              value={ownerType}
              onChange={(e) => {
                setOwnerType(e.target.value);
                setOwner("");
              }}
            >
              <option value="">全部</option>
              {section !== "receipt_cursors" && (
                <option value="person_id">Person</option>
              )}
              <option value="space_id">Space</option>
            </select>
          </label>
          {ownerType && (
            <label className="form-group">
              内部主体 UUID
              <input
                className="form-control"
                required
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
              />
            </label>
          )}
          {!["states", "receipt_cursors"].includes(section) && (
            <>
              <label className="form-group">
                周期 ID
                <input
                  className="form-control"
                  value={cycle}
                  onChange={(e) => setCycle(e.target.value)}
                />
              </label>
              <label className="form-group">
                批次 ID
                <input
                  className="form-control"
                  type="number"
                  min="1"
                  step="1"
                  value={run}
                  onChange={(e) => setRun(e.target.value)}
                />
              </label>
            </>
          )}
          <button className="btn-secondary">应用全库筛选</button>
        </form>
        {props.allowed("list_self_reflection_history") && (
          <QueryList
            method="list_self_reflection_history"
            args={{ section, scope }}
            refresh={props.refresh}
            onRow={flatten}
            columns={columns[section]}
            actions={(row) =>
              section === "cycles" ? (
                <button
                  className="btn-secondary"
                  onClick={() => {
                    selectSection("runs");
                    setCycle(String(row.id));
                    setRun("");
                    selectScope({ cycle_id: String(row.id) });
                  }}
                >
                  查看本周期批次
                </button>
              ) : section === "runs" ? (
                <button
                  className="btn-secondary"
                  onClick={() => {
                    selectSection("requests");
                    setRun(String(row.id));
                    setCycle("");
                    selectScope({ run_id: Number(row.id) });
                  }}
                >
                  查看实际请求
                </button>
              ) : undefined
            }
          />
        )}
      </Section>
    </>
  );
}
