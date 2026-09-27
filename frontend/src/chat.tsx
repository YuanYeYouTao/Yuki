import { stamp, text } from "./format";
import { useQuery } from "./hooks";
import { useState } from "react";
import type { Row, Page } from "./api";
import { Empty, ErrorNote, Icon, JsonNote } from "./components";
import { Traces } from "./traces";

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
  const [cursor, setCursor] = useState<string | null>(null),
    [older, setOlder] = useState<Row[]>([]),
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
      page: { limit: 40, cursor },
    },
    refresh,
    !!conversation,
  );
  const key = `${conversation}:${refresh}:${JSON.stringify(filter)}`;
  const [pageKey, setPageKey] = useState(key);
  if (pageKey !== key) {
    setPageKey(key);
    setCursor(null);
    setOlder([]);
    setSelected(null);
  }
  const rows = [...(data?.items || []), ...older].sort(
    (a, b) => Number(a.event_id) - Number(b.event_id),
  );
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
            {data?.next_cursor && (
              <button
                className="btn-secondary"
                disabled={loading}
                onClick={() => {
                  setOlder(rows);
                  setCursor(data.next_cursor);
                }}
              >
                加载更早的消息
              </button>
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
                return (
                  <article
                    key={Number(row.event_id)}
                    className={`chat-block vertical ${sent ? "assistant" : "user"}`}
                  >
                    <div className="chat-header">
                      <div className="avatar">
                        <Icon name={sent ? "moon-star" : "cat"} />
                      </div>
                      <div className="sender-name">
                        {sent
                          ? "Yuki"
                          : text(
                              row.sender_display_name ||
                                row.author_person_id ||
                                row.author_kind,
                            )}
                        <time>{stamp(row.occurred_at)}</time>
                      </div>
                    </div>
                    <div className={`message ${sent ? "assistant" : "user"}`}>
                      {row.content === null
                        ? "消息正文未授权读取"
                        : text(row.content)}
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
                          <p className="small">
                            {row.attachment_indexes.map((v) => (
                              <a
                                key={Number(v)}
                                className="file-open"
                                target="_blank"
                                rel="noopener noreferrer"
                                href={`/api/control/files/chat/${encodeURIComponent(conversation)}/${row.event_id}/${Number(v)}`}
                              >
                                打开附件 {Number(v) + 1}{" "}
                              </a>
                            ))}
                          </p>
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
