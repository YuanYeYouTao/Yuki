import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { command, query, type Row } from "./api";
import { ErrorNote } from "./components";
import { useQuery } from "./hooks";
import type { PageProps } from "./pages";

const FINISHED = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
  "completed",
]);

/** One persistent Manager tty: the browser only renders and sends original Control actions. */
export function InteractiveTerminal({ props }: { props: PageProps }) {
  const viewport = useRef<HTMLDivElement>(null);
  const emulator = useRef<Terminal | null>(null);
  const inputHandler = useRef<(data: string) => void>(() => {});
  const inputBuffer = useRef("");
  const inputTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sending = useRef(false);
  const blocked = useRef(false);
  const cursor = useRef(0);
  const [run, setRun] = useState("");
  const [reconnect, setReconnect] = useState("");
  const [launchRequest, setLaunchRequest] = useState("");
  const [lastRequest, setLastRequest] = useState("");
  const [status, setStatus] = useState("尚未连接");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [inputPending, setInputPending] = useState(false);
  const [inputBlocked, setInputBlocked] = useState(false);
  function blockInput(value: boolean) {
    blocked.current = value;
    setInputBlocked(value);
  }
  const canRead = props.allowed("read_environment");
  const canWrite = props.allowed("mutate_environment_terminal");
  const environment = useQuery<Row>(
    "read_environment",
    { section: "status", arguments: {} },
    props.refresh + (run ? 1 : 0),
    canRead,
  );
  const recent = (
    ((environment.data?.fields as Row | undefined)?.runs || []) as Row[]
  )
    .filter(
      (item) =>
        item.kind === "terminal_exec" &&
        ["queued", "running"].includes(String(item.status)),
    )
    .slice(0, 4);

  useEffect(() => {
    const element = viewport.current;
    if (
      !element ||
      typeof window.matchMedia !== "function" ||
      typeof ResizeObserver === "undefined"
    )
      return;
    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: "Consolas, Cascadia Code, monospace",
      fontSize: 13,
      scrollback: 5000,
      theme: {
        background: "#211f2b",
        foreground: "#f0eaf7",
        cursor: "#ccb8f0",
        selectionBackground: "#a98dda77",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(element);
    fit.fit();
    const resize = new ResizeObserver(() => fit.fit());
    resize.observe(element);
    const listener = term.onData((data) => inputHandler.current(data));
    emulator.current = term;
    return () => {
      if (inputTimer.current) clearTimeout(inputTimer.current);
      emulator.current = null;
      listener.dispose();
      resize.disconnect();
      term.dispose();
    };
  }, []);

  async function mutation(
    action: "exec" | "write" | "control",
    spec: Row,
    requestId: string,
  ) {
    return command("mutate_environment_terminal", {
      request_id: requestId,
      expected_revision: 0,
      payload: { resource_id: "environment", action, spec },
      target: { kind: "yuki" },
    });
  }

  async function flush() {
    if (sending.current || blocked.current || !run || !inputBuffer.current)
      return;
    sending.current = true;
    setInputPending(true);
    try {
      while (inputBuffer.current && !blocked.current) {
        // The original terminal_write contract accepts no more than 8192 UTF-8 bytes.
        const chunk = [...inputBuffer.current].slice(0, 1024).join("");
        inputBuffer.current = inputBuffer.current.slice(chunk.length);
        const requestId = crypto.randomUUID();
        setLastRequest(requestId);
        try {
          const result = await mutation(
            "write",
            { run_id: run, text: chunk },
            requestId,
          );
          if (result.success !== true) {
            blockInput(true);
            setError(
              new Error(
                "原输入请求仍在处理或结果未知；请按原请求查询回执后继续输入。",
              ),
            );
            break;
          }
        } catch (failure) {
          // No implicit retry: keep this request for its original persisted receipt.
          blockInput(true);
          setError(failure);
          break;
        }
      }
    } finally {
      sending.current = false;
      setInputPending(false);
    }
  }
  useEffect(() => {
    inputHandler.current = (data) => {
      if (!run || blocked.current || !canWrite) return;
      inputBuffer.current += data;
      if (!inputTimer.current)
        inputTimer.current = setTimeout(() => {
          inputTimer.current = null;
          void flush();
        }, 60);
    };
  });

  function connect(runId: string) {
    if (inputTimer.current) {
      clearTimeout(inputTimer.current);
      inputTimer.current = null;
    }
    cursor.current = 0;
    inputBuffer.current = "";
    blockInput(false);
    setRun(runId);
    setReconnect(runId);
    setStatus("连接中");
    setError(null);
    emulator.current?.clear();
    emulator.current?.focus();
  }

  async function launch() {
    const requestId = crypto.randomUUID();
    setLaunchRequest(requestId);
    setBusy(true);
    setError(null);
    try {
      const result = await mutation(
        "exec",
        { command: "bash", cwd: "/workspace", tty: true, timeout_seconds: 0 },
        requestId,
      );
      if (!result.success || !result.resource_id)
        throw new Error("终端未返回原运行编号");
      connect(String(result.resource_id));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  async function recoverLaunch() {
    if (!launchRequest) return;
    setBusy(true);
    setError(null);
    try {
      const result = await query<Row>("read_terminal_submission", {
        request_id: launchRequest,
      });
      const fields = result.fields as Row;
      if (fields?.run_id) connect(String(fields.run_id));
      else setStatus("原请求尚无运行编号；请稍后查询原请求");
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  async function recoverInput() {
    if (!lastRequest) return;
    setBusy(true);
    try {
      const result = await query<Row>("read_operation", {
        request_id: lastRequest,
      });
      const fields = (result.fields || result) as Row;
      if (
        fields.success === true ||
        (fields.status === "succeeded" && fields.success !== false)
      ) {
        blockInput(false);
        setError(null);
        void flush();
      } else
        setError(new Error("原输入未确认成功；先核对回执，不自动重新输入。"));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  async function interrupt() {
    if (!run) return;
    const requestId = crypto.randomUUID();
    setLastRequest(requestId);
    setBusy(true);
    try {
      const result = await mutation(
        "control",
        { run_id: run, action: "interrupt" },
        requestId,
      );
      if (result.success !== true) {
        blockInput(true);
        setError(
          new Error("原中断请求仍在处理或结果未知；请按原请求查询回执。"),
        );
      }
    } catch (failure) {
      setError(failure);
      blockInput(true);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!run || !canRead) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const result = await query<Row>("read_environment", {
          section: "terminal",
          arguments: { run_id: run, cursor: cursor.current },
        });
        if (!active) return;
        const fields = result.fields as Row;
        if (fields.output_lost === true)
          emulator.current?.writeln("\r\n[较早输出已过期，无法补回]\r\n");
        if (fields.output) emulator.current?.write(String(fields.output));
        if (typeof fields.next_cursor === "number")
          cursor.current = fields.next_cursor;
        const nextStatus = String(fields.status || "running");
        setStatus(nextStatus);
        if (fields.error) setError(new Error(String(fields.error)));
        if (!FINISHED.has(nextStatus)) timer = setTimeout(read, 900);
      } catch (failure) {
        if (active) {
          setError(failure);
          timer = setTimeout(read, 2000);
        }
      }
    }
    void read();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [run, canRead]);

  return (
    <div className="interactive-terminal">
      <div className="terminal-toolbar">
        <span>
          <span className="terminal-light" aria-hidden="true" /> /workspace ·{" "}
          {status}
        </span>
        <button
          type="button"
          className="btn-secondary"
          disabled={busy || !canWrite}
          onClick={() => void launch()}
        >
          新建终端
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={busy || inputPending || inputBlocked || !run || !canWrite}
          onClick={() => void interrupt()}
        >
          Ctrl+C
        </button>
      </div>
      <div
        ref={viewport}
        className="terminal-screen"
        aria-label="Yuki 的交互终端"
      />
      <div className="terminal-footer">
        <span>
          {inputPending
            ? "正在发送输入…"
            : run
              ? "点击终端后直接输入。终端继续运行时，离开页面不会自动关闭。"
              : "点“新建终端”连接持久 Linux 环境。"}
        </span>
        {run && (
          <details>
            <summary>当前运行编号</summary>
            <code>{run}</code>
          </details>
        )}
      </div>
      <details className="terminal-reconnect">
        <summary>接回已有终端或查询原请求</summary>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (reconnect) connect(reconnect);
          }}
        >
          <label>
            运行编号
            <input
              className="form-control"
              value={reconnect}
              onChange={(e) => setReconnect(e.target.value)}
              placeholder="已有 run_id"
            />
          </label>
          <button className="btn-secondary" disabled={!reconnect || !canRead}>
            接回
          </button>
        </form>
        {launchRequest && (
          <p>
            启动请求 <code>{launchRequest}</code>{" "}
            <button
              type="button"
              className="btn-secondary"
              disabled={busy || !props.allowed("read_terminal_submission")}
              onClick={() => void recoverLaunch()}
            >
              查询原启动
            </button>
          </p>
        )}
        {lastRequest && (
          <p>
            最后输入/控制请求 <code>{lastRequest}</code>{" "}
            <button
              type="button"
              className="btn-secondary"
              disabled={busy}
              onClick={() => void recoverInput()}
            >
              查询原回执
            </button>
          </p>
        )}
      </details>
      {recent.length > 0 && (
        <div className="terminal-recent">
          <span className="small">当前环境中的终端</span>
          {recent.map((item) => (
            <button
              key={String(item.run_id)}
              type="button"
              className="btn-secondary"
              onClick={() => connect(String(item.run_id))}
            >
              {run === item.run_id ? "已连接" : "接回终端"} ·{" "}
              {new Date(Number(item.created) * 1000).toLocaleString()}
            </button>
          ))}
        </div>
      )}
      {error != null && <ErrorNote error={error} />}
    </div>
  );
}
