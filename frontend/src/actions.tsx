import { useEffect, useRef, useState } from "react";
import { command, query } from "./api";
import type { Command, Row } from "./api";
import { ErrorNote, JsonNote } from "./components";

export interface Intent {
  method: string;
  label: string;
  revision: number;
  payload: Row;
  target?: Row;
  edit?: "value";
  hint?: string;
  valueKind?: "boolean" | "number" | "string" | "secret";
  review?: Row | string;
  onReceipt?: (requestId: string, result: Row) => void;
}
export function ActionSheet({
  intent,
  close,
  completed,
}: {
  intent: Intent;
  close: () => void;
  completed: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState(String(intent.payload.value ?? ""));
  const [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false),
    [result, setResult] = useState<Row | null>(null);
  const [submitted, setSubmitted] = useState<Command | null>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  async function submit() {
    setError(null);
    let envelope: Command;
    try {
      if (
        intent.valueKind === "number" &&
        (!draft.trim() || !Number.isFinite(Number(draft)))
      )
        throw new Error("invalid number");
      const edited =
        intent.valueKind === "number"
          ? Number(draft)
          : intent.valueKind === "boolean"
            ? draft === "true"
            : draft;
      const payload = {
        ...intent.payload,
        ...(intent.edit ? { value: edited } : {}),
      };
      envelope = {
        request_id: crypto.randomUUID(),
        expected_revision: intent.revision,
        payload,
        target: intent.target || { kind: "yuki" },
      };
    } catch {
      setError(new Error("请输入有效的值；结构化内容请检查 JSON 格式。"));
      return;
    }
    setBusy(true);
    setSubmitted(envelope);
    try {
      const value = await command(intent.method, envelope);
      setResult(value);
      intent.onReceipt?.(envelope.request_id, value);
      completed();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function receipt() {
    if (!submitted) return;
    setBusy(true);
    setError(null);
    try {
      setResult(
        await query<Row>("read_operation", {
          request_id: submitted.request_id,
        }),
      );
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="action-sheet"
      onCancel={(e) => {
        if (busy) e.preventDefault();
        else close();
      }}
      aria-labelledby="action-title"
    >
      <header>
        <h2 id="action-title">{intent.label}</h2>
        <button className="btn-secondary" disabled={busy} onClick={close}>
          关闭
        </button>
      </header>
      <p className="small">
        当前版本 {intent.revision} · 提交前请检查目标和内容。
      </p>
      {intent.hint && <p>{intent.hint}</p>}
      {intent.review != null &&
        (typeof intent.review === "string" ? (
          <pre className="persona-text">{intent.review}</pre>
        ) : (
          <JsonNote title="即将提交的内容" value={intent.review} />
        ))}
      {!result && intent.edit && (
        <label className="form-group">
          {intent.edit === "value" ? "新的值" : "操作内容"}
          {intent.valueKind === "boolean" ? (
            <select
              className="form-control"
              value={draft}
              disabled={!!submitted}
              onChange={(event) => setDraft(event.target.value)}
            >
              <option value="true">开启</option>
              <option value="false">关闭</option>
            </select>
          ) : intent.valueKind ? (
            <input
              className="form-control"
              type={
                intent.valueKind === "number"
                  ? "number"
                  : intent.valueKind === "secret"
                    ? "password"
                    : "text"
              }
              step="any"
              autoComplete="off"
              value={draft}
              disabled={!!submitted}
              onChange={(event) => setDraft(event.target.value)}
            />
          ) : (
            <textarea
              className="form-control code-editor"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={!!submitted}
              spellCheck={false}
            />
          )}
        </label>
      )}
      {!intent.edit && !result && (
        <div>
          <p>{intent.label}</p>
          <details className="small">
            <summary>内部目标编号</summary>
            <code>
              {String(
                intent.payload.resource_id || intent.target?.id || "Yuki",
              )}
            </code>
          </details>
        </div>
      )}
      {error != null && <ErrorNote error={error} />}
      {result && <JsonNote title="持久回执" value={result} />}
      {submitted && <p className="small">请求编号：{submitted.request_id}</p>}
      <div className="settings-actions">
        {!submitted && (
          <button className="btn-primary" disabled={busy} onClick={submit}>
            {busy ? "正在提交…" : "提交"}
          </button>
        )}
        {submitted && (
          <button className="btn-secondary" disabled={busy} onClick={receipt}>
            查询原请求
          </button>
        )}
      </div>
    </dialog>
  );
}
