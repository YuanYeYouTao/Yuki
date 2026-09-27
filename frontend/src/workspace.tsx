import { useState } from "react";
import { query, type Row } from "./api";
import { useQuery } from "./hooks";
import type { PageProps } from "./pages";
import {
  Badge,
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
} from "./components";
import { stamp, text } from "./format";
const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
import { SchemaFields } from "./schema-fields";
import { initialSchemaValue } from "./schema-values";

function EnvironmentAction({
  schema,
  terminal,
  props,
  initial,
}: {
  schema: Row;
  terminal: boolean;
  props: PageProps;
  initial?: Row;
}) {
  const [action, setAction] = useState(terminal ? "exec" : "write");
  const selected = schema[action] as Row;
  const [values, setValues] = useState<Row>(
    initial || (initialSchemaValue(selected, selected) as Row),
  );
  const [run, setRun] = useState(""),
    [requestId, setRequestId] = useState("");
  const method = terminal
    ? "mutate_environment_terminal"
    : "mutate_environment_file";
  return (
    <>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          props.act({
            method,
            label: terminal ? "终端操作" : "工作区文件操作",
            revision: 0,
            payload: { resource_id: "environment", action, spec: values },
            review: values,
            hint: terminal
              ? "命令在 Yuki 原持久 Linux 环境执行。回执仅表示已受理，请按 run_id 查看实际结果。"
              : "使用读取到的原内容 SHA256；文件冲突需重新读取。",
            onReceipt: (_request, result) => {
              if (terminal) setRequestId(_request);
              if (terminal && result.success && result.resource_id)
                setRun(String(result.resource_id));
            },
          });
        }}
      >
        <label className="form-group">
          {terminal ? "终端动作" : "文件动作"}
          <select
            className="form-control"
            value={action}
            onChange={(e) => {
              const next = e.target.value;
              setAction(next);
              setValues(
                initialSchemaValue(
                  schema[next] as Row,
                  schema[next] as Row,
                ) as Row,
              );
            }}
          >
            {Object.keys(schema).map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
        <SchemaFields
          key={action}
          schema={selected}
          root={selected}
          values={values}
          change={setValues}
          prefix={terminal ? "terminal" : "workspace-file"}
        />
        <button className="btn-primary" disabled={!props.allowed(method)}>
          检查并提交
        </button>
      </form>
      {terminal && (
        <TerminalReader
          key={`${run}:${requestId}`}
          initial={run}
          originalRequest={requestId}
          props={props}
        />
      )}
    </>
  );
}

