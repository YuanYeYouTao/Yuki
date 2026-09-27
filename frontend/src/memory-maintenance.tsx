import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import {
  Badge,
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
} from "./components";
import { SchemaFields } from "./schema-fields";
import { stamp } from "./format";

function RebuildForm({ schema, props }: { schema: Row; props: PageProps }) {
  const [selection, setSelection] = useState<Row>({
    all_events: true,
    maximum_events: 100,
  });
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        props.act({
          method: "rebuild_memory",
          label: "规划记忆重建",
          revision: 0,
          payload: { action: "plan", resource_id: "yuki", spec: selection },
          review: selection,
          hint: "仅创建原历史快照与计划；读取计划后再开始提取。",
        });
      }}
    >
      <SchemaFields
        schema={schema}
        root={schema}
        values={selection}
        change={setSelection}
        prefix="memory-rebuild"
        labels={{
          all_events: "允许完整历史",
          minimum_event_id: "起始内部事件 ID",
          maximum_event_id: "截止内部事件 ID",
          maximum_events: "最多事件",
          after: "起始时间",
          before: "截止时间",
          third_party_mode: "第三方记忆策略",
          expired_claim_policy: "过期声明策略",
          include_failed_live_jobs: "包含失败实时任务",
          bot_user_ids: "接入账号筛选",
          sender_user_ids: "历史发送者筛选",
          group_ids: "历史群来源筛选",
          scope_types: "历史会话类型",
        }}
      />
      <button
        className="btn-primary"
        disabled={!props.allowed("rebuild_memory")}
      >
        检查并规划重建
      </button>
    </form>
  );
}

