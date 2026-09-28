import { useEffect, useState } from "react";
import { query } from "./api";
import type { Row } from "./api";
import { Empty, ErrorNote, Section } from "./components";
import { originName, stamp, text } from "./format";
import { Traces } from "./traces";

type Step = {
  id: number;
  kind: string;
  created_at: string;
  payload_status: string;
};
type Turn = {
  turn_id: string;
  original_conversation_id: string;
  origin: string | null;
  started_at: string;
  last_step_at: string;
  latest_kind: string;
  status: string;
  steps: Step[];
  step_count?: number;
  steps_truncated?: boolean;
};
type Activity = {
  conversation_id: string;
  observed_at: string;
  state: "active" | "idle" | "evidence_insufficient";
  active: Turn[];
  recent: Turn[];
  coverage_note?: string | null;
};

const stepName: Record<string, string> = {
  chat_processing_start: "接收并处理消息",
  chat_processing_end: "消息处理结束",
  turn_start: "开始这一轮",
  turn_end: "这一轮完成",
  turn_error: "这一轮失败",
  model_start: "向模型请求",
  model_end: "模型已回复",
  model_error: "模型请求失败",
  provider_start: "联系模型服务",
  provider_end: "模型服务已返回",
  provider_error: "模型服务失败",
  tool_batch_start: "执行工具",
  tool_batch_end: "工具已返回",
  tool_batch_error: "工具执行失败",
  social_delivery: "消息已投递",
  model_route: "选择模型",
  provider_request: "发送模型请求",
  provider_response: "收到模型响应",
  tool_result_staged: "记录工具结果",
};

function TurnCard({ turn }: { turn: Turn }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <article className="live-turn">
      <header>
        <strong>
          {turn.status === "active"
            ? "正在执行"
            : turn.status === "completed"
              ? "已完成"
              : turn.status === "failed"
                ? "执行失败"
                : "状态未能确认"}
        </strong>
        <time>{stamp(turn.started_at)}</time>
      </header>
      <p className="small">
        {turn.origin
          ? originName[turn.origin] || text(turn.origin)
          : "Yuki 的轮次"}{" "}
        · 最近步骤：
        {stepName[turn.latest_kind] || text(turn.latest_kind)}
      </p>
      <ol className="live-steps">
        {turn.steps.map((step) => (
          <li key={step.id}>
            <time>{stamp(step.created_at)}</time>
            <span>{stepName[step.kind] || text(step.kind)}</span>
            {step.payload_status !== "recorded" && (
              <span className="small"> · 正文 {text(step.payload_status)}</span>
            )}
          </li>
        ))}
      </ol>
      {turn.steps_truncated && (
        <p className="small">
          此处只显示最近 {turn.steps.length} 步；展开后可查看其余记录。
        </p>
      )}
      <button
        type="button"
        className="file-open"
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? "收起完整执行过程" : "展开模型、工具和结果"}
      </button>
      {expanded && (
        <Traces
          title="完整执行过程"
          refresh={turn.status === "active" ? (turn.steps.at(-1)?.id ?? 0) : 0}
          scope={{
            turn_id: turn.turn_id,
            conversation_id: turn.original_conversation_id,
          }}
        />
      )}
    </article>
  );
}

export function LiveSession({
  conversation,
  refresh,
}: {
  conversation: string;
  refresh: number;
}) {
  const [state, setState] = useState<{
    conversation: string;
    data: Activity | null;
    error: unknown;
  }>({ conversation: "", data: null, error: null });
  const [showRecent, setShowRecent] = useState(false);
  useEffect(() => {
    if (!conversation) return;
    let disposed = false;
    let inFlight = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function poll() {
      if (disposed || inFlight) return;
      inFlight = true;
      try {
        const response = await query<Row>(
          "read_conversation_execution",
          { conversation_id: conversation },
          controller.signal,
        );
        if (disposed) return;
        const fields = (response.fields || response) as Activity;
        setState({ conversation, data: fields, error: null });
        timer = setTimeout(
          poll,
          document.hidden ? 30000 : fields.state === "active" ? 4000 : 15000,
        );
      } catch (error) {
        if (disposed) return;
        setState({ conversation, data: null, error });
        timer = setTimeout(poll, document.hidden ? 30000 : 15000);
      } finally {
        inFlight = false;
      }
    }
    function onVisibilityChange() {
      if (disposed || document.hidden) return;
      if (timer) clearTimeout(timer);
      timer = undefined;
      if (!inFlight) void poll();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    void poll();
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [conversation, refresh]);

  const data = state.conversation === conversation ? state.data : null;
  const error = state.conversation === conversation ? state.error : null;
  return (
    <Section title="现在的 Yuki">
      {!conversation && <Empty>选择会话后查看 Yuki 此刻的执行过程。</Empty>}
      {conversation && error != null && <ErrorNote error={error} />}
      {conversation && !data && !error && <Empty>正在读取当前会话…</Empty>}
      {data && (
        <div className="live-session" aria-live="polite">
          <p className="live-status">
            {data.state === "active"
              ? `正在执行 ${data.active.length} 个轮次`
              : data.state === "idle"
                ? "当前没有执行"
                : "当前状态证据不足"}
          </p>
          {data.coverage_note && (
            <p className="small">
              {data.coverage_note ===
              "diagnostic_history_is_bounded_and_may_be_incomplete"
                ? "执行记录有保留期限；较早步骤或写入失败的步骤可能无法查看。"
                : text(data.coverage_note)}
            </p>
          )}
          {data.active.map((turn) => (
            <TurnCard key={turn.turn_id} turn={turn} />
          ))}
          {!!data.recent.length && (
            <>
              <button
                type="button"
                className="file-open"
                onClick={() => setShowRecent((value) => !value)}
              >
                {showRecent ? "收起最近轮次" : "查看最近一次轮次"}
              </button>
              {showRecent &&
                data.recent.map((turn) => (
                  <TurnCard key={turn.turn_id} turn={turn} />
                ))}
            </>
          )}
          <p className="small">状态读取于 {stamp(data.observed_at)}</p>
        </div>
      )}
    </Section>
  );
}
