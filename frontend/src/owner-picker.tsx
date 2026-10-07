import type { QueryMethod } from "./control-methods";
import { useState } from "react";
import type { Page, Row } from "./api";
import { useQuery } from "./hooks";
import { useDisplayNames } from "./names";
import type { NameKind } from "./names";

const sources: Record<NameKind, [QueryMethod, string, string]> = {
  person: ["list_persons", "person_id", "未命名人物"],
  space: ["list_spaces", "space_id", "未命名群"],
  conversation: ["list_conversations", "conversation_id", "未命名会话"],
  presence: ["list_presences", "presence_id", "Yuki"],
  binding: ["list_identity_bindings", "binding_id", "未命名接入绑定"],
  space_binding: ["list_space_bindings", "binding_id", "未命名群绑定"],
};

/** Select a canonical ID by an existing human name; ID stays the submitted value. */
export function OwnerPicker({
  kind,
  value,
  change,
  refresh = 0,
  required = false,
  empty = "全部",
  disabled = false,
  label,
}: {
  kind: NameKind;
  value: string;
  change: (value: string) => void;
  refresh?: number;
  required?: boolean;
  empty?: string;
  disabled?: boolean;
  label?: string;
}) {
  const [method, idField, fallback] = sources[kind];
  const [cursor, setCursor] = useState<string | null>(null);
  const [previous, setPrevious] = useState<Row[]>([]);
  const [search, setSearch] = useState("");
  const page = useQuery<Page>(
    method,
    { page: { limit: 100, cursor } },
    refresh,
  );
  const rows = [...previous, ...(page.data?.items || [])].filter(
    (row) => row[idField],
  );
  const names = useDisplayNames({
    [kind]: rows.map((row) => String(row[idField] || "")),
  });
  function display(row: Row): string {
    const id = String(row[idField] || "");
    const candidate = names[id] || String(row.name || row.display_name || "");
    return candidate && candidate !== "redacted" ? candidate : fallback;
  }
  const known = rows.some((row) => row[idField] === value);
  return (
    <span className="owner-picker">
      <input
        className="form-control"
        aria-label="按名称筛选"
        placeholder="按名字筛选…"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
      />
      <select
        className="form-control"
        aria-label={label || fallback}
        required={required}
        disabled={disabled}
        value={value}
        onChange={(event) => change(event.target.value)}
      >
        <option value="">{empty}</option>
        {!!value && !known && <option value={value}>当前选择（已保存）</option>}
        {rows
          .filter((row) => {
            const id = String(row[idField] || "");
            const label = display(row);
            return (
              !search ||
              label.toLocaleLowerCase().includes(search.toLocaleLowerCase()) ||
              id === value
            );
          })
          .map((row) => {
            const id = String(row[idField] || "");
            return (
              <option key={id} value={id}>
                {display(row)}
              </option>
            );
          })}
      </select>
      {page.data?.next_cursor && (
        <button
          type="button"
          className="btn-secondary"
          onClick={() => {
            setPrevious(rows);
            setCursor(page.data?.next_cursor || null);
          }}
        >
          更多
        </button>
      )}
    </span>
  );
}
