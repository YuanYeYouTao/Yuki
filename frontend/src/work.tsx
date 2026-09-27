import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { stamp, text } from "./format";
import {
  Badge,
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
  Table,
} from "./components";
const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const status = (value: unknown) => <Badge value={value} />;

function WorkDetail({
  workId,
  allowed,
  refresh,
}: {
  workId: string;
  allowed: PageProps["allowed"];
  refresh: number;
}) {
  const detail = useQuery<Row>(
    "read_work",
    { work_id: workId, include_content: allowed("read_execution_trace") },
    refresh,
    allowed("read_work"),
  );
  const fields = detail.data?.fields as Row | undefined;
  return (
    <Section title={`Work ${workId}`}>
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
          <p>
            根工作：{String(fields.root_id)} · 会话：
            {String(fields.conversation_id)} · Generation：
            {String(fields.generation)}
          </p>
          <a
            className="file-open"
            href={`#audit?work=${encodeURIComponent(workId)}`}
          >
            完整执行轨迹
          </a>
          <JsonNote title="共享累计预算" value={fields.shared_budget} />
          <JsonNote title="原检查点状态" value={fields.journal} />
          <JsonNote title="恢复调度状态" value={fields.recovery} />
          <h3>信号等待</h3>
          {(fields.waits as Row[]).map((wait) => (
            <div className="paper-note" key={String(wait.id)}>
              <p>
                {String(wait.id)} · {String(wait.mode)} ·{" "}
                <Badge value={wait.status} /> · 截止{" "}
                {wait.deadline == null ? "无期限" : stamp(wait.deadline)}
              </p>
              {wait.conditions != null && (
                <JsonNote title="条件及满足状态" value={wait.conditions} />
              )}
            </div>
          ))}
          {!(fields.waits as Row[]).length && <Empty>没有持久等待绑定。</Empty>}
          {fields.waits_has_more === true && (
            <p className="small">仅显示最近 20 项等待绑定。</p>
          )}
          <h3>子工作</h3>
          <Table
            rows={fields.children as Row[]}
            columns={[
              ["id", "Work"],
              ["state", "状态", status],
              ["reason", "原因"],
              ["model_requests", "模型请求"],
              ["tool_calls", "工具调用"],
              ["updated", "更新时间", stamp],
            ]}
            actions={(row) => (
              <a
                className="file-open"
                href={`#audit?work=${encodeURIComponent(String(row.id))}`}
              >
                执行轨迹
              </a>
            )}
          />
          <h3>接纳输入</h3>
          <Table
            rows={fields.inputs as Row[]}
            columns={[
              ["id", "内部输入 ID"],
              ["event_id", "内部事件 ID"],
              ["kind", "类型"],
              ["state", "状态", status],
              ["ready", "准备完成", status],
              ["created", "时间", stamp],
            ]}
          />
          <h3>业务效果回执</h3>
          <Table
            rows={fields.effects as Row[]}
            columns={[
              ["effect_key", "原效果键"],
              ["kind", "类型"],
              ["state", "状态", status],
              ["updated", "时间", stamp],
            ]}
          />
          <h3>投递意图</h3>
          <Table
            rows={fields.deliveries as Row[]}
            columns={[
              ["id", "原投递 ID"],
              ["kind", "类型"],
              ["state", "状态", status],
              ["message_count", "消息数"],
              [
                "not_before",
                "最早执行",
                (value) => (value == null ? "无延迟" : stamp(value)),
              ],
              ["updated", "时间", stamp],
            ]}
          />
          <p className="small">
            每类最多显示最近 20 项。
            {["children", "inputs", "effects", "deliveries"]
              .filter((name) => fields[`${name}_has_more`])
              .map(
                (name) =>
                  ({
                    children: "子工作",
                    inputs: "输入",
                    effects: "效果",
                    deliveries: "投递",
                  })[name],
              )
              .join("、")}{" "}
            {["children", "inputs", "effects", "deliveries"].some(
              (name) => fields[`${name}_has_more`],
            )
              ? "还有更早的记录。"
              : ""}{" "}
            请求正文、工具结果与投递详情可沿原执行轨迹查看。
          </p>
        </>
      )}
    </Section>
  );
}
export function Work({ allowed, act, refresh, conversation }: PageProps) {
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
            ["resource_id", "Work"],
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
        <WorkDetail workId={workId} allowed={allowed} refresh={refresh} />
      )}
      <Section title="定时与自动化">
        <div className="settings-actions">
          <button
            className="btn-primary"
            disabled={!allowed("mutate_automation")}
            onClick={() =>
              act({
                method: "mutate_automation",
                label: "创建自动化",
                revision: 0,
                payload: {
                  action: "create",
                  resource_id: "yuki",
                  spec: {
                    owner_id: "",
                    conversation_id: conversation || null,
                    script: {},
                  },
                },
                edit: "spec",
                hint: "指定委托人的内部 Person ID，并填写完整脚本。投递目标必须明确声明。",
              })
            }
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
                disabled={!allowed("read_automation")}
                onClick={() => setId(Number(row.automation_id))}
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
                onClick={() =>
                  act({
                    method: "mutate_automation",
                    label: "更新自动化脚本",
                    revision: Number(fields.revision),
                    payload: {
                      action: "update",
                      resource_id: String(id),
                      spec: fields.script,
                    },
                    edit: "spec",
                    hint: "保留原任务的创建者与投递场景，按刚读取的版本验证脚本。",
                  })
                }
              >
                编辑脚本
              </button>
              <h3>最近执行（最多 20 次）</h3>
              <Table
                rows={fields.runs as Row[]}
                columns={[
                  ["id", "执行 ID"],
                  ["status", "状态", status],
                  ["scheduled_for", "计划时间", stamp],
                  ["model_calls", "模型调用"],
                  ["tool_calls", "工具调用"],
                  ["sent_messages", "消息"],
                  ["error_category", "原因"],
                ]}
              />
              <h3>执行步骤（最多 200 项）</h3>
              <Table
                rows={fields.steps as Row[]}
                columns={[
                  ["run_id", "执行 ID"],
                  ["step_id", "步骤"],
                  ["capability", "能力"],
                  ["status", "状态", status],
                  ["error_category", "原因"],
                ]}
              />
            </>
          )}
        </Section>
      )}
    </>
  );
}
