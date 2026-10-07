import type { QueryMethod } from "./control-methods";
import { useQuery } from "./hooks";
import { text } from "./format";
import { useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "./api";
import type { Page, Row } from "./api";
import { Avatar, useDisplayNames } from "./names";
import type { NameReferences } from "./names";
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
function Cell({ value }: { value: unknown }) {
  const content = text(value);
  const [expanded, setExpanded] = useState(false);
  if (content.length <= 180)
    return <span className="cell-text">{content}</span>;
  return (
    <div className="long-cell">
      <span className="cell-text">
        {expanded ? content : content.slice(0, 180) + "…"}
      </span>
      <button
        type="button"
        className="file-open"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? "收起" : "展开全文"}
      </button>
    </div>
  );
}
const kindFor = (key: string, row?: Row) =>
  key === "target_id" && row?.target_kind === "person"
    ? "person"
    : key === "target_id" && row?.target_kind === "space"
      ? "space"
      : key.includes("person")
        ? "person"
        : key.includes("space_binding")
          ? "space_binding"
          : key.includes("space")
            ? "space"
            : key.includes("conversation")
              ? "conversation"
              : key.includes("presence")
                ? "presence"
                : key.includes("binding")
                  ? "binding"
                  : "";
export function Table({
  rows,
  columns,
  actions,
}: {
  rows: Row[];
  columns: Column[];
  actions?: (row: Row) => ReactNode;
}) {
  const refs: NameReferences = {};
  for (const row of rows)
    for (const [key] of columns) {
      const kind = kindFor(key, row) as keyof NameReferences,
        value = row[key];
      if (
        kind &&
        typeof value === "string" &&
        /^[0-9a-f]{8}-[0-9a-f-]{27,}$/.test(value)
      )
        (refs[kind] ||= []).push(value);
    }
  const names = useDisplayNames(refs);
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
                  {render ? (
                    render(row[key], row)
                  ) : kindFor(key, row) &&
                    typeof row[key] === "string" &&
                    /^[0-9a-f]{8}-[0-9a-f-]{27,}$/.test(String(row[key])) ? (
                    <span className="named-reference">
                      {["person", "space", "presence", "conversation"].includes(
                        kindFor(key, row),
                      ) && (
                        <span className="reference-avatar">
                          <Avatar
                            kind={
                              kindFor(key, row) as
                                "person" | "space" | "presence" | "conversation"
                            }
                            id={row[key]}
                            label={names[String(row[key])] || "头像"}
                            fallback={
                              names[String(row[key])] ||
                              (kindFor(key, row) === "space" ? "群" : "人")
                            }
                          />
                        </span>
                      )}
                      <span>
                        {names[String(row[key])] ||
                          (kindFor(key, row) === "space"
                            ? "未命名群"
                            : kindFor(key, row) === "person"
                              ? "未命名人物"
                              : "未命名对象")}
                        <details className="reference-id">
                          <summary>内部编号</summary>
                          {String(row[key])}
                        </details>
                      </span>
                    </span>
                  ) : (
                    <Cell value={row[key]} />
                  )}
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
  method: QueryMethod;
  args?: Row;
  refresh?: number;
  columns: Column[];
  actions?: (row: Row) => ReactNode;
  onRow?: (row: Row) => Row;
}) {
  const [number, setNumber] = useState(1),
    [jump, setJump] = useState("1"),
    [search, setSearch] = useState("");
  const { data, error, loading } = useQuery<Page>(
    method,
    { ...args, page: { limit: 30, number } },
    refresh,
  );
  const key = JSON.stringify(args);
  const [pageKey, setPageKey] = useState(`${method}:${refresh}:${key}`);
  if (pageKey !== `${method}:${refresh}:${key}`) {
    setPageKey(`${method}:${refresh}:${key}`);
    setNumber(1);
    setJump("1");
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
          disabled={number <= 1 || loading}
          onClick={() => {
            setNumber(number - 1);
            setJump(String(number - 1));
          }}
        >
          上一页
        </button>
        <span className="small">
          第 {number} 页 / 共 {Math.max(1, Math.ceil((data?.total || 0) / 30))}{" "}
          页 · 共 {data?.total ?? "?"} 条
        </span>
        <button
          className="btn-secondary"
          disabled={
            data?.total == null ||
            number >= Math.max(1, Math.ceil(data.total / 30)) ||
            loading
          }
          onClick={() => {
            setNumber(number + 1);
            setJump(String(number + 1));
          }}
        >
          下一页
        </button>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const target = Number(jump);
            if (
              Number.isInteger(target) &&
              target >= 1 &&
              target <= Math.max(1, Math.ceil((data?.total || 0) / 30))
            )
              setNumber(target);
          }}
        >
          <label>
            跳至{" "}
            <input
              aria-label="页码"
              type="number"
              min="1"
              max={Math.max(1, Math.ceil((data?.total || 0) / 30))}
              value={jump}
              onChange={(event) => setJump(event.target.value)}
            />
          </label>
          <button className="btn-secondary">跳转</button>
        </form>
      </div>
    </>
  );
}
