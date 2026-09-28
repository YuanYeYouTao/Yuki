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
type TurnMessage = {
  event_id: number;
  direction: "received" | "sent";
  conversation_id: string;
  occurred_at: string;
  content: string | null;
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
  messages?: TurnMessage[];
  messages_truncated?: boolean;
  usage?: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cached_input_tokens: number;
    missing_usage_calls: number;
  } | null;
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

function TurnCard({ turn, content }: { turn: Turn; content: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [showSteps, setShowSteps] = useState(false);
  const visibleSteps = showSteps ? turn.steps : turn.steps.slice(-3);
  const activePhase: Record<string, string> = {
    chat_processing_start: "正在处理收到的消息",
    model_start: "正在等待模型回复",
    provider_start: "正在等待 Provider 返回",
    tool_batch_start: "正在执行工具",
    turn_start: "本轮已开始",
  };
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
      <p className="live-phase">
        {turn.origin
          ? originName[turn.origin] || text(turn.origin)
          : "Yuki 的轮次"}
        {" · "}
        {turn.status === "active"
          ? activePhase[turn.latest_kind] ||
            `运行中 · ${stepName[turn.latest_kind] || text(turn.latest_kind)}`
          : stepName[turn.latest_kind] || text(turn.latest_kind)}
      </p>
      {!!turn.messages?.length && (
        <div className="turn-messages" aria-label="本轮收发消息">
          {turn.messages.map((message) => (
            <div
              key={message.event_id}
              className={`turn-message ${message.direction}`}
            >
              <strong>
                {message.direction === "received" ? "收到" : "发出"} ·{" "}
                {stamp(message.occurred_at)}
              </strong>
              <span>
                {content
                  ? message.content || "这条消息没有保存文本正文"
                  : "消息正文未授权读取"}
              </span>
              {message.conversation_id !== turn.original_conversation_id && (
                <small>发送到其他会话 · {message.conversation_id}</small>
              )}
              <small>内部事件 #{message.event_id}</small>
            </div>
          ))}
        </div>
      )}
      {!turn.messages?.length && (
        <p className="small">
          这段诊断没有可核验的收发事件，不能据此判断本轮没有收到或发出消息。
        </p>
      )}
      {turn.messages_truncated && (
        <p className="small">
          这里只显示最近执行记录关联的消息；较早消息请在聊天时间线查看。
        </p>
      )}
      {turn.usage && (
        <p className="small turn-usage">
          本轮 Token {turn.usage.total_tokens.toLocaleString("zh-CN")} · 输入{" "}
          {turn.usage.input_tokens.toLocaleString("zh-CN")}（其中缓存{" "}
          {turn.usage.cached_input_tokens.toLocaleString("zh-CN")}） / 输出{" "}
          {turn.usage.output_tokens.toLocaleString("zh-CN")} ·{" "}
          {turn.usage.calls} 次调用
          {turn.usage.missing_usage_calls > 0 &&
            ` · ${turn.usage.missing_usage_calls} 次上游未报总 Token`}
        </p>
      )}
      <ol className="live-steps" aria-label="最近执行状态">
        {visibleSteps.map((step) => (
          <li key={step.id}>
            <time>{stamp(step.created_at)}</time>
            <span>{stepName[step.kind] || text(step.kind)}</span>
            {step.payload_status !== "recorded" && (
              <span className="small"> · 正文 {text(step.payload_status)}</span>
            )}
          </li>
        ))}
      </ol>
      {turn.steps.length > 3 && (
        <button
          type="button"
          className="file-open"
          onClick={() => setShowSteps((value) => !value)}
        >
          {showSteps
            ? "收起状态记录"
            : `查看全部 ${turn.steps.length} 条状态记录`}
        </button>
      )}
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
  content = false,
}: {
  conversation: string;
  refresh: number;
  content?: boolean;
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
          { conversation_id: conversation, include_content: content },
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
  }, [conversation, refresh, content]);

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
            <TurnCard key={turn.turn_id} turn={turn} content={content} />
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
                  <TurnCard key={turn.turn_id} turn={turn} content={content} />
                ))}
            </>
          )}
          <p className="small">状态读取于 {stamp(data.observed_at)}</p>
        </div>
      )}
    </Section>
  );
}
