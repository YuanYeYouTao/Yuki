import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { OwnerPicker } from "./owner-picker";
import { useQuery } from "./hooks";
import {
  Badge,
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
  Table,
} from "./components";
import { stamp, text } from "./format";
import { Traces } from "./traces";
import { Relationships } from "./relationships";
import { Reflection } from "./reflection";
import { MemoryMaintenance } from "./memory-maintenance";

const choices: Record<string, [string, string][]> = {
  scope_type: [
    ["person", "人物"],
    ["person_group", "人物在群中"],
    ["group", "群空间"],
    ["self", "Yuki 自身"],
  ],
  visibility_type: [
    ["global", "全局"],
    ["private", "私聊可见"],
    ["group", "群可见"],
  ],
  kind: [
    ["fact", "事实"],
    ["preference", "偏好"],
    ["episode", "经历"],
  ],
  status: [
    ["active", "有效"],
    ["contested", "有争议"],
    ["superseded", "已替代"],
    ["invalidated", "已失效"],
  ],
  review_state: [
    ["verified", "已验证"],
    ["quarantined", "已隔离"],
    ["legacy_unreviewed", "历史未审核"],
  ],
};
const labels: Record<string, string> = {
  scope_type: "记忆归属",
  visibility_type: "SELF 可见范围",
  kind: "类型",
  status: "状态",
  review_state: "审核状态",
};
const embeddingState: Record<string, string> = {
  ok: "本地向量索引覆盖完整；此页不探测远端服务",
  degraded: "部分记忆尚未建好向量，检索会降级",
  not_configured: "缺少 Embedding 地址或 API Key，当前使用全文检索",
  disabled: "已关闭，当前使用全文检索",
  unavailable: "向量状态暂不可读取",
};

function FactDetails({ id, props }: { id: number; props: PageProps }) {
  const result = useQuery<Row>(
    "read_memory_fact",
    { fact_id: id },
    props.refresh,
    props.allowed("read_memory_fact"),
  );
  const fields = result.data?.fields as Row | undefined;
  return (
    <Section title={`记忆 #${id}`}>
      {result.error != null && <ErrorNote error={result.error} />}
      {result.loading && <Empty>正在读取…</Empty>}
      {fields && (
        <>
          <Table
            rows={[fields]}
            columns={[
              ["scope_type", "范围"],
              ["kind", "类型"],
              ["status", "状态", (v) => <Badge value={v} />],
              ["review_state", "审核", (v) => <Badge value={v} />],
              ["importance", "重要性"],
              ["confidence", "可信度"],
            ]}
          />
          {fields.content != null ? (
            <pre className="memory-body">{text(fields.content)}</pre>
          ) : (
            <Empty>当前账号只可查看元数据，未读取正文。</Empty>
          )}
          <p className="small">
            记忆更新 {stamp(fields.updated_at)} · 来源{" "}
            {text(fields.source_type)} · 权威 {text(fields.authority)}
          </p>
          <JsonNote value={fields} title="归属、版本与质量元数据" />
          <div className="settings-actions">
            {["confirm", "quarantine"].map((action) => (
              <button
                key={action}
                className="btn-secondary"
                disabled={!props.allowed("mutate_memory")}
                onClick={() =>
                  props.act({
                    method: "mutate_memory",
                    label: action === "confirm" ? "确认记忆" : "隔离记忆",
                    revision: Number(fields.revision),
                    payload: { action, resource_id: String(id) },
                  })
                }
              >
                {action === "confirm" ? "确认" : "隔离"}
              </button>
            ))}
          </div>
        </>
      )}
    </Section>
  );
}

