import { useEffect, useMemo, useState } from "react";
import { query } from "./api";
import type { Row } from "./api";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export type NameKind =
  | "person"
  | "space"
  | "presence"
  | "conversation"
  | "binding"
  | "space_binding";
export type NameReferences = Partial<Record<NameKind, string[]>>;

export function useDisplayNames(
  references: NameReferences,
): Record<string, string> {
  const signature = JSON.stringify(references);
  const batches = useMemo(() => {
    const unique = new Set<string>();
    const result: NameReferences[] = [];
    let batch: NameReferences = {};
    let count = 0;
    for (const [kind, ids] of Object.entries(references) as [
      NameKind,
      string[],
    ][]) {
      for (const id of ids) {
        if (!uuid.test(id) || unique.has(id)) continue;
        unique.add(id);
        if (count === 100) {
          result.push(batch);
          batch = {};
          count = 0;
        }
        (batch[kind] ||= []).push(id);
        count++;
      }
    }
    if (count) result.push(batch);
    return result;
  }, [signature]);
  const [state, setState] = useState<{
    key: string;
    names: Record<string, string>;
  }>({ key: "", names: {} });
  useEffect(() => {
    if (!batches.length) return;
    let active = true;
    Promise.all(
      batches.map((references) =>
        query<Row>("read_display_names", { references }),
      ),
    )
      .then((pages) => {
        if (!active) return;
        const names = Object.assign(
          {},
          ...pages.map((page) => (page.fields as Row)?.names || {}),
        ) as Record<string, string>;
        for (const [id, value] of Object.entries(names))
          if (value === "redacted") delete names[id];
        setState({ key: signature, names });
      })
      .catch(() => {
        if (active) setState({ key: signature, names: {} });
      });
    return () => {
      active = false;
    };
  }, [signature, batches]);
  return state.key === signature ? state.names : {};
}

export function Avatar({
  kind,
  id,
  label = "头像",
  fallback = "",
}: {
  kind: "person" | "space" | "presence" | "conversation";
  id?: unknown;
  label?: string;
  fallback?: string;
}) {
  const canonical = typeof id === "string" && uuid.test(id) ? id : "";
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [canonical, kind]);
  return canonical && !failed ? (
    <img
      className="avatar-photo"
      src={`/api/control/files/avatar/${kind}/${encodeURIComponent(canonical)}`}
      alt={label}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  ) : (
    <span className="avatar-initial" aria-label={label}>
      {Array.from(fallback.trim() || label.trim() || "？")[0]}
    </span>
  );
}
