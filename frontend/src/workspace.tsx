import { lazy, Suspense, useState } from "react";
import type { Row } from "./api";
import { useQuery } from "./hooks";
import type { PageProps } from "./pages";
import { Empty, ErrorNote, QueryList, Section } from "./components";
import { DesktopWindow, MediaPreview } from "./preview";
import { FileManager } from "./workspace-manager";
import "./workspace.css";

const InteractiveTerminal = lazy(() =>
  import("./terminal-console").then((module) => ({
    default: module.InteractiveTerminal,
  })),
);

const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const imageName = (name: unknown) =>
  /\.(?:png|jpe?g|gif|webp|bmp|avif)$/i.test(String(name || ""));

/** Published artifacts are immutable snapshots, independent of the live Linux files. */
function PublishedSnapshots(props: PageProps) {
  const [id, setId] = useState("");
  const artifact = useQuery<Row>(
    "read_workspace",
    { artifact_id: id },
    props.refresh,
    !!id && props.allowed("read_workspace"),
  );
  const fields = artifact.data?.fields as Row | undefined;
  return (
    <details className="workspace-snapshots">
      <summary>已发布的文件快照（独立于当前工作区）</summary>
      <p className="small">
        快照用于原有消息交付和历史取证。当前文件的打开、编辑、重命名和删除请在上面的工作区中操作。
      </p>
      <QueryList
        method="list_workspace"
        refresh={props.refresh}
        onRow={flatten}
        columns={[
          ["name", "快照名"],
          ["size", "大小（字节）"],
          ["modified_at", "发布时间"],
        ]}
        actions={(row) => (
          <>
            <button
              className="btn-secondary"
              onClick={() => setId(String(row.resource_id))}
            >
              查看
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
          </>
        )}
      />
      {id && (
        <DesktopWindow
          title={String(fields?.name || "文件快照")}
          close={() => setId("")}
        >
          {artifact.error != null && <ErrorNote error={artifact.error} />}
          {!fields && !artifact.error && <Empty>正在打开快照…</Empty>}
          {fields && (
            <>
              <p className="small">
                独立快照 · 版本 {String(fields.revision || "未知")}
              </p>
              {imageName(fields.name) && props.allowed("download_workspace") ? (
                <MediaPreview
                  url={`/api/control/files/workspace/${encodeURIComponent(id)}`}
                  title={String(fields.name)}
                />
              ) : fields.binary ? (
                <Empty>二进制文件，请下载查看。</Empty>
              ) : (
                <pre className="file-preview">{String(fields.text ?? "")}</pre>
              )}
              {fields.truncated === true && (
                <p className="small">快照预览已截断。</p>
              )}
            </>
          )}
        </DesktopWindow>
      )}
    </details>
  );
}

export function Files(props: PageProps) {
  return (
    <>
      <Section title="Yuki 的持久工作区">
        <FileManager props={props} />
      </Section>
      <Section title="Yuki 的终端">
        <Suspense fallback={<Empty>正在准备终端…</Empty>}>
          <InteractiveTerminal props={props} />
        </Suspense>
      </Section>
      {props.allowed("list_workspace") && (
        <Section title="已发布内容">
          <PublishedSnapshots {...props} />
        </Section>
      )}
    </>
  );
}
