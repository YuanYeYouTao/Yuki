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
  edit?: "spec" | "value" | "payload";
  hint?: string;
  valueKind?: "boolean" | "number" | "string" | "secret";
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
  const [draft, setDraft] = useState(
    intent.valueKind === "string" || intent.valueKind === "secret"
      ? String(intent.payload.value ?? "")
      : JSON.stringify(
          intent.edit === "payload"
            ? intent.payload
            : (intent.payload[intent.edit || "spec"] ?? {}),
          null,
          2,
        ),
  );
  const [revision, setRevision] = useState(intent.revision);
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
        intent.valueKind === "string" || intent.valueKind === "secret"
          ? draft
          : intent.valueKind === "number"
            ? Number(draft)
            : JSON.parse(draft);
      const payload =
        intent.edit === "payload"
          ? edited
          : {
              ...intent.payload,
              ...(intent.edit ? { [intent.edit]: edited } : {}),
            };
      envelope = {
        request_id: crypto.randomUUID(),
        expected_revision: revision,
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
      {intent.edit === "payload" && (
        <label className="form-group">
          刚读取的资源版本
          <input
            className="form-control"
            type="number"
            min="0"
            step="1"
            value={revision}
            disabled={!!submitted}
            onChange={(e) => setRevision(Number(e.target.value))}
          />
        </label>
      )}
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
        <p>
          {intent.label}：
          {String(intent.payload.resource_id || intent.target?.id || "Yuki")}
        </p>
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