export function MemoryMaintenance({ props }: { props: PageProps }) {
  const [planning, setPlanning] = useState(false),
    [kind, setKind] = useState("rebuild"),
    [selected, setSelected] = useState("");
  const schema = useQuery<Row>(
    "read_memory_maintenance_schema",
    {},
    props.refresh,
    planning && props.allowed("read_memory_maintenance_schema"),
  );
  const detail = useQuery<Row>(
    "read_memory_maintenance_run",
    { operation_id: selected },
    props.refresh,
    !!selected && props.allowed("read_memory_maintenance_run"),
  );
  const fields = detail.data?.fields as Row | undefined;
  return (
    <Section title="记忆维护">
      <div className="settings-actions">
        <button
          className="btn-secondary"
          disabled={!props.allowed("rebuild_memory")}
          onClick={() => setPlanning(!planning)}
        >
          规划重建
        </button>
        <button
          className="btn-secondary"
          disabled={!props.allowed("dream_memory")}
          onClick={() =>
            props.act({
              method: "dream_memory",
              label: "规划梦境整理",
              revision: 0,
              payload: { action: "plan", resource_id: "yuki" },
              review: "创建原梦境整理计划；读取原计划后再开始。",
            })
          }
        >
          规划梦境整理
        </button>
        <button
          className="btn-secondary"
          disabled={!props.allowed("maintain_memory")}
          onClick={() =>
            props.act({
              method: "maintain_memory",
              label: "维护索引",
              revision: 0,
              payload: { action: "run", resource_id: "yuki" },
              review: "运行原索引维护 worker 的单次维护；不会创建第二套索引。",
            })
          }
        >
          维护索引
        </button>
      </div>
      {planning && (
        <>
          <h3>重建范围与策略</h3>
          {schema.error != null && <ErrorNote error={schema.error} />}
          {schema.loading && <Empty>正在读取原维护 schema…</Empty>}
          {schema.data && (
            <RebuildForm
              schema={(schema.data.fields as Row).rebuild as Row}
              props={props}
            />
          )}
        </>
      )}
      <h3>原计划与执行记录</h3>
      <label className="form-group">
        维护类型
        <select
          className="form-control"
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setSelected("");
          }}
        >
          <option value="rebuild">历史重建</option>
          <option value="dream">梦境整理</option>
        </select>
      </label>
      {props.allowed("list_operations") && (
        <QueryList
          method="list_operations"
          args={{ kind }}
          refresh={props.refresh}
          columns={[
            ["operation_id", "原计划"],
            ["status", "状态", (v) => <Badge value={v} />],
            ["updated_at", "更新", stamp],
            ["error_category", "原因"],
          ]}
          actions={(row) => (
            <button
              className="btn-secondary"
              disabled={!props.allowed("read_memory_maintenance_run")}
              onClick={() => setSelected(String(row.operation_id))}
            >
              范围与执行详情
            </button>
          )}
        />
      )}
      {detail.error != null && <ErrorNote error={detail.error} />}
      {fields && (
        <>
          <h3>{selected}</h3>
          <JsonNote title="实际水位、额度与审核统计" value={fields} />
          {kind === "rebuild" && (
            <>
              <div className="row-actions">
                {[
                  ["pause", "暂停原执行", ["extracting", "committing"]],
                  [
                    "resume",
                    "继续原执行",
                    ["extraction_paused", "commit_paused"],
                  ],
                  ["commit", "提交已审核候选", ["review"]],
                ].map(
                  ([action, label, statuses]) =>
                    (statuses as string[]).includes(String(fields.status)) && (
                      <button
                        key={String(action)}
                        className="btn-secondary"
                        disabled={
                          !props.allowed("rebuild_memory") ||
                          (action === "commit" &&
                            Number(
                              (fields.review_counts as Row)?.pending || 0,
                            ) > 0)
                        }
                        onClick={() =>
                          props.act({
                            method: "rebuild_memory",
                            label: String(label),
                            revision: Number(fields.revision),
                            payload: { action, resource_id: fields.public_id },
                            review: {
                              operation_id: selected,
                              status: fields.status,
                              review_counts: fields.review_counts,
                            },
                          })
                        }
                      >
                        {String(label)}
                      </button>
                    ),
                )}
              </div>
              <h3>原重建候选、审核与实际提交结果</h3>
              {props.allowed("list_memory_rebuild_proposals") && (
                <QueryList
                  method="list_memory_rebuild_proposals"
                  args={{ run_id: fields.public_id, include_content: true }}
                  refresh={props.refresh}
                  onRow={(row) => ({ ...row, ...((row.fields as Row) || {}) })}
                  columns={[
                    ["id", "候选"],
                    [
                      "event_id",
                      "内部来源",
                      (v) => (
                        <a
                          href={`#audit?event=${encodeURIComponent(String(v))}`}
                        >
                          事件 {String(v)}
                        </a>
                      ),
                    ],
                    ["kind", "类型"],
                    ["content", "内容"],
                    ["evidence_quote", "原证据"],
                    ["review_status", "审核", (v) => <Badge value={v} />],
                    ["commit_status", "提交", (v) => <Badge value={v} />],
                    ["actual_fact_id", "实际记忆"],
                    ["error_category", "错误"],
                  ]}
                  actions={(row) => (
                    <>
                      {["approve", "reject"].map((action) => (
                        <button
                          key={action}
                          className="btn-secondary"
                          disabled={
                            !props.allowed("rebuild_memory") ||
                            fields.status !== "review" ||
                            row.review_status !== "pending" ||
                            !row.content_visible
                          }
                          onClick={() =>
                            props.act({
                              method: "rebuild_memory",
                              label:
                                action === "approve"
                                  ? "批准原候选"
                                  : "拒绝原候选",
                              revision: Number(fields.revision),
                              payload: {
                                action,
                                resource_id: fields.public_id,
                                spec: { proposal_ids: [Number(row.id)] },
                              },
                              review: {
                                proposal_id: row.id,
                                event_id: row.event_id,
                                content: row.content,
                                evidence: row.evidence_quote,
                              },
                            })
                          }
                        >
                          {action === "approve" ? "批准" : "拒绝"}
                        </button>
                      ))}
                    </>
                  )}
                />
              )}
            </>
          )}
          {((fields.status === "failed" && kind === "rebuild") ||
            (fields.status === "partial_failed" && kind === "dream")) && (
            <button
              className="btn-secondary"
              disabled={!props.allowed("retry_operation")}
              onClick={() =>
                props.act({
                  method: "retry_operation",
                  label:
                    kind === "rebuild"
                      ? "将原失败项恢复为暂停，随后显式续跑"
                      : "重试原失败簇（保留预算与成功簇）",
                  revision: Number(fields.revision),
                  payload: { action: "retry", resource_id: selected },
                  review: fields,
                })
              }
            >
              重试失败原计划
            </button>
          )}
          <div className="settings-actions">
            {["planned"].includes(String(fields.status)) && (
              <button
                className="btn-secondary"
                disabled={
                  !props.allowed(
                    kind === "rebuild" ? "rebuild_memory" : "dream_memory",
                  )
                }
                onClick={() =>
                  props.act({
                    method:
                      kind === "rebuild" ? "rebuild_memory" : "dream_memory",
                    label: "开始原计划",
                    revision: Number(fields.revision),
                    payload: {
                      action: "start",
                      resource_id: String(fields.public_id),
                    },
                    review: { operation_id: selected, status: fields.status },
                  })
                }
              >
                开始原计划
              </button>
            )}
            {!["completed", "cancelled", "rolled_back"].includes(
              String(fields.status),
            ) && (
              <button
                className="btn-secondary"
                disabled={
                  !props.allowed(
                    kind === "rebuild" ? "rebuild_memory" : "dream_memory",
                  )
                }
                onClick={() =>
                  props.act({
                    method:
                      kind === "rebuild" ? "rebuild_memory" : "dream_memory",
                    label: "取消原计划",
                    revision: Number(fields.revision),
                    payload: {
                      action: "cancel",
                      resource_id: String(fields.public_id),
                    },
                    review: selected,
                  })
                }
              >
                取消原计划
              </button>
            )}
          </div>
        </>
      )}
      <h3>实时记忆任务</h3>
      {props.allowed("list_memory_jobs") && (
        <QueryList
          method="list_memory_jobs"
          refresh={props.refresh}
          columns={[
            ["job_id", "任务"],
            ["kind", "类型"],
            ["status", "状态", (v) => <Badge value={v} />],
            ["operation", "进度"],
          ]}
        />
      )}
    </Section>
  );
}