function TerminalReader({
  initial,
  originalRequest,
  props,
}: {
  initial: string;
  originalRequest: string;
  props: PageProps;
}) {
  const [requestId, setRequestId] = useState(originalRequest);
  const [run, setRun] = useState(initial),
    [cursor, setCursor] = useState(0),
    [result, setResult] = useState<Row | null>(null),
    [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  async function read() {
    setBusy(true);
    setError(null);
    try {
      const value = await query<Row>("read_environment", {
        section: "terminal",
        arguments: { run_id: run, cursor },
      });
      setResult(value.fields as Row);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="terminal-reader">
      <form
        className="search-line"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            const value = await query<Row>("read_terminal_submission", {
              request_id: requestId,
            });
            const fields = value.fields as Row;
            setResult(fields);
            if (fields.run_id) {
              setRun(String(fields.run_id));
              setCursor(0);
            }
          } catch (e) {
            setError(e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          原管理请求 UUID
          <input
            className="form-control"
            required
            value={requestId}
            onChange={(e) => setRequestId(e.target.value)}
          />
        </label>
        <button
          className="btn-secondary"
          disabled={busy || !props.allowed("read_terminal_submission")}
        >
          查原执行，不重跑
        </button>
      </form>
      <h3>按原 run_id 查看终端</h3>
      <form
        className="search-line"
        onSubmit={(e) => {
          e.preventDefault();
          void read();
        }}
      >
        <label>
          run_id
          <input
            className="form-control"
            required
            value={run}
            onChange={(e) => {
              setRun(e.target.value);
              setCursor(0);
              setResult(null);
            }}
          />
        </label>
        <label>
          字节游标
          <input
            type="number"
            min="0"
            className="form-control"
            value={cursor}
            onChange={(e) => setCursor(Number(e.target.value))}
          />
        </label>
        <button
          className="btn-secondary"
          disabled={busy || !props.allowed("read_environment")}
        >
          读取输出
        </button>
      </form>
      {error != null && <ErrorNote error={error} />}
      {result && (
        <>
          <p className="small">
            {String(result.status || result.error || "未知")} · run_id{" "}
            {String(result.run_id || "未知")}
          </p>
          {result.output != null && (
            <pre className="file-preview">{String(result.output)}</pre>
          )}
          {result.output_lost === true && (
            <p className="error-note">
              早期输出已按原日志保留策略删除，不能回填。
            </p>
          )}
          <JsonNote title="原终端回执" value={result} />
          {typeof result.next_cursor === "number" && (
            <button
              className="btn-secondary"
              onClick={() => setCursor(Number(result.next_cursor))}
            >
              使用下一字节游标
            </button>
          )}
        </>
      )}
    </div>
  );
}

function Environment({ props }: { props: PageProps }) {
  const schema = useQuery<Row>(
    "read_environment",
    { section: "schema", arguments: {} },
    props.refresh,
    props.allowed("read_environment"),
  );
  const health = useQuery<Row>(
    "read_environment",
    { section: "status", arguments: {} },
    props.refresh,
    props.allowed("read_environment"),
  );
  const [path, setPath] = useState("/workspace"),
    [directory, setDirectory] = useState("/workspace"),
    [cursors, setCursors] = useState<string[]>([""]),
    [filePath, setFilePath] = useState(""),
    [offset, setOffset] = useState(0);
  const listing = useQuery<Row>(
    "read_environment",
    {
      section: "files",
      arguments: { path: directory, cursor: cursors.at(-1) || "", limit: 30 },
    },
    props.refresh,
    props.allowed("read_environment"),
  );
  const file = useQuery<Row>(
    "read_environment",
    { section: "file", arguments: { path: filePath, offset } },
    props.refresh,
    !!filePath && props.allowed("read_environment"),
  );
  const schemas = schema.data?.fields as Row | undefined,
    fields = listing.data?.fields as Row | undefined,
    read = file.data?.fields as Row | undefined;
  return (
    <>
      <Section title="持久 Linux 文件与环境">
        {health.error != null && <ErrorNote error={health.error} />}
        {health.data && (
          <JsonNote title="实际环境状态" value={health.data.fields} />
        )}
        <form
          className="search-line"
          onSubmit={(e) => {
            e.preventDefault();
            setDirectory(path);
            setCursors([""]);
          }}
        >
          <label>
            目录
            <input
              className="form-control"
              value={path}
              onChange={(e) => setPath(e.target.value)}
            />
          </label>
          <button className="btn-secondary">打开目录</button>
        </form>
        {listing.error != null && <ErrorNote error={listing.error} />}
        {fields?.error ? (
          <ErrorNote error={new Error(String(fields.error))} />
        ) : (
          <div className="workspace-file-list">
            {((fields?.items || []) as Row[]).map((row) => (
              <div className="config-array-item" key={String(row.path)}>
                <span>
                  {text(row.name)} · {text(row.kind)} · {text(row.size)} 字节
                </span>
                <button
                  className="btn-secondary"
                  onClick={() => {
                    if (row.kind === "directory") {
                      setPath(String(row.path));
                      setDirectory(String(row.path));
                      setCursors([""]);
                    } else {
                      setFilePath(String(row.path));
                      setOffset(0);
                    }
                  }}
                >
                  打开
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="row-actions">
          <button
            className="btn-secondary"
            disabled={cursors.length === 1}
            onClick={() => setCursors(cursors.slice(0, -1))}
          >
            上一页
          </button>
          <button
            className="btn-secondary"
            disabled={!fields?.next_cursor}
            onClick={() =>
              setCursors([...cursors, String(fields?.next_cursor)])
            }
          >
            下一页
          </button>
        </div>
        {file.error != null && <ErrorNote error={file.error} />}
        {read && (
          <>
            <JsonNote title="文件片段与实际版本" value={read} />
            {read.next_cursor != null && (
              <button
                className="btn-secondary"
                onClick={() => setOffset(Number(read.next_cursor))}
              >
                下一片段
              </button>
            )}
            {schemas && !read.binary && !read.next_cursor && offset === 0 && (
              <EnvironmentAction
                key={`${filePath}:${read.version}`}
                schema={schemas.file_actions as Row}
                terminal={false}
                props={props}
                initial={{
                  path: filePath,
                  text: read.text || "",
                  expected_version: read.version,
                }}
              />
            )}
          </>
        )}
        {schema.error != null && <ErrorNote error={schema.error} />}
        {schemas && !read && (
          <EnvironmentAction
            schema={schemas.file_actions as Row}
            terminal={false}
            props={props}
          />
        )}
      </Section>
      {schemas && (
        <Section title="Yuki 的原持久终端">
          <p className="small">
            显式执行、输入和中断。完成记录留在原 Manager
            与管理审计，不触发聊天续轮。
          </p>
          <EnvironmentAction
            schema={schemas.terminal_actions as Row}
            terminal
            props={props}
          />
        </Section>
      )}
    </>
  );
}

function ArtifactEditor({
  id,
  fields,
  props,
}: {
  id: string;
  fields: Row;
  props: PageProps;
}) {
  const [name, setName] = useState(String(fields.name || "")),
    [body, setBody] = useState(String(fields.text || ""));
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        props.act({
          method: "mutate_workspace",
          label: "保存共享文件",
          revision: Number(fields.revision),
          payload: {
            resource_id: id,
            action: "edit",
            spec: { name, text: body },
          },
          review: { name, original_revision: fields.revision, text: body },
        });
      }}
    >
      <label className="form-group">
        文件名
        <input
          className="form-control"
          required
          maxLength={128}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="form-group">
        UTF-8 内容
        <textarea
          className="form-control"
          rows={12}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </label>
      <button
        className="btn-primary"
        disabled={!props.allowed("mutate_workspace")}
      >
        检查并保存
      </button>
    </form>
  );
}

export function Files(props: PageProps) {
  const [id, setId] = useState(""),
    [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  const file = useQuery<Row>(
    "read_workspace",
    { artifact_id: id },
    props.refresh,
    !!id && props.allowed("read_workspace"),
  );
  const fields = file.data?.fields as Row | undefined;
  async function upload(file: File) {
    setError(null);
    setBusy(true);
    try {
      if (file.size > 640 * 1024)
        throw new Error("单次上传最多 640 KiB；大型文件可通过持久终端处理。");
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 16384)
        binary += String.fromCharCode(
          ...bytes.subarray(offset, offset + 16384),
        );
      props.act({
        method: "mutate_workspace",
        label: "上传到共享工作区",
        revision: 0,
        payload: {
          resource_id: "yuki",
          action: "upload",
          spec: { name: file.name, base64: btoa(binary) },
        },
        review: { name: file.name, size: file.size },
        onReceipt: (_request, value) => {
          if (value.success) setId(String(value.resource_id));
        },
      });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Section title="Yuki 的共享文件与已发布快照">
        <label className="form-group">
          上传文件（最多 640 KiB）
          <input
            type="file"
            disabled={busy || !props.allowed("mutate_workspace")}
            onChange={(e) => {
              const selected = e.target.files?.[0];
              if (selected) void upload(selected);
              e.target.value = "";
            }}
          />
        </label>
        {error != null && <ErrorNote error={error} />}
        <QueryList
          method="list_workspace"
          refresh={props.refresh}
          onRow={flatten}
          columns={[
            ["name", "文件"],
            ["size", "字节"],
            ["revision", "版本"],
            ["immutable", "快照", (value) => <Badge value={value} />],
            ["modified_at", "修改时间", stamp],
          ]}
          actions={(row) => (
            <>
              <button
                className="btn-secondary"
                onClick={() => setId(String(row.resource_id))}
              >
                打开
              </button>
              {props.allowed("download_workspace") && (
                <a
                  className="btn-secondary"
                  href={`/api/control/files/workspace/${encodeURIComponent(String(row.resource_id))}`}
                  download
                >
                  下载
                </a>
              )}
              <button
                className="btn-secondary"
                disabled={!props.allowed("mutate_workspace")}
                onClick={() =>
                  props.act({
                    method: "mutate_workspace",
                    label: "删除共享文件快照",
                    revision: Number(row.revision),
                    payload: { resource_id: row.resource_id, action: "delete" },
                    review: { name: row.name, revision: row.revision },
                    hint: "删除 artifact 不会删除原持久 Linux 文件。",
                  })
                }
              >
                删除
              </button>
            </>
          )}
        />
      </Section>
      {id && (
        <Section title={fields ? text(fields.name) : "文件内容"}>
          {file.error != null && <ErrorNote error={file.error} />}
          {fields && (
            <>
              <p className="small">
                版本 {text(fields.revision)} · SHA256 {text(fields.sha256)}
              </p>
              {fields.binary ? (
                <Empty>二进制文件，请下载或使用持久终端处理。</Empty>
              ) : (
                <pre className="file-preview">{text(fields.text)}</pre>
              )}
              {fields.truncated === true && (
                <p className="small">预览已截断，不能用片段覆盖完整文件。</p>
              )}
              {!fields.binary && !fields.truncated && !fields.immutable && (
                <ArtifactEditor
                  key={`${id}:${fields.revision}`}
                  id={id}
                  fields={fields}
                  props={props}
                />
              )}
            </>
          )}
        </Section>
      )}
      <Environment props={props} />
    </>
  );
}
