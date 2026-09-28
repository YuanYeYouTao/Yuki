import { useEffect, useState } from "react";
import { ApiError, command, query, type Row } from "./api";
import { Empty, ErrorNote, JsonNote } from "./components";
import { useQuery } from "./hooks";
import { DesktopWindow, MediaPreview } from "./preview";
import type { PageProps } from "./pages";

type Entry = {
  path: string;
  name: string;
  kind: string;
  size: number;
  modified_at: number;
};
type Operation = {
  requestId: string;
  label: string;
  error: unknown;
  result?: Row;
};
const ROOT = "/workspace";
const isImage = (name: string) =>
  /\.(?:png|jpe?g|gif|webp|bmp|avif)$/i.test(name);
const fileUrl = (path: string) =>
  `/api/control/files/environment?path=${encodeURIComponent(path)}`;
const basename = (path: string) => path.split("/").at(-1) || "工作区";
const parentOf = (path: string) =>
  path === ROOT ? ROOT : path.slice(0, path.lastIndexOf("/")) || ROOT;
const joined = (directory: string, name: string) => `${directory}/${name}`;

function Breadcrumbs({
  directory,
  open,
}: {
  directory: string;
  open: (path: string) => void;
}) {
  const segments = directory.slice(ROOT.length).split("/").filter(Boolean);
  return (
    <nav className="workspace-breadcrumbs" aria-label="工作区路径">
      <button type="button" onClick={() => open(ROOT)}>
        工作区
      </button>
      {segments.map((part, index) => {
        const path = ROOT + "/" + segments.slice(0, index + 1).join("/");
        return (
          <span key={path}>
            <span aria-hidden="true"> / </span>
            <button type="button" onClick={() => open(path)}>
              {part}
            </button>
          </span>
        );
      })}
    </nav>
  );
}

function FileWindow({
  path,
  create,
  close,
  mutate,
  canRead,
  canWrite,
  refresh,
}: {
  path: string;
  create: boolean;
  close: () => void;
  mutate: (
    label: string,
    action: string,
    spec: Row,
    onSuccess?: () => void,
  ) => Promise<void>;
  canRead: boolean;
  canWrite: boolean;
  refresh: number;
}) {
  const file = useQuery<Row>(
    "read_environment",
    { section: "file", arguments: { path } },
    refresh,
    !create && canRead,
  );
  const fields = file.data?.fields as Row | undefined;
  const [body, setBody] = useState("");
  const [original, setOriginal] = useState("");
  const [destination, setDestination] = useState(path);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<unknown>(null);
  useEffect(() => {
    if (create) {
      setBody("");
      setOriginal("");
      return;
    }
    if (fields && !fields.binary && !fields.truncated) {
      setBody(String(fields.text ?? ""));
      setOriginal(String(fields.text ?? ""));
    }
  }, [create, fields]);
  const dirty = body !== original;
  function dismiss() {
    if (!dirty || window.confirm("有未保存的修改，确定关闭吗？")) close();
  }
  async function perform(
    label: string,
    action: string,
    spec: Row,
    after?: () => void,
  ) {
    setLocalError(null);
    setBusy(true);
    try {
      await mutate(label, action, spec, after);
    } catch (error) {
      setLocalError(error);
    } finally {
      setBusy(false);
    }
  }
  const preview = isImage(basename(path));
  return (
    <DesktopWindow
      title={create ? `新建文件 · ${basename(path)}` : basename(path)}
      close={dismiss}
    >
      <div className="workspace-file-meta">
        <span title={path}>{path}</span>
        {!create && fields && (
          <span>{Number(fields.size).toLocaleString()} 字节</span>
        )}
      </div>
      {file.error != null && <ErrorNote error={file.error} />}
      {localError != null && <ErrorNote error={localError} />}
      {!create && !fields && file.error == null && (
        <Empty>正在读取真实文件…</Empty>
      )}
      {!create && preview && canRead && (
        <MediaPreview url={fileUrl(path)} title={basename(path)} />
      )}
      {!create && fields?.binary === true && !preview && (
        <Empty>二进制文件可以下载；修改请使用下方的终端。</Empty>
      )}
      {!create && fields?.truncated === true && !preview && (
        <Empty>
          文件超过文本编辑上限，避免用片段覆盖完整文件。可下载或使用终端编辑。
        </Empty>
      )}
      {(create || (fields && !fields.binary && !fields.truncated)) &&
        !preview && (
          <>
            <label className="form-group">
              文件内容（UTF-8）
              <textarea
                className="form-control workspace-editor"
                value={body}
                onChange={(e) => setBody(e.target.value)}
                spellCheck={false}
                disabled={busy || !canWrite}
              />
            </label>
            <div className="workspace-file-actions">
              <button
                type="button"
                className="btn-primary"
                disabled={busy || !canWrite || (!create && !dirty)}
                onClick={() =>
                  void perform(
                    create ? "新建工作文件" : "保存工作文件",
                    "write",
                    {
                      path,
                      text: body,
                      expected_version: create ? "missing" : fields?.version,
                    },
                    () => {
                      setOriginal(body);
                      if (create) close();
                    },
                  )
                }
              >
                {busy ? "正在保存…" : create ? "创建文件" : "保存到工作区"}
              </button>
            </div>
          </>
        )}
      {!create && fields && (
        <>
          <div className="workspace-file-actions">
            {canRead && (
              <a
                className="btn-secondary"
                href={fileUrl(path)}
                download={basename(path)}
              >
                下载原文件
              </a>
            )}
          </div>
          <form
            className="workspace-move"
            onSubmit={(e) => {
              e.preventDefault();
              if (destination === path) return;
              void perform(
                "移动或重命名工作文件",
                "move",
                { path, destination, expected_version: fields.version },
                close,
              );
            }}
          >
            <label>
              重命名或移动到
              <input
                className="form-control"
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
                disabled={busy || !canWrite}
              />
            </label>
            <button
              className="btn-secondary"
              disabled={busy || !canWrite || destination === path}
            >
              移动
            </button>
          </form>
          <div className="workspace-file-actions">
            <button
              type="button"
              className="btn-secondary"
              disabled={busy || !canWrite}
              onClick={() =>
                void perform(
                  "删除真实工作文件",
                  "delete",
                  { path, expected_version: fields.version },
                  close,
                )
              }
            >
              删除这个文件
            </button>
          </div>
          <details className="workspace-file-version">
            <summary>实际内容版本</summary>
            <code>{String(fields.version || "未知")}</code>
          </details>
        </>
      )}
    </DesktopWindow>
  );
}

