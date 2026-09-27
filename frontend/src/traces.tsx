import { stamp, text } from "./format";
import { useQuery } from "./hooks";
import { useState } from "react";
import {
  Badge,
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
} from "./components";
import type { Row } from "./api";
import { displayTrace } from "./trace-display";

export function TraceContent({ row }: { row: Row }) {
  const evidence = row.payload as Row | null;
  if (!evidence)
    return (
      <Empty>
        {row.payload_status === "omitted_size"
          ? "正文超过记录上限，仅保留索引。"
          : "这条记录没有可读取的正文。"}
      </Empty>
    );
  const { prompts, reasoning, replies } = displayTrace(evidence);
  return (
    <>
      {prompts.length > 0 && (
        <div className="prompt-messages">
          {prompts.map((message, i) => (
            <article key={i} className="paper-note">
              <strong>{message.role}</strong>
              <pre>
                {typeof message.content === "string"
                  ? message.content
                  : JSON.stringify(message.content, null, 2)}
              </pre>
            </article>
          ))}
        </div>
      )}
      {reasoning.length > 0 && (
        <details className="reasoning">
          <summary>模型返回的可读思考</summary>
          <pre>{reasoning.join("\n\n")}</pre>
        </details>
      )}
      {replies.length > 0 && (
        <pre className="file-preview">{replies.join("\n\n")}</pre>
      )}
      <JsonNote title="完整诊断记录" value={evidence} />
    </>
  );
}
export function TraceReader({ id }: { id: number }) {
  const { data, error, loading } = useQuery<Row>("read_execution_trace", {
    entry_id: id,
  });
  return (
    <Section title={`记录 #${id}`}>
      {loading && <Empty>正在读取…</Empty>}
      {error ? (
        <ErrorNote error={error} />
      ) : (
        data && (
          <>
            <p className="small">
              {stamp(data.created_at)} · {text(data.kind)} ·{" "}
              {text(data.payload_status)} · 有效至 {stamp(data.expires_at)}
            </p>
            <TraceContent row={data} />
          </>
        )
      )}
    </Section>
  );
}
export function Traces({
  scope = {},
  refresh = 0,
}: {
  scope?: Row;
  refresh?: number;
}) {
  const [selected, setSelected] = useState<number | null>(null),
    [turn, setTurn] = useState(""),
    [draft, setDraft] = useState("");
  return (
    <>
      <Section title="执行轨迹">
        <form
          className="search-line"
          onSubmit={(e) => {
            e.preventDefault();
            setTurn(draft.trim());
            setSelected(null);
          }}
        >
          <input
            className="form-control"
            aria-label="轮次编号"
            placeholder="按完整轮次编号查询…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button className="btn-secondary">查找</button>
        </form>
        <p className="section-caption">
          点击轮次查看该轮所有步骤；正文需要单独的内容读取权限。诊断记录超过保留期限后不再显示。
        </p>
        <QueryList
          key={JSON.stringify(scope) + turn}
          method="list_execution_trace"
          args={{
            scope: {
              ...scope,
              ...(turn ? { turn_id: turn } : {}),
              descending: !turn,
            },
          }}
          refresh={refresh}
          columns={[
            ["created_at", "时间", (v) => stamp(v)],
            ["kind", "步骤"],
            ["origin", "来源"],
            [
              "turn_id",
              "轮次",
              (v) => (
                <button
                  className="file-open"
                  onClick={() => {
                    setTurn(String(v));
                    setDraft(String(v));
                    setSelected(null);
                  }}
                >
                  {text(v)}
                </button>
              ),
            ],
            ["parent_operation_id", "父步骤"],
            ["payload_status", "正文", (v) => <Badge value={v} />],
          ]}
          actions={(row) => (
            <button
              className="btn-secondary"
              onClick={() => setSelected(Number(row.id))}
            >
              查看 #{text(row.id)}
            </button>
          )}
        />
      </Section>
      {selected != null && <TraceReader id={selected} />}
    </>
  );
}
