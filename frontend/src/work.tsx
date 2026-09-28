import { useState } from "react";
import { AutomationHistory } from "./automation-history";
import { AutomationEditor } from "./automation-editor";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { stamp, text } from "./format";
import { useDisplayNames } from "./names";
import {
  Badge,
  type Column,
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
} from "./components";
const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const status = (value: unknown) => <Badge value={value} />;

function WorkActions({
  workId,
  fields,
  allowed,
  act,
}: {
  workId: string;
  fields: Row;
  allowed: PageProps["allowed"];
  act: PageProps["act"];
}) {
  if (
    !allowed("mutate_work") ||
    ["completed", "failed", "cancelled"].includes(String(fields.state))
  )
    return null;
  const submit = (action: string, label: string) =>
    act({
      method: "mutate_work",
      label,
      revision: Number(fields.revision),
      payload: { resource_id: workId, action },
      hint:
        action === "cancel"
          ? "停止这个工作及其子工作。已发出的效果保留原回执，其他工作继续。"
          : "续原工作，不重置预算或重发已有效果。等待、未知效果和原恢复边界仍由执行层核验。",
    });
  return (
    <div className="settings-actions">
      {["waiting_user", "suspended"].includes(String(fields.state)) && (
        <button
          className="btn-secondary"
          onClick={() => submit("resume", "续跑原工作")}
        >
          续跑原工作
        </button>
      )}
      <button
        className="btn-secondary"
        onClick={() => submit("cancel", "取消工作树")}
      >
        取消工作树
      </button>
    </div>
  );
}