function MemoryFacts(props: PageProps) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [scope, setScope] = useState<Row>({});
  const [selected, select] = useState<number | null>(null);
  const [trace, showTrace] = useState<Row | null>(null);
  const health = useQuery<Row>(
    "read_memory_health",
    {},
    props.refresh,
    props.allowed("read_memory_health"),
  );
  const update = (key: string, value: string) =>
    setDraft({ ...draft, [key]: value });
  const self = draft.scope_type === "self";
  const ownerInputs: [string, string][] = self
    ? draft.visibility_type === "private"
      ? [["visibility_person_id", "可见人物"]]
      : draft.visibility_type === "group"
        ? [["visibility_space_id", "可见群"]]
        : []
    : draft.scope_type === "person"
      ? [["person_id", "记忆人物"]]
      : draft.scope_type === "group"
        ? [["space_id", "记忆群"]]
        : [
            ["person_id", "记忆人物"],
            ["space_id", "记忆群"],
          ];
  return (
    <>
      <Section title="记忆健康">
        {health.error != null && <ErrorNote error={health.error} />}
        {health.loading && <Empty>正在读取…</Empty>}
        {health.data && (
          <>
            <Table
              rows={[health.data]}
              columns={[
                ["index", "全文索引"],
                [
                  "embedding",
                  "向量状态",
                  (value) => embeddingState[String(value)] || text(value),
                ],
                ["consistency", "一致性"],
              ]}
            />
            <p className="small">
              向量覆盖 {text(health.data.embedding_ready_count ?? "?")} /{" "}
              {text(health.data.embedding_fact_count ?? "?")} 条有效记忆 ·
              失败任务 {text(health.data.embedding_failed_jobs ?? "?")}
              {health.data.embedding_last_error
                ? ` · 最近错误 ${text(health.data.embedding_last_error)}`
                : ""}
            </p>
            {health.data.embedding_pending_restart && (
              <p className="small">向量检索开关已保存，重启 Bot 后生效。</p>
            )}
            {health.data.embedding === "not_configured" && (
              <p className="small">
                开关默认开启；需在服务器配置 MEMORY_EMBEDDING_BASE_URL 和
                MEMORY_EMBEDDING_API_KEY，重启后才会建立向量索引。
              </p>
            )}
            {health.data.embedding_saved_enabled != null && (
              <button
                className="btn-secondary"
                disabled={!props.allowed("set_config")}
                onClick={() =>
                  props.act({
                    method: "set_config",
                    label: health.data?.embedding_saved_enabled
                      ? "关闭记忆向量检索"
                      : "启用记忆向量检索",
                    revision: Number(
                      health.data?.embedding_config_version || 0,
                    ),
                    payload: {
                      key: "memory.embedding_enabled",
                      scope_type: "global",
                      scope_id: "",
                      value: !health.data?.embedding_saved_enabled,
                    },
                  })
                }
              >
                {health.data.embedding_saved_enabled
                  ? "关闭向量检索"
                  : "启用向量检索"}
              </button>
            )}
          </>
        )}
      </Section>
      <Section title="长期记忆">
        <form
          className="schema-grid"
          onSubmit={(e) => {
            e.preventDefault();
            const next: Row = {};
            for (const key of [
              "scope_type",
              "kind",
              "status",
              "review_state",
              ...(self ? ["visibility_type"] : []),
            ])
              if (draft[key]) next[key] = draft[key];
            for (const [key] of ownerInputs)
              if (draft[key]?.trim()) next[key] = draft[key].trim();
            for (const key of ["fact_id", "event_id", "tool_receipt_id"])
              if (draft[key]) next[key] = Number(draft[key]);
            setScope(next);
            select(null);
            showTrace(null);
          }}
        >
          {[
            "scope_type",
            "kind",
            "status",
            "review_state",
            ...(self ? ["visibility_type"] : []),
          ].map((key) => (
            <label className="form-group" key={key}>
              {labels[key]}
              <select
                className="form-control"
                value={draft[key] || ""}
                onChange={(e) => {
                  if (key === "scope_type")
                    setDraft({
                      ...draft,
                      scope_type: e.target.value,
                      person_id: "",
                      space_id: "",
                      visibility_type: "",
                      visibility_person_id: "",
                      visibility_space_id: "",
                    });
                  else if (key === "visibility_type")
                    setDraft({
                      ...draft,
                      visibility_type: e.target.value,
                      visibility_person_id: "",
                      visibility_space_id: "",
                    });
                  else update(key, e.target.value);
                }}
              >
                <option value="">全部</option>
                {choices[key].map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          ))}
          {ownerInputs.map(([key, label]) => (
            <label className="form-group" key={key}>
              {label}
              <OwnerPicker
                kind={key.includes("person") ? "person" : "space"}
                label={label}
                value={draft[key] || ""}
                change={(value) => update(key, value)}
                refresh={props.refresh}
              />
            </label>
          ))}
          {[
            ["fact_id", "内部记忆 ID"],
            ["event_id", "来源事件 ID"],
            ["tool_receipt_id", "工具证据回执 ID"],
          ].map(([key, label]) => (
            <label className="form-group" key={key}>
              {label}
              <input
                className="form-control"
                type="number"
                min="1"
                step="1"
                value={draft[key] || ""}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    [key]: e.target.value,
                    ...(key === "event_id"
                      ? { tool_receipt_id: "" }
                      : key === "tool_receipt_id"
                        ? { event_id: "" }
                        : {}),
                  })
                }
              />
            </label>
          ))}
          <div className="settings-actions">
            <button className="btn-secondary" type="submit">
              应用全库筛选
            </button>
            <button
              className="btn-secondary"
              type="button"
              onClick={() => {
                setDraft({});
                setScope({});
                select(null);
                showTrace(null);
              }}
            >
              清除筛选
            </button>
          </div>
        </form>
        <p className="small">
          按人物或群名称选择范围；SELF
          的可见主体与记忆所有者分别查询。来源事件是
          chat_events.id，工具证据是原持久回执 ID。
        </p>
        {props.allowed("list_memory_facts") ? (
          <QueryList
            method="list_memory_facts"
            args={{ scope }}
            refresh={props.refresh}
            columns={[
              ["fact_id", "编号"],
              ["category", "类别"],
              ["scope_type", "范围"],
              ["person_id", "Person"],
              ["space_id", "Space"],
              ["content", "内容"],
              ["status", "状态", (v) => <Badge value={v} />],
              ["review_state", "审核", (v) => <Badge value={v} />],
            ]}
            actions={(row) => (
              <button
                className="btn-secondary"
                disabled={!props.allowed("read_memory_fact")}
                onClick={() => {
                  select(Number(row.fact_id));
                  showTrace(null);
                }}
              >
                详情与证据 #{text(row.fact_id)}
              </button>
            )}
          />
        ) : (
          <Empty>需要 Memory 元数据读取权限。</Empty>
        )}
      </Section>
      {selected != null && (
        <>
          <button
            className="btn-secondary"
            onClick={() => {
              select(null);
              showTrace(null);
            }}
          >
            返回当前筛选的全部证据
          </button>
          <FactDetails key={selected} id={selected} props={props} />
        </>
      )}
      <Section
        title={selected == null ? "当前范围的证据" : `记忆 #${selected} 的证据`}
      >
        {props.allowed("list_memory_evidence") && (
          <QueryList
            method="list_memory_evidence"
            args={{
              scope: {
                ...scope,
                ...(selected != null ? { fact_id: selected } : {}),
              },
            }}
            refresh={props.refresh}
            columns={[
              ["evidence_id", "证据"],
              ["fact_id", "记忆"],
              ["event_id", "内部事件"],
              ["tool_receipt_id", "工具回执"],
              ["relation", "关系"],
              ["authority", "权威"],
              ["confidence", "可信度"],
              ["excerpt", "原文摘录"],
              ["created_at", "入账时间", stamp],
            ]}
            actions={(row) => (
              <>
                <button
                  className="btn-secondary"
                  onClick={() => {
                    select(Number(row.fact_id));
                    showTrace(null);
                  }}
                >
                  查看记忆 #{text(row.fact_id)}
                </button>
                {(row.event_id != null || row.execution_id != null) && (
                  <button
                    className="btn-secondary"
                    disabled={!props.allowed("list_execution_trace")}
                    onClick={() =>
                      showTrace(
                        row.event_id != null
                          ? { source_event_id: Number(row.event_id) }
                          : { execution_id: String(row.execution_id) },
                      )
                    }
                  >
                    来源执行
                  </button>
                )}
              </>
            )}
          />
        )}
      </Section>
      {trace && (
        <>
          <button className="btn-secondary" onClick={() => showTrace(null)}>
            收起来源执行
          </button>
          <Traces
            scope={trace}
            refresh={props.refresh}
            title="原证据来源的执行轨迹"
          />
        </>
      )}
      <MemoryMaintenance props={props} />
    </>
  );
}

export function Memory(props: PageProps) {
  const [view, selectView] = useState("facts");
  return (
    <>
      <Section title="记忆与关系">
        <div className="settings-actions">
          <button
            className="btn-secondary"
            aria-pressed={view === "facts"}
            onClick={() => selectView("facts")}
          >
            记忆与证据
          </button>
          <button
            className="btn-secondary"
            aria-pressed={view === "relationships"}
            onClick={() => selectView("relationships")}
          >
            人物关系
          </button>
          <button
            className="btn-secondary"
            aria-pressed={view === "reflection"}
            onClick={() => selectView("reflection")}
          >
            自省与水位
          </button>
        </div>
      </Section>
      {view === "reflection" ? (
        <Reflection {...props} />
      ) : view === "facts" ? (
        <MemoryFacts {...props} />
      ) : (
        <Relationships {...props} />
      )}
    </>
  );
}