export function FileManager({ props }: { props: PageProps }) {
  const [directory, setDirectory] = useState(ROOT);
  const [location, setLocation] = useState(ROOT);
  const [page, setPage] = useState(1);
  const [jump, setJump] = useState("1");
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<{
    path: string;
    create: boolean;
  } | null>(null);
  const [movingDirectory, setMovingDirectory] = useState("");
  const [destination, setDestination] = useState("");
  const [newName, setNewName] = useState("");
  const [folderName, setFolderName] = useState("");
  const [operation, setOperation] = useState<Operation | null>(null);
  const [busy, setBusy] = useState(false);
  const canRead = props.allowed("read_environment");
  const canWrite = props.allowed("mutate_environment_file");
  const list = useQuery<Row>(
    "read_environment",
    {
      section: "files",
      arguments: { path: directory, number: page, limit: 30 },
    },
    props.refresh + refresh,
    canRead,
  );
  const fields = list.data?.fields as Row | undefined;
  const entries = (fields?.items || []) as Entry[];
  const total = Number(fields?.total || 0);
  const pages = Math.max(1, Math.ceil(total / 30));
  function open(path: string) {
    setDirectory(path);
    setLocation(path);
    setPage(1);
    setJump("1");
    setSelected(null);
  }
  async function mutate(
    label: string,
    action: string,
    spec: Row,
    onSuccess?: () => void,
  ) {
    const requestId = crypto.randomUUID();
    setOperation({ requestId, label, error: null });
    try {
      const result = await command("mutate_environment_file", {
        request_id: requestId,
        expected_revision: 0,
        payload: { resource_id: "environment", action, spec },
        target: { kind: "yuki" },
      });
      setOperation({ requestId, label, error: null, result });
      if (result.success) {
        setRefresh((value) => value + 1);
        onSuccess?.();
      }
    } catch (error) {
      setOperation({ requestId, label, error });
      // Unknown external effects keep the original request for explicit receipt lookup.
      throw error;
    }
  }
  async function recover() {
    if (!operation) return;
    setBusy(true);
    try {
      const result = await query<Row>("read_operation", {
        request_id: operation.requestId,
      });
      setOperation({ ...operation, error: null, result });
      setRefresh((value) => value + 1);
    } catch (error) {
      setOperation({ ...operation, error });
    } finally {
      setBusy(false);
    }
  }
  async function deleteEntry(entry: Entry) {
    setBusy(true);
    let submitted = false;
    try {
      let expected_version: unknown;
      if (entry.kind === "file") {
        const response = await query<Row>("read_environment", {
          section: "file",
          arguments: { path: entry.path },
        });
        expected_version = (response.fields as Row).version;
      }
      submitted = true;
      await mutate(
        entry.kind === "directory" ? "删除真实空目录" : "删除真实工作文件",
        "delete",
        { path: entry.path, ...(expected_version ? { expected_version } : {}) },
      );
    } catch (error) {
      // A failed version read has no Control request to recover. Once submitted,
      // mutate() owns the original request ID and its receipt/error display.
      if (!submitted)
        setOperation({ requestId: "", label: "删除工作区条目", error });
    } finally {
      setBusy(false);
    }
  }
  async function uploadSelected(file: File) {
    if (file.size > 4 * 1024 * 1024 || !file.name || /[\\/]/.test(file.name)) {
      setOperation({
        requestId: "",
        label: "上传真实文件",
        error: new Error(
          "文件最大 4 MiB，文件名不能包含路径分隔符；更大的文件可通过终端处理。",
        ),
      });
      return;
    }
    setBusy(true);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let encoded = "";
      for (let offset = 0; offset < bytes.length; offset += 16384)
        encoded += String.fromCharCode(
          ...bytes.subarray(offset, offset + 16384),
        );
      await mutate("上传到真实工作区", "upload", {
        path: joined(directory, file.name),
        base64: btoa(encoded),
        expected_version: "missing",
      });
    } catch (error) {
      if (!(error instanceof ApiError))
        setOperation({ requestId: "", label: "上传真实文件", error });
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="workspace-manager">
      <p className="small">
        这里直接读取并修改 Yuki 与终端共用的持久 <code>/workspace</code>
        ；文件修改按原内容 SHA 核对冲突。
      </p>
      <div className="workspace-toolbar">
        <button
          className="btn-secondary"
          disabled={directory === ROOT}
          onClick={() => open(parentOf(directory))}
          aria-label="上一级目录"
        >
          ↑ 上一级
        </button>
        <Breadcrumbs directory={directory} open={open} />
        <button
          className="btn-secondary"
          onClick={() => setRefresh((value) => value + 1)}
        >
          刷新
        </button>
      </div>
      <form
        className="workspace-location"
        onSubmit={(e) => {
          e.preventDefault();
          open(location);
        }}
      >
        <label>
          路径
          <input
            className="form-control"
            value={location}
            onChange={(e) => setLocation(e.target.value)}
          />
        </label>
        <button className="btn-secondary">打开</button>
      </form>
      <div className="workspace-create">
        <label className="workspace-upload">
          上传本地文件到当前目录（最大 4 MiB）
          <input
            type="file"
            disabled={!canWrite || busy}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void uploadSelected(file);
              e.target.value = "";
            }}
          />
        </label>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (
              newName &&
              !/[\\/]/.test(newName) &&
              newName !== "." &&
              newName !== ".."
            )
              setSelected({ path: joined(directory, newName), create: true });
          }}
        >
          <label>
            新建文件
            <input
              className="form-control"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="文件名.txt"
            />
          </label>
          <button className="btn-secondary" disabled={!canWrite || !newName}>
            创建
          </button>
        </form>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!folderName || /[\\/]/.test(folderName)) return;
            setBusy(true);
            void mutate(
              "新建真实目录",
              "mkdir",
              { path: joined(directory, folderName) },
              () => setFolderName(""),
            )
              .catch(() => {})
              .finally(() => setBusy(false));
          }}
        >
          <label>
            新建文件夹
            <input
              className="form-control"
              value={folderName}
              onChange={(e) => setFolderName(e.target.value)}
              placeholder="文件夹名"
            />
          </label>
          <button
            className="btn-secondary"
            disabled={!canWrite || !folderName || busy}
          >
            创建
          </button>
        </form>
      </div>
      {list.error != null && <ErrorNote error={list.error} />}
      {list.loading && <Empty>正在读取工作区…</Empty>}
      {fields && (
        <>
          <div
            className="workspace-entries"
            role="list"
            aria-label="当前目录文件"
          >
            {!entries.length && <Empty>这个目录目前没有文件。</Empty>}
            {entries.map((entry) => (
              <div className="workspace-entry" role="listitem" key={entry.path}>
                <button
                  type="button"
                  className="workspace-entry-main"
                  disabled={entry.kind === "special"}
                  onClick={() =>
                    entry.kind === "directory"
                      ? open(entry.path)
                      : setSelected({ path: entry.path, create: false })
                  }
                >
                  <span className="workspace-entry-icon" aria-hidden="true">
                    {entry.kind === "directory"
                      ? "📁"
                      : isImage(entry.name)
                        ? "🖼"
                        : "📄"}
                  </span>
                  <span className="workspace-entry-name">{entry.name}</span>
                </button>
                {entry.kind === "file" &&
                  isImage(entry.name) &&
                  props.allowed("download_environment_file") && (
                    <MediaPreview
                      compact
                      url={fileUrl(entry.path)}
                      title={entry.name}
                    />
                  )}
                <span className="workspace-entry-size">
                  {entry.kind === "directory"
                    ? "文件夹"
                    : `${Number(entry.size).toLocaleString()} 字节`}
                </span>
                <time
                  className="workspace-entry-time"
                  dateTime={new Date(entry.modified_at * 1000).toISOString()}
                >
                  {new Date(entry.modified_at * 1000).toLocaleString()}
                </time>
                {entry.kind === "directory" && (
                  <button
                    type="button"
                    className="file-open"
                    disabled={busy || !canWrite}
                    onClick={() => {
                      setMovingDirectory(entry.path);
                      setDestination(entry.path);
                    }}
                  >
                    移动
                  </button>
                )}
                <button
                  type="button"
                  className="file-open"
                  disabled={busy || !canWrite || entry.kind === "special"}
                  onClick={() => void deleteEntry(entry)}
                >
                  删除
                </button>
              </div>
            ))}
          </div>
          <div className="pagination workspace-pagination">
            <span>
              共 {total.toLocaleString()} 项 · 第 {page} 页 / 共 {pages} 页
            </span>
            <button
              className="btn-secondary"
              disabled={page <= 1}
              onClick={() => {
                setPage(page - 1);
                setJump(String(page - 1));
              }}
            >
              上一页
            </button>
            <button
              className="btn-secondary"
              disabled={page >= pages}
              onClick={() => {
                setPage(page + 1);
                setJump(String(page + 1));
              }}
            >
              下一页
            </button>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const value = Number(jump);
                if (Number.isInteger(value) && value >= 1 && value <= pages)
                  setPage(value);
              }}
            >
              <label>
                跳到第{" "}
                <input
                  className="form-control"
                  type="number"
                  min="1"
                  max={pages}
                  value={jump}
                  onChange={(e) => setJump(e.target.value)}
                />{" "}
                页
              </label>
              <button className="btn-secondary">跳转</button>
            </form>
          </div>
        </>
      )}
      {operation && (
        <div className="workspace-operation" role="status">
          <strong>{operation.label}</strong>
          {operation.error != null && <ErrorNote error={operation.error} />}
          {operation.result && (
            <JsonNote title="原操作回执" value={operation.result} />
          )}
          {operation.requestId && (
            <details>
              <summary>请求编号与恢复</summary>
              <code>{operation.requestId}</code>
              <button
                type="button"
                className="btn-secondary"
                disabled={busy}
                onClick={() => void recover()}
              >
                按原请求查询结果
              </button>
            </details>
          )}
        </div>
      )}
      {selected && (
        <FileWindow
          key={`${selected.path}:${selected.create}`}
          path={selected.path}
          create={selected.create}
          close={() => setSelected(null)}
          mutate={mutate}
          canRead={canRead}
          canWrite={canWrite}
          refresh={props.refresh + refresh}
        />
      )}
      {movingDirectory && (
        <DesktopWindow
          title={`移动文件夹 · ${basename(movingDirectory)}`}
          close={() => setMovingDirectory("")}
        >
          <form
            className="workspace-move"
            onSubmit={(e) => {
              e.preventDefault();
              if (destination !== movingDirectory) {
                setBusy(true);
                void mutate(
                  "移动真实目录",
                  "move",
                  { path: movingDirectory, destination },
                  () => setMovingDirectory(""),
                )
                  .catch(() => {})
                  .finally(() => setBusy(false));
              }
            }}
          >
            <label>
              目标路径
              <input
                className="form-control"
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
              />
            </label>
            <button
              className="btn-primary"
              disabled={busy || !canWrite || destination === movingDirectory}
            >
              移动目录
            </button>
          </form>
          <p className="small">目标不能已存在；本次操作会移动真实持久目录。</p>
        </DesktopWindow>
      )}
    </div>
  );
}
