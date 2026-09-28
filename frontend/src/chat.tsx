import { stamp, text } from "./format";
import { useQuery } from "./hooks";
import { useState } from "react";
import type { Row, Page } from "./api";
import { Empty, ErrorNote, Icon, JsonNote } from "./components";
import { Traces } from "./traces";
import { MediaPreview } from "./preview";
import { Avatar, useDisplayNames } from "./names";

export function Chat({
  conversation,
  content,
  refresh,
  notebook,
}: {
  conversation: string;
  content: boolean;
  refresh: number;
  notebook: React.ReactNode;
}) {
  const [pageNumber, setPageNumber] = useState(1),
    [pageInput, setPageInput] = useState("1"),
    [selected, setSelected] = useState<Row | null>(null);
  const [since, setSince] = useState(""),
    [until, setUntil] = useState(""),
    [filter, setFilter] = useState<Row>({});
  const [event, setEvent] = useState("");
  const { data, loading, error } = useQuery<Page>(
    "list_chat_events",
    {
      conversation_id: conversation,
      include_content: content,
      history: { descending: true, ...filter },
      page: { limit: 40, number: pageNumber },
    },
    refresh,
    !!conversation,
  );
  const key = `${conversation}:${refresh}:${JSON.stringify(filter)}`;
  const [pageKey, setPageKey] = useState(key);
  if (pageKey !== key) {
    setPageKey(key);
    setPageNumber(1);
    setPageInput("1");
    setSelected(null);
  }
  const rows = [...(data?.items || [])].sort(
    (a, b) =>
      new Date(String(b.occurred_at)).valueOf() -
        new Date(String(a.occurred_at)).valueOf() ||
      Number(b.event_id) - Number(a.event_id),
  );
  const names = useDisplayNames({
    person: rows.map((row) => String(row.author_person_id || "")),
  });
  const totalPages = Math.max(1, Math.ceil((data?.total || 0) / 40));
  const ids = new Set<number>();
  return (
    <>
      <div className="main-layout">
        <section className="panel">
          <h1 className="panel-header">
            <Icon name="chat-bubble" />
            聊天与活动
          </h1>
          <form
            className="chat-filters"
            onSubmit={(e) => {
              e.preventDefault();
              setFilter({
                ...(since ? { since: new Date(since).toISOString() } : {}),
                ...(until ? { until: new Date(until).toISOString() } : {}),
                ...(event ? { event_id: Number(event) } : {}),
              });
            }}
          >
            <label>
              从
              <input
                type="datetime-local"
                value={since}
                onChange={(e) => setSince(e.target.value)}
              />
            </label>
            <label>
              到
              <input
                type="datetime-local"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
              />
            </label>
            <label>
              事件
              <input
                type="number"
                min="1"
                value={event}
                placeholder="#"
                onChange={(e) => setEvent(e.target.value)}
              />
            </label>
            <button className="btn-secondary">查找</button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setSince("");
                setUntil("");
                setEvent("");
                setFilter({});
              }}
            >
              最新
            </button>
          </form>
          <div className="panel-content chat-messages">
            {!conversation && (
              <Empty>选择一个会话，翻阅 Yuki 接收和发送的消息。</Empty>
            )}
            {error != null && <ErrorNote error={error} />}
            {!!conversation && (
              <div className="pagination chat-pagination">
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={pageNumber <= 1 || loading}
                  onClick={() => {
                    setPageNumber(pageNumber - 1);
                    setPageInput(String(pageNumber - 1));
                  }}
                >
                  上一页
                </button>
                <span className="small">
                  第 {pageNumber} 页 / 共 {totalPages} 页 · 共{" "}
                  {data?.total ?? "?"} 条
                </span>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={
                    pageNumber >= totalPages || loading || data?.total == null
                  }
                  onClick={() => {
                    setPageNumber(pageNumber + 1);
                    setPageInput(String(pageNumber + 1));
                  }}
                >
                  下一页
                </button>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    const target = Number(pageInput);
                    if (
                      Number.isInteger(target) &&
                      target >= 1 &&
                      target <= totalPages
                    )
                      setPageNumber(target);
                  }}
                >
                  <label>
                    跳至{" "}
                    <input
                      aria-label="聊天页码"
                      type="number"
                      min="1"
                      max={totalPages}
                      value={pageInput}
                      onChange={(e) => setPageInput(e.target.value)}
                    />
                  </label>
                  <button className="btn-secondary">跳转</button>
                </form>
              </div>
            )}
            {loading && <Empty>正在翻阅…</Empty>}
            {conversation && !loading && !error && !rows.length && <Empty />}
            {rows
              .filter((row) => {
                const id = Number(row.event_id);
                if (ids.has(id)) return false;
                ids.add(id);
                return true;
              })
              .map((row) => {
                const sent = row.direction === "outbound";
                const senderName = sent
                  ? "Yuki"
                  : text(
                      names[String(row.author_person_id)] ||
                        row.sender_display_name ||
                        "未命名人物",
                    );
                return (
                  <article
                    key={Number(row.event_id)}
                    className={`chat-block vertical ${sent ? "assistant" : "user"}`}
                  >
                    <div className="chat-header">
                      <div className="avatar">
                        <Avatar
                          kind={sent ? "presence" : "person"}
                          id={
                            sent ? row.author_presence_id : row.author_person_id
                          }
                          label={`${senderName} 的头像`}
                          fallback={senderName}
                        />
                      </div>
                      <div className="sender-name">
                        {senderName}
                        <time>{stamp(row.occurred_at)}</time>
                      </div>
                    </div>
                    <div className={`message ${sent ? "assistant" : "user"}`}>
                      {row.content === null
                        ? "消息正文未授权读取"
                        : row.content
                          ? text(row.content)
                          : null}
                      {Array.isArray(row.media_references) &&
                        row.media_references.map((reference, index) => {
                          const media = reference as Row;
                          const path =
                            media.kind === "emoji"
                              ? `emoji/${encodeURIComponent(String(media.id))}`
                              : `workspace/${encodeURIComponent(String(media.id))}`;
                          return (
                            <MediaPreview
                              key={`${path}:${index}`}
                              title={
                                media.kind === "emoji" ? "表情" : "图片或文件"
                              }
                              url={`/api/control/files/${path}`}
                            />
                          );
                        })}
                      {!!row.audio_transcript && (
                        <p className="small">
                          语音：{text(row.audio_transcript)}
                        </p>
                      )}
                      {!!row.visual_summary && (
                        <p className="small">
                          视觉摘要：{text(row.visual_summary)}
                        </p>
                      )}
                      {Array.isArray(row.attachment_indexes) &&
                        row.attachment_indexes.length > 0 && (
                          <div className="chat-attachments">
                            {row.attachment_indexes.map((v) => (
                              <MediaPreview
                                key={Number(v)}
                                title={`附件 ${Number(v) + 1}`}
                                url={`/api/control/files/chat/${encodeURIComponent(conversation)}/${row.event_id}/${Number(v)}`}
                              />
                            ))}
                          </div>
                        )}
                      {!row.content &&
                        (!Array.isArray(row.media_references) ||
                          !row.media_references.length) &&
                        (!Array.isArray(row.attachment_indexes) ||
                          !row.attachment_indexes.length) && (
                          <span className="small">
                            这条事件没有保存可显示的正文或媒体
                          </span>
                        )}
                    </div>
                    <footer>
                      <button
                        className="file-open"
                        onClick={() => setSelected(row)}
                      >
                        #{text(row.event_id)} · 查看本轮
                      </button>
                      <span className="small">
                        {text(row.origin)}
                        {row.suppression_status === "duplicate"
                          ? " · 重复接入，未重复处理"
                          : ""}
                        {row.reply_to_event_id
                          ? ` · 引用 #${row.reply_to_event_id}`
                          : ""}
                        {row.caused_by_event_id
                          ? ` · 源事件 #${row.caused_by_event_id}`
                          : ""}
                      </span>
                    </footer>
                  </article>
                );
              })}
          </div>
        </section>
        <div className="right-container">
          <section className="panel">{notebook}</section>
        </div>
      </div>
      {selected != null && (
        <div className="chat-trace">
          <button className="btn-secondary" onClick={() => setSelected(null)}>
            收起事件 #{text(selected.event_id)}
          </button>
          <Traces
            scope={{
              conversation_id: conversation,
              [selected.direction === "outbound"
                ? "delivered_event_id"
                : "source_event_id"]: Number(selected.event_id),
            }}
            refresh={refresh}
          />
          <JsonNote
            title="事件关联"
            value={{
              event_id: selected.event_id,
              conversation_id: conversation,
            }}
          />
        </div>
      )}
    </>
  );
}
