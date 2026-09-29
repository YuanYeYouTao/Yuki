import { useEffect, useState } from "react";
import { query } from "./api";
import type { Row } from "./api";
import { Empty, ErrorNote, Section } from "./components";
import { originName, stamp, text } from "./format";
import { Traces } from "./traces";
import { TraceStepDetail } from "./trace-step-detail";

type Step = {
  id: number;
  operation_id?: string;
  kind: string;
  created_at: string;
  payload_status: string;
};
type TurnMessage = {
  event_id: number;
  direction: "received" | "sent";
  sender_display_name?: string | null;
  delivery_status?: "confirmed" | null;
  conversation_id: string;
  occurred_at: string;
  content: string | null;
  content_truncated?: boolean;
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
  tool_batch_start: "开始工具批次",
  tool_batch_end: "工具批次结束",
  tool_batch_error: "工具执行失败",
  tool_start: "调用工具",
  tool_end: "工具返回结果",
  tool_error: "工具调用失败",
  social_delivery: "消息已投递",
  model_route: "选择模型",
  provider_request: "发送模型请求",
  provider_response: "收到模型响应",
  tool_result_staged: "记录工具结果",
};

function TurnCard({
  turn,
  content,
  traceContent,
}: {
  turn: Turn;
  content: boolean;
  traceContent: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [showSteps, setShowSteps] = useState(false);
  const [showOperations, setShowOperations] = useState(false);
  const [selectedStepId, setSelectedStepId] = useState<number | null>(null);
  const [olderSteps, setOlderSteps] = useState<Step[]>([]);
  const [olderMessages, setOlderMessages] = useState<TurnMessage[]>([]);
  const [hasOlder, setHasOlder] = useState<boolean | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<unknown>(null);
  const steps = [
    ...new Map(
      [...olderSteps, ...turn.steps].map((step) => [step.id, step]),
    ).values(),
  ].sort((a, b) => a.id - b.id);
  const messages = [
    ...new Map(
      [...olderMessages, ...(turn.messages || [])].map((message) => [
        message.event_id,
        message,
      ]),
    ).values(),
  ].sort((a, b) => a.event_id - b.event_id);
  const visibleSteps = showSteps ? steps : steps.slice(-3);
  const operations = steps.filter(
    (step) =>
      ["recorded", "redacted"].includes(step.payload_status) &&
      ([
        "tool_start",
        "tool_end",
        "model_route",
        "model_start",
        "model_end",
        "provider_start",
        "provider_response",
        "social_delivery",
      ].includes(step.kind) ||
        step.kind.endsWith("_error")),
  );
  const recentOperations = operations.slice(-8);
  const selectedStep = steps.find((step) => step.id === selectedStepId);
  const family = selectedStep?.kind.replace(/_(start|end|error)$/, "");
  const pairedStep =
    selectedStep?.operation_id && family
      ? steps.find(
          (step) =>
            step.id !== selectedStep.id &&
            step.operation_id === selectedStep.operation_id &&
            step.kind.replace(/_(start|end|error)$/, "") === family &&
            step.kind !== selectedStep.kind,
        )
      : undefined;
  const detailSteps = selectedStep
    ? [selectedStep, ...(pairedStep ? [pairedStep] : [])].sort(
        (a, b) => a.id - b.id,
      )
    : [];
  const operationStart = new Map<string, Step>(
    steps
      .filter((step) => step.kind.endsWith("_start") && step.operation_id)
      .map((step) => [step.operation_id!, step]),
  );
  const stepLabel = (step: Step) => {
    const name = stepName[step.kind] || text(step.kind);
    const start = step.operation_id
      ? operationStart.get(step.operation_id)
      : undefined;
    const elapsed = start
      ? Date.parse(step.created_at) - Date.parse(start.created_at)
      : null;
    return step.kind.endsWith("_end") && elapsed != null && elapsed >= 0
      ? `${name} · ${(elapsed / 1000).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} 秒`
      : name;
  };
  const activePhase: Record<string, string> = {
    chat_processing_start: "正在处理收到的消息",
    model_start: "正在等待模型回复",
    provider_start: "正在等待 Provider 返回",
    tool_batch_start: "正在执行工具",
    turn_start: "本轮已开始",
  };
  async function loadOlder() {
    const beforeStepId = steps[0]?.id;
    if (beforeStepId == null || loadingOlder) return;
    setLoadingOlder(true);
    setOlderError(null);
    try {
      const response = await query<Row>("read_conversation_execution", {
        conversation_id: turn.original_conversation_id,
        turn_id: turn.turn_id,
        before_step_id: beforeStepId,
        include_content: content,
      });
      const page = (response.fields || response) as Turn;
      if (
        page.turn_id !== turn.turn_id ||
        page.original_conversation_id !== turn.original_conversation_id
      )
        throw new Error("较早执行记录的轮次不匹配");
      setOlderSteps((current) => [...current, ...page.steps]);
      setOlderMessages((current) => [...current, ...(page.messages || [])]);
      setHasOlder(page.steps_truncated === true && page.steps.length > 0);
    } catch (error) {
      setOlderError(error);
    } finally {
      setLoadingOlder(false);
    }
  }
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
      {!!messages.length && (
        <div className="turn-messages" aria-label="本轮收发消息">
          {messages.map((message) => (
            <div
              key={message.event_id}
              className={`turn-message ${message.direction}`}
            >
              <strong>
                {message.direction === "received" ? "收到" : "发出"} ·{" "}
                {message.sender_display_name ||
                  (message.direction === "received" ? "发送者未记录" : "Yuki")}
                {" · "}
                {stamp(message.occurred_at)}
              </strong>
              <span>
                {content
                  ? message.content || "这条消息没有保存文本正文"
                  : "消息正文未授权读取"}
              </span>
              {message.content_truncated && (
                <small>这里只显示前 500 字；完整消息请到聊天时间线查看。</small>
              )}
              {message.delivery_status === "confirmed" && (
                <small>已由投递回执确认并写入发送账本</small>
              )}
              {message.conversation_id !== turn.original_conversation_id && (
                <small>发送到其他会话 · {message.conversation_id}</small>
              )}
              <small>内部事件 #{message.event_id}</small>
            </div>
          ))}
        </div>
      )}
      {!messages.length && (
        <p className="small">
          这段诊断没有可核验的收发事件，不能据此判断本轮没有收到或发出消息。
        </p>
      )}
      {(hasOlder ?? turn.messages_truncated) && (
        <p className="small">
          这里只显示已加载执行记录关联的消息；可继续加载更早记录。
        </p>
      )}
      {(hasOlder ?? turn.steps_truncated) && (
        <button
          type="button"
          className="file-open"
          disabled={loadingOlder}
          onClick={() => void loadOlder()}
        >
          {loadingOlder ? "正在加载更早记录…" : "加载更早的状态和收发消息"}
        </button>
      )}
      {olderError != null && <ErrorNote error={olderError} />}
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
      {traceContent && operations.length > 0 && (
        <button
          type="button"
          className="file-open"
          onClick={() => {
            setShowOperations((value) => !value);
            setSelectedStepId(null);
          }}
        >
          {showOperations
            ? "收起本轮具体操作"
            : `查看本轮具体操作（${operations.length} 条）`}
        </button>
      )}
      {showOperations && (
        <div
          className="trace-operation-pair"
          role="group"
          aria-label="本轮具体操作"
        >
          {operations.length > recentOperations.length && (
            <p className="small">
              这里只展开最近 8 条关键操作；较早步骤可在下面逐项查看。
            </p>
          )}
          {recentOperations.map((step) => (
            <TraceStepDetail key={step.id} id={step.id} />
          ))}
        </div>
      )}
      <ol className="live-steps" aria-label="最近执行状态">
        {visibleSteps.map((step) => (
          <li key={step.id}>
            <time>{stamp(step.created_at)}</time>
            <span>{stepLabel(step)}</span>
            {step.payload_status === "redacted" && (
              <span className="small"> · 部分字段已脱敏</span>
            )}
            {step.payload_status === "omitted_size" && (
              <span className="small"> · 内容过大未保存</span>
            )}
            {traceContent &&
              ["recorded", "redacted"].includes(step.payload_status) && (
                <button
                  type="button"
                  className="file-open"
                  aria-label={`查看记录 #${step.id} 的具体操作`}
                  aria-expanded={selectedStepId === step.id}
                  onClick={() => {
                    setShowOperations(false);
                    setSelectedStepId((current) =>
                      current === step.id ? null : step.id,
                    );
                  }}
                >
                  {selectedStepId === step.id
                    ? "收起细节"
                    : "查看实际参数与结果"}
                </button>
              )}
          </li>
        ))}
      </ol>
      {!traceContent && (
        <p className="small">工具参数、模型请求和结果需要执行正文读取权限。</p>
      )}
      {detailSteps.length > 0 && (
        <div className="trace-operation-pair" aria-label="本步的具体操作与结果">
          {detailSteps.map((step) => (
            <TraceStepDetail key={step.id} id={step.id} />
          ))}
        </div>
      )}
      {steps.length > 3 && (
        <button
          type="button"
          className="file-open"
          onClick={() => setShowSteps((value) => !value)}
        >
          {showSteps
            ? "收起状态记录"
            : `查看全部已加载的 ${steps.length} 条状态记录`}
        </button>
      )}
      {(hasOlder ?? turn.steps_truncated) && (
        <p className="small">本轮还有更早的步骤，使用上方按钮继续加载。</p>
      )}
      <button
        type="button"
        className="file-open"
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? "收起原始轨迹表" : "查看原始轨迹表"}
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
  traceContent = false,
}: {
  conversation: string;
  refresh: number;
  content?: boolean;
  traceContent?: boolean;
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
            <TurnCard
              key={turn.turn_id}
              turn={turn}
              content={content}
              traceContent={traceContent}
            />
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
                  <TurnCard
                    key={turn.turn_id}
                    turn={turn}
                    content={content}
                    traceContent={traceContent}
                  />
                ))}
            </>
          )}
          <p className="small">状态读取于 {stamp(data.observed_at)}</p>
        </div>
      )}
    </Section>
  );
}
