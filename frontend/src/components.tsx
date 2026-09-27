import { useQuery } from "./hooks";
import { stamp, text } from "./format";
import { useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "./api";
import type { Page, Row } from "./api";
export function Icon({ name }: { name: string }) {
  return (
    <img
      className="ui-icon"
      src={`/ui/static/images/icons/${name}.svg`}
      alt=""
    />
  );
}
export function Empty({
  children = "这里还没有记录。",
}: {
  children?: ReactNode;
}) {
  return <div className="empty-note">{children}</div>;
}
export function ErrorNote({ error }: { error: unknown }) {
  return (
    <p className="error-note" role="alert">
      {error instanceof Error ? error.message : "加载失败。"}
      {error instanceof ApiError && error.requestId && (
        <small>请求 {error.requestId}</small>
      )}
    </p>
  );
}
export function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="settings-panel">
      <h2 className="settings-header">{title}</h2>
      <div className="settings-content">{children}</div>
    </section>
  );
}
export function Badge({ value }: { value: unknown }) {
  return (
    <span
      className={`badge ${["true", "running", "active", "completed", "delivered", "connected", "succeeded"].includes(String(value)) ? "good" : ""}`}
    >
      {typeof value === "boolean"
        ? value
          ? "是"
          : "否"
        : String(value ?? "未知")}
    </span>
  );
}
export function JsonNote({ value, title }: { value: unknown; title?: string }) {
  return (
    <details className="json-note" open>
      <summary>{title || "内容"}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
export type Column = [
  string,
  string,
  ((value: unknown, row: Row) => ReactNode)?,
];
export function Table({
  rows,
  columns,
  actions,
}: {
  rows: Row[];
  columns: Column[];
  actions?: (row: Row) => ReactNode;
}) {
  if (!rows.length) return <Empty />;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map(([key, title]) => (
              <th key={key}>{title}</th>
            ))}
            {actions && <th>操作</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={String(
                row.resource_id ||
                  row.event_id ||
                  row.fact_id ||
                  row.operation_id ||
                  row.id ||
                  i,
              )}
            >
              {columns.map(([key, , render]) => (
                <td key={key}>
                  {render ? render(row[key], row) : text(row[key])}
                </td>
              ))}
              {actions && (
                <td>
                  <div className="row-actions">{actions(row)}</div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function QueryList({
  method,
  args = {},
  refresh = 0,
  columns,
  actions,
  onRow,
}: {
  method: string;
  args?: Row;
  refresh?: number;
  columns: Column[];
  actions?: (row: Row) => ReactNode;
  onRow?: (row: Row) => Row;
}) {
  const [cursor, setCursor] = useState<string | null>(null),
    [stack, setStack] = useState<(string | null)[]>([]),
    [search, setSearch] = useState("");
  const { data, error, loading } = useQuery<Page>(
    method,
    { ...args, page: { limit: 30, cursor } },
    refresh,
  );
  const key = JSON.stringify(args);
  const [pageKey, setPageKey] = useState(`${method}:${refresh}:${key}`);
  if (pageKey !== `${method}:${refresh}:${key}`) {
    setPageKey(`${method}:${refresh}:${key}`);
    setCursor(null);
    setStack([]);
  }
  const rows = (data?.items || [])
    .map((row) => (onRow ? onRow(row) : row))
    .filter((row) =>
      JSON.stringify(row)
        .toLocaleLowerCase()
        .includes(search.toLocaleLowerCase()),
    );
  return (
    <>
      <div className="search-line">
        <input
          className="form-control"
          aria-label="筛选本页记录"
          placeholder="筛选本页…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      {error ? (
        <ErrorNote error={error} />
      ) : loading ? (
        <Empty>正在翻阅…</Empty>
      ) : (
        <Table rows={rows} columns={columns} actions={actions} />
      )}
      <div className="pagination">
        <button
          className="btn-secondary"
          disabled={!stack.length || loading}
          onClick={() => {
            setCursor(stack.at(-1) ?? null);
            setStack(stack.slice(0, -1));
          }}
        >
          上一页
        </button>
        <span className="small">
          第 {stack.length + 1} 页 ·{" "}
          {data?.snapshot_at ? stamp(data.snapshot_at) : ""}
        </span>
        <button
          className="btn-secondary"
          disabled={!data?.next_cursor || loading}
          onClick={() => {
            setStack([...stack, cursor]);
            setCursor(data!.next_cursor);
          }}
        >
          下一页
        </button>
      </div>
    </>
  );
}
