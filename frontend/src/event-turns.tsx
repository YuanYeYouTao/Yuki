import { useState } from "react";
import { query } from "./api";
import type { Page } from "./api";
import { Empty, ErrorNote, Section } from "./components";
import { originName, stamp, text } from "./format";
import { useQuery } from "./hooks";
import { Traces } from "./traces";

type EventTurn = {
  turn_id: string;
  origin: string | null;
  created_at: string;
  original_conversation_id: string;
  trace_status: string;
};

type EventTurnsProps = {
  conversation: string;
  eventId: number;
  direction: string;
  refresh: number;
};

export function EventTurns(props: EventTurnsProps) {
  return (
    <EventTurnsContent
      key={`${props.conversation}:${props.eventId}:${props.direction}:${props.refresh}`}
      {...props}
    />
  );
}

function EventTurnsContent({
  conversation,
  eventId,
  direction,
  refresh,
}: EventTurnsProps) {
  const args = {
    conversation_id: conversation,
    event_id: eventId,
    direction,
    page: { limit: 20, number: 1 },
  };
  const initial = useQuery<Page>("list_event_turns", args, refresh);
  const [extra, setExtra] = useState<EventTurn[]>([]);
  const [pageNumber, setPageNumber] = useState(1);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [chosen, setChosen] = useState<EventTurn | null>(null);
  const first = (initial.data?.items || []).map(
    (row) => (row.fields || row) as EventTurn,
  );
  const turns = [...first, ...extra];
  const hasMore = turns.length < (initial.data?.total || 0);
  const selected = chosen || (turns.length === 1 && !hasMore ? turns[0] : null);

  async function loadMore() {
    if (!hasMore || loadingMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await query<Page>("list_event_turns", {
        ...args,
        page: { limit: 20, number: pageNumber + 1 },
      });
      setExtra((items) => [
        ...items,
        ...page.items.map((row) => (row.fields || row) as EventTurn),
      ]);
      setPageNumber((number) => number + 1);
    } catch (reason) {
      setError(reason);
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <Section title={`事件 #${eventId} 的执行过程`}>
      {initial.loading && <Empty>正在查找关联轮次…</Empty>}
      {initial.error != null && <ErrorNote error={initial.error} />}
      {error != null && <ErrorNote error={error} />}
      {!initial.loading && !initial.error && !turns.length && (
        <Empty>
          没有可查看的执行轮次。该事件可能未触发 Yuki
          执行，或诊断已过期、关联未能记录。
        </Empty>
      )}
      {turns.length > 1 && (
        <div className="event-turn-choices">
          <p className="small">这条事件关联了多个轮次，请选择要查看的执行。</p>
          {turns.map((turn) => (
            <button
              key={turn.turn_id}
              type="button"
              className="btn-secondary"
              onClick={() => setChosen(turn)}
            >
              {stamp(turn.created_at)} ·{" "}
              {(turn.origin && originName[turn.origin]) ||
                turn.origin ||
                "Yuki"}{" "}
              ·{" "}
              {turn.trace_status === "completed"
                ? "已完成"
                : turn.trace_status === "failed"
                  ? "失败"
                  : "状态待确认"}
            </button>
          ))}
        </div>
      )}
      {hasMore && (
        <button
          type="button"
          className="btn-secondary"
          disabled={loadingMore}
          onClick={() => void loadMore()}
        >
          {loadingMore ? "正在加载…" : "加载更多关联轮次"}
        </button>
      )}
      {selected && (
        <Traces
          key={selected.turn_id}
          title={`本轮执行 · ${stamp(selected.created_at)}`}
          scope={{
            turn_id: selected.turn_id,
            conversation_id: selected.original_conversation_id,
          }}
          refresh={refresh}
        />
      )}
      {selected && (
        <details className="reference-id">
          <summary>内部关联编号</summary>
          {text(selected.turn_id)} · {text(selected.original_conversation_id)}
        </details>
      )}
    </Section>
  );
}
