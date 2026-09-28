import { stamp, text } from "./format";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { query } from "./api";
import type { Row, Page } from "./api";
import { Empty, ErrorNote, Icon, JsonNote } from "./components";
import { EventTurns } from "./event-turns";
import { DesktopWindow, MediaPreview } from "./preview";
import { Avatar, useDisplayNames } from "./names";

const CHAT_BATCH = 40;
const CHAT_WINDOW = 160;
const CHAT_POLL_MS = 12000;
const EMPTY_ROWS: Row[] = [];

interface Timeline {
  scope: string;
  rows: Row[];
  olderCursor: string | null;
  loading: boolean;
  loadingOlder: boolean;
  error: unknown;
  trimmed: boolean;
  unread: boolean;
  latestId: number;
}

function eventId(row: Row): number {
  return Number(row.event_id);
}

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
  const [selected, setSelected] = useState<{ scope: string; row: Row } | null>(
    null,
  );
  const [since, setSince] = useState(""),
    [until, setUntil] = useState(""),
    [filter, setFilter] = useState<Row>({});
  const [event, setEvent] = useState("");
  const [searchVersion, setSearchVersion] = useState(0);
  const filterKey = JSON.stringify(filter);
  const scope = `${conversation}:${content}:${filterKey}:${searchVersion}`;
  const selectedRow = selected?.scope === scope ? selected.row : null;
  const [timeline, setTimeline] = useState<Timeline>({
    scope: "",
    rows: [],
    olderCursor: null,
    loading: false,
    loadingOlder: false,
    error: null,
    trimmed: false,
    unread: false,
    latestId: 0,
  });
  const current = timeline.scope === scope ? timeline : null;
  const rows = current?.rows ?? EMPTY_ROWS;
  const scrollRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const bottomPendingRef = useRef(false);
  const olderInFlightRef = useRef(false);
  const olderControllerRef = useRef<AbortController | null>(null);
  const requestVersionRef = useRef(0);
  const latestControllerRef = useRef<AbortController | null>(null);
  const pollControllerRef = useRef<AbortController | null>(null);
  const previousScrollRef = useRef(0);
  const anchorRef = useRef<{ id: number; top: number } | null>(null);
  const requestPage = useCallback(
    (cursor: string | null = null, signal?: AbortSignal) =>
      query<Page>(
        "list_chat_events",
        {
          conversation_id: conversation,
          include_content: content,
          history: { descending: true, ...JSON.parse(filterKey) },
          page: { limit: CHAT_BATCH, ...(cursor ? { cursor } : {}) },
        },
        signal,
      ),
    [conversation, content, filterKey],
  );
  const captureAnchor = useCallback(() => {
    const viewport = scrollRef.current;
    const list = listRef.current;
    if (!viewport || !list) return;
    const viewportTop = viewport.getBoundingClientRect().top;
    for (const child of list.querySelectorAll<HTMLElement>("[data-event-id]")) {
      const bounds = child.getBoundingClientRect();
      if (bounds.bottom > viewportTop) {
        anchorRef.current = {
          id: Number(child.dataset.eventId),
          top: bounds.top - viewportTop,
        };
        return;
      }
    }
  }, []);
  const restoreAnchor = useCallback(() => {
    const viewport = scrollRef.current;
    const anchor = anchorRef.current;
    const element = listRef.current?.querySelector<HTMLElement>(
      `[data-event-id="${anchor?.id}"]`,
    );
    if (!viewport || !anchor || !element) return;
    const nextTop =
      element.getBoundingClientRect().top -
      viewport.getBoundingClientRect().top;
    viewport.scrollTop += nextTop - anchor.top;
  }, []);
  const resetLatest = useCallback(() => {
    if (!conversation) return;
    latestControllerRef.current?.abort();
    olderControllerRef.current?.abort();
    olderControllerRef.current = null;
    olderInFlightRef.current = false;
    pollControllerRef.current?.abort();
    pollControllerRef.current = null;
    const version = ++requestVersionRef.current;
    bottomPendingRef.current = true;
    followRef.current = true;
    anchorRef.current = null;
    setTimeline({
      scope,
      rows: [],
      olderCursor: null,
      loading: true,
      loadingOlder: false,
      error: null,
      trimmed: false,
      unread: false,
      latestId: 0,
    });
    const controller = new AbortController();
    latestControllerRef.current = controller;
    void requestPage(null, controller.signal)
      .then((page) => {
        setTimeline((old) =>
          old.scope === scope && requestVersionRef.current === version
            ? {
                ...old,
                rows: [...page.items].reverse(),
                olderCursor: page.next_cursor,
                loading: false,
                latestId: Math.max(0, ...page.items.map(eventId)),
              }
            : old,
        );
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setTimeline((old) =>
            old.scope === scope && requestVersionRef.current === version
              ? { ...old, loading: false, error }
              : old,
          );
      });
    return () => {
      controller.abort();
      olderControllerRef.current?.abort();
      pollControllerRef.current?.abort();
    };
  }, [conversation, scope, requestPage]);
  useEffect(() => {
    if (!conversation) return;
    olderControllerRef.current?.abort();
    olderControllerRef.current = null;
    olderInFlightRef.current = false;
    pollControllerRef.current?.abort();
    pollControllerRef.current = null;
    const controller = new AbortController();
    latestControllerRef.current = controller;
    const version = ++requestVersionRef.current;
    bottomPendingRef.current = true;
    followRef.current = true;
    anchorRef.current = null;
    void requestPage(null, controller.signal)
      .then((page) => {
        if (requestVersionRef.current !== version) return;
        setTimeline({
          scope,
          rows: [...page.items].reverse(),
          olderCursor: page.next_cursor,
          loading: false,
          loadingOlder: false,
          error: null,
          trimmed: false,
          unread: false,
          latestId: Math.max(0, ...page.items.map(eventId)),
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || requestVersionRef.current !== version)
          return;
        setTimeline({
          scope,
          rows: [],
          olderCursor: null,
          loading: false,
          loadingOlder: false,
          error,
          trimmed: false,
          unread: false,
          latestId: 0,
        });
      });
    return () => {
      controller.abort();
      olderControllerRef.current?.abort();
      olderControllerRef.current = null;
      olderInFlightRef.current = false;
      pollControllerRef.current?.abort();
    };
  }, [conversation, scope, requestPage]);
  useLayoutEffect(() => {
    const viewport = scrollRef.current;
    if (!viewport) return;
    if (bottomPendingRef.current) {
      viewport.scrollTop = viewport.scrollHeight;
      bottomPendingRef.current = false;
    } else if (!followRef.current) {
      restoreAnchor();
    }
  }, [rows, restoreAnchor]);
  useEffect(() => {
    const list = listRef.current;
    if (!list || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const viewport = scrollRef.current;
      if (!viewport) return;
      if (followRef.current) viewport.scrollTop = viewport.scrollHeight;
      else restoreAnchor();
    });
    observer.observe(list);
    return () => observer.disconnect();
  }, [scope, restoreAnchor]);
  const loadOlder = useCallback(() => {
    if (
      !current?.olderCursor ||
      current.loading ||
      current.loadingOlder ||
      olderInFlightRef.current
    )
      return;
    olderInFlightRef.current = true;
    const controller = new AbortController();
    olderControllerRef.current = controller;
    const version = requestVersionRef.current;
    captureAnchor();
    setTimeline((old) => ({ ...old, loadingOlder: true }));
    void requestPage(current.olderCursor, controller.signal)
      .then((page) => {
        setTimeline((old) => {
          if (old.scope !== scope || requestVersionRef.current !== version)
            return old;
          const seen = new Set(old.rows.map(eventId));
          const older = [...page.items]
            .reverse()
            .filter((row) => !seen.has(eventId(row)));
          const combined = [...older, ...old.rows];
          return {
            ...old,
            rows: combined.slice(0, CHAT_WINDOW),
            olderCursor: page.next_cursor,
            loadingOlder: false,
            trimmed: old.trimmed || combined.length > CHAT_WINDOW,
          };
        });
      })
      .catch((error: unknown) => {
        setTimeline((old) =>
          old.scope === scope && requestVersionRef.current === version
            ? { ...old, loadingOlder: false, error }
            : old,
        );
      })
      .finally(() => {
        if (olderControllerRef.current === controller) {
          olderControllerRef.current = null;
          olderInFlightRef.current = false;
        }
      });
  }, [current, captureAnchor, requestPage, scope]);
  const pollLatest = useCallback(() => {
    if (
      !conversation ||
      filterKey !== "{}" ||
      document.visibilityState === "hidden" ||
      pollControllerRef.current
    )
      return;
    const version = requestVersionRef.current;
    const controller = new AbortController();
    pollControllerRef.current = controller;
    void requestPage(null, controller.signal)
      .then((page) => {
        setTimeline((old) => {
          if (
            old.scope !== scope ||
            old.loading ||
            requestVersionRef.current !== version
          )
            return old;
          const newestId = Math.max(0, ...page.items.map(eventId));
          if (newestId <= old.latestId) return old;
          if (!followRef.current || old.trimmed)
            return { ...old, unread: true, latestId: newestId };
          const visibleIds = new Set(old.rows.map(eventId));
          if (
            old.rows.length &&
            !page.items.some((row) => visibleIds.has(eventId(row)))
          ) {
            bottomPendingRef.current = true;
            return {
              ...old,
              rows: [...page.items].reverse(),
              olderCursor: page.next_cursor,
              latestId: newestId,
            };
          }
          const additions = [...page.items]
            .reverse()
            .filter((row) => !visibleIds.has(eventId(row)));
          const combined = [...old.rows, ...additions];
          bottomPendingRef.current = true;
          return combined.length <= CHAT_WINDOW
            ? { ...old, rows: combined, latestId: newestId }
            : {
                ...old,
                rows: [...page.items].reverse(),
                olderCursor: page.next_cursor,
                latestId: newestId,
              };
        });
      })
      .catch(() => {
        /* A later poll or manual refresh can retry this read. */
      })
      .finally(() => {
        if (pollControllerRef.current === controller)
          pollControllerRef.current = null;
      });
  }, [conversation, filterKey, requestPage, scope]);
  useEffect(() => {
    const timer = window.setInterval(pollLatest, CHAT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [pollLatest]);
  const previousRefreshRef = useRef(refresh);
  useEffect(() => {
    if (previousRefreshRef.current !== refresh) {
      previousRefreshRef.current = refresh;
      pollLatest();
    }
  }, [refresh, pollLatest]);
  const names = useDisplayNames({
    person: rows.map((row) => String(row.author_person_id || "")),
  });
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
                ...(event ? { through_event_id: Number(event) } : {}),
              });
              setSearchVersion((version) => version + 1);
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
                setSearchVersion((version) => version + 1);
              }}
            >
              最新
            </button>
          </form>
          <div
            className="panel-content chat-messages"
            ref={scrollRef}
            onScroll={(scrollEvent) => {
              const viewport = scrollEvent.currentTarget;
              const top = viewport.scrollTop;
              const movingUp = top < previousScrollRef.current - 1;
              previousScrollRef.current = top;
              const nearBottom =
                viewport.scrollHeight - top - viewport.clientHeight < 72;
              followRef.current = nearBottom && !current?.trimmed;
              if (nearBottom && current?.unread) resetLatest();
              else {
                if (!nearBottom) captureAnchor();
                if (movingUp && top < 180) loadOlder();
              }
            }}
          >
            {!conversation && (
              <Empty>选择一个会话，翻阅 Yuki 接收和发送的消息。</Empty>
            )}
            {current?.error != null && <ErrorNote error={current.error} />}
            {conversation && (!current || current.loading) && (
              <Empty>正在翻阅…</Empty>
            )}
            {conversation &&
              current &&
              !current.loading &&
              !current.error &&
              !rows.length && <Empty />}
            {!!conversation && !!current?.olderCursor && (
              <button
                type="button"
                className="btn-secondary chat-older"
                disabled={current.loadingOlder}
                onClick={loadOlder}
              >
                {current.loadingOlder ? "正在加载更早消息…" : "加载更早消息"}
              </button>
            )}
            {(current?.trimmed || current?.unread) && (
              <button
                className="btn-secondary chat-return-latest"
                onClick={resetLatest}
              >
                {current.unread
                  ? "有新消息，回到底部"
                  : "较新消息已暂时收起，回到最新"}
              </button>
            )}
            <div className="chat-timeline" ref={listRef}>
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
                      data-event-id={Number(row.event_id)}
                      className={`chat-block vertical ${sent ? "assistant" : "user"}`}
                    >
                      <div className="chat-header">
                        <div className="avatar">
                          <Avatar
                            kind={sent ? "presence" : "person"}
                            id={
                              sent
                                ? row.author_presence_id
                                : row.author_person_id
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
                          onClick={() => setSelected({ scope, row })}
                        >
                          #{text(row.event_id)} · 查看事件与执行
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
          </div>
        </section>
        <div className="right-container">
          <section className="panel">{notebook}</section>
        </div>
      </div>
      {selectedRow != null && (
        <DesktopWindow
          title={`事件 #${text(selectedRow.event_id)} 的执行过程`}
          close={() => setSelected(null)}
        >
          <EventTurns
            key={`${selectedRow.event_id}:${selectedRow.direction}`}
            conversation={conversation}
            eventId={Number(selectedRow.event_id)}
            direction={String(selectedRow.direction)}
            refresh={refresh}
          />
          <JsonNote
            title="事件关联"
            value={{
              event_id: selectedRow.event_id,
              conversation_id: conversation,
            }}
          />
        </DesktopWindow>
      )}
    </>
  );
}