function WorkDetail({
  workId,
  allowed,
  act,
  refresh,
  selectWork,
}: {
  workId: string;
  allowed: PageProps["allowed"];
  act: PageProps["act"];
  refresh: number;
  selectWork: (id: string) => void;
}) {
  const detail = useQuery<Row>(
    "read_work",
    { work_id: workId, include_content: allowed("read_execution_trace") },
    refresh,
    allowed("read_work"),
  );
  const fields = detail.data?.fields as Row | undefined;
  const names = useDisplayNames({
    conversation: [String(fields?.conversation_id || "")],
  });
  return (
    <Section title="工作详情">
      {detail.error != null && <ErrorNote error={detail.error} />}
      {detail.loading && <Empty>正在读取工作详情…</Empty>}
      {fields && (
        <>
          <div className="metric-grid">
            {[
              ["状态", fields.state],
              ["原因", fields.reason],
              ["模型请求", fields.model_requests],
              ["工具调用", fields.tool_calls],
              ["已发消息", fields.sent_messages],
              ["更新于", stamp(fields.updated)],
            ].map(([name, value]) => (
              <div className="vital-card" key={String(name)}>
                <span className="small">{String(name)}</span>
                <strong>{text(value)}</strong>
              </div>
            ))}
          </div>
          {fields.goal != null && (
            <pre className="persona-text">{String(fields.goal)}</pre>
          )}
          <p>会话：{names[String(fields.conversation_id)] || "未命名会话"}</p>
          <details className="reference-id">
            <summary>内部工作编号</summary>
            工作：{workId} · 根工作：{String(fields.root_id)} · Generation：
            {String(fields.generation)}
          </details>
          <a
            className="file-open"
            href={`#audit?work=${encodeURIComponent(workId)}`}
          >
            完整执行轨迹
          </a>
          <JsonNote title="共享累计预算" value={fields.shared_budget} />
          <JsonNote title="原检查点状态" value={fields.journal} />
          <JsonNote title="恢复调度状态" value={fields.recovery} />
          <WorkActions
            workId={workId}
            fields={fields}
            allowed={allowed}
            act={act}
          />
          {[
            [
              "waits",
              "信号等待",
              [
                ["id", "原等待 ID"],
                ["mode", "方式"],
                ["status", "状态", status],
                ["deadline", "截止", stamp],
                ["created", "登记时间", stamp],
                [
                  "conditions",
                  "条件",
                  (value: unknown) =>
                    value == null ? (
                      "未读取正文"
                    ) : (
                      <JsonNote title="条件及满足状态" value={value} />
                    ),
                ],
              ],
            ],
            [
              "children",
              "子工作",
              [
                ["id", "Work"],
                ["state", "状态", status],
                ["reason", "原因"],
                ["model_requests", "模型请求"],
                ["tool_calls", "工具调用"],
                ["updated", "更新", stamp],
              ],
            ],
            [
              "inputs",
              "接纳输入",
              [
                ["id", "内部输入 ID"],
                ["event_id", "内部事件 ID"],
                ["kind", "类型"],
                ["state", "状态", status],
                ["ready", "准备完成", status],
                ["created", "时间", stamp],
              ],
            ],
            [
              "effects",
              "业务效果回执",
              [
                ["effect_key", "原效果键"],
                ["kind", "类型"],
                ["state", "状态", status],
                ["updated", "时间", stamp],
              ],
            ],
            [
              "deliveries",
              "投递意图",
              [
                ["id", "原投递 ID"],
                ["kind", "类型"],
                ["state", "状态", status],
                ["message_count", "消息数"],
                ["not_before", "最早执行", stamp],
                ["updated", "时间", stamp],
              ],
            ],
          ].map(([section, label, columns]) => (
            <div key={String(section)} role="region" aria-label={String(label)}>
              <h3>{String(label)}</h3>
              {allowed("list_work_history") && (
                <QueryList
                  key={`${workId}:${section}`}
                  method="list_work_history"
                  args={{
                    work_id: workId,
                    section,
                    include_content:
                      section === "waits" && allowed("read_execution_trace"),
                  }}
                  refresh={refresh}
                  onRow={flatten}
                  columns={columns as Column[]}
                  actions={
                    section === "children"
                      ? (row) => (
                          <button
                            className="btn-secondary"
                            onClick={() => selectWork(String(row.id))}
                          >
                            工作详情
                          </button>
                        )
                      : undefined
                  }
                />
              )}
            </div>
          ))}
          <p className="small">
            历史按登记时间分页。请求正文、工具结果与投递详情沿原执行轨迹查看。
          </p>
        </>
      )}
    </Section>
  );
}
export function Work({ allowed, act, refresh, conversation }: PageProps) {
  const props = { allowed, act, refresh, conversation };
  const [editing, setEditing] = useState<"create" | "update" | null>(null);
  const [workId, selectWork] = useState<string | null>(null);
  const [id, setId] = useState<number | null>(null);
  const detail = useQuery<Row>(
    "read_automation",
    { automation_id: id },
    refresh,
    id != null && allowed("read_automation"),
  );
  const fields = detail.data?.fields as Row | undefined;
  function automationAction(row: Row, action: string, label: string) {
    act({
      method: "mutate_automation",
      label,
      revision: Number(row.revision),
      payload: { resource_id: String(row.automation_id), action },
    });
  }
  return (
    <>
      <Section title="持久工作">
        <QueryList
          method="list_work"
          args={{ include_content: allowed("read_execution_trace") }}
          refresh={refresh}
          onRow={flatten}
          columns={[
            [
              "resource_id",
              "工作",
              (value) => (
                <details className="reference-id">
                  <summary>工作记录</summary>
                  {text(value)}
                </details>
              ),
            ],
            ["conversation_id", "会话"],
            ["goal", "目标"],
            ["state", "状态", status],
            ["model_requests", "模型请求"],
            ["tool_calls", "工具调用"],
            ["active_seconds", "活跃秒数"],
            ["sent_messages", "已发消息"],
            ["reason", "原因"],
          ]}
          actions={(row) => (
            <>
              <button
                className="btn-secondary"
                disabled={!allowed("read_work")}
                onClick={() => selectWork(String(row.resource_id))}
              >
                工作详情
              </button>
              <a
                className="file-open"
                href={`#audit?work=${encodeURIComponent(String(row.resource_id))}`}
              >
                执行轨迹
              </a>
            </>
          )}
        />
      </Section>
      {workId && (
        <WorkDetail
          key={workId}
          workId={workId}
          allowed={allowed}
          act={act}
          refresh={refresh}
          selectWork={selectWork}
        />
      )}
      <Section title="定时与自动化">
        <div className="settings-actions">
          <button
            className="btn-primary"
            disabled={!allowed("mutate_automation")}
            onClick={() => setEditing("create")}
          >
            新建
          </button>
        </div>
        <QueryList
          method="list_automations"
          refresh={refresh}
          columns={[
            ["automation_id", "编号"],
            ["name", "名称"],
            ["status", "状态", status],
            ["run_count", "已执行"],
            ["target_kind", "投递类型"],
            ["target_id", "投递目标"],
            ["route_state", "路由状态"],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <>
              <button
                className="btn-secondary"
                disabled={
                  !allowed("read_automation") &&
                  !allowed("list_automation_runs")
                }
                onClick={() => {
                  setId(Number(row.automation_id));
                  setEditing(null);
                }}
              >
                详情
              </button>
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_automation")}
                onClick={() =>
                  automationAction(
                    row,
                    row.status === "paused" ? "resume" : "pause",
                    row.status === "paused" ? "恢复任务" : "暂停任务",
                  )
                }
              >
                {row.status === "paused" ? "恢复" : "暂停"}
              </button>
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_automation")}
                onClick={() => automationAction(row, "run_now", "立即执行")}
              >
                执行
              </button>
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_automation")}
                onClick={() => automationAction(row, "cancel", "取消自动化")}
              >
                取消
              </button>
            </>
          )}
        />
      </Section>
      {editing === "create" && (
        <AutomationEditor props={props} close={() => setEditing(null)} />
      )}
      {id != null && (
        <Section title={`自动化 #${id}`}>
          {detail.error != null && <ErrorNote error={detail.error} />}
          {fields && (
            <>
              <div className="vital-pair">
                <div className="vital-card">
                  下次执行
                  <br />
                  {stamp(fields.next_run_at)}
                </div>
                <div className="vital-card">
                  连续失败
                  <br />
                  {text(fields.consecutive_failures)}
                </div>
              </div>
              <JsonNote title="时间安排" value={fields.schedule} />
              <JsonNote title="脚本" value={fields.script} />
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_automation")}
                onClick={() => setEditing("update")}
              >
                编辑脚本
              </button>
              {editing === "update" && fields.script != null && (
                <AutomationEditor
                  key={`${id}:${fields.revision}`}
                  automationId={id}
                  creatorKind={String(fields.creator_kind)}
                  revision={Number(fields.revision)}
                  initial={fields.script as Row}
                  props={props}
                  close={() => setEditing(null)}
                />
              )}
            </>
          )}
          <AutomationHistory
            key={id}
            automationId={id}
            allowed={allowed}
            refresh={refresh}
          />
        </Section>
      )}
    </>
  );
}
