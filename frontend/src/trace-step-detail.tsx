import { useQuery } from "./hooks";
import type { Row } from "./api";
import { Empty, ErrorNote } from "./components";
import { text } from "./format";

const object = (value: unknown): Row =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Row)
    : {};
const items = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];
const pretty = (value: unknown): string => {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2) || "无记录";
};

function DetailValue({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null) return null;
  return (
    <div className="trace-detail-value">
      <strong>{label}</strong>
      <pre>{pretty(value)}</pre>
    </div>
  );
}

function RecordedOperation({ kind, data }: { kind: string; data: Row }) {
  if (kind === "tool_start") {
    const call = object(data.call);
    const fn = object(call.function);
    return (
      <>
        <p>
          调用工具：<strong>{text(fn.name)}</strong>
        </p>
        <DetailValue label="实际参数" value={fn.arguments} />
      </>
    );
  }
  if (kind === "tool_end")
    return <DetailValue label="工具返回结果" value={data.result} />;
  if (kind === "tool_batch_start")
    return (
      <div>
        {items(data.calls).map((value, index) => {
          const fn = object(object(value).function);
          return (
            <div className="trace-detail-value" key={index}>
              <strong>
                {index + 1}. {text(fn.name)}
              </strong>
              <pre>{pretty(fn.arguments)}</pre>
            </div>
          );
        })}
      </div>
    );
  if (kind === "tool_batch_end") {
    const result = object(data.result);
    return (
      <>
        <p>
          实际执行 {text(result.executed_count)} 次 · 复用已有结果{" "}
          {text(result.reused_count)} 次
        </p>
        <DetailValue label="逐项工具结果" value={result.calls} />
      </>
    );
  }
  if (kind === "model_route")
    return (
      <p>
        用途 {text(data.task)} · Provider {text(data.provider)} · 模型{" "}
        {text(data.model)}
        <br />
        配置档 {text(data.profile_id)} · 协议 {text(data.protocol)}
      </p>
    );
  if (kind === "model_start") {
    const request = object(data.request);
    return (
      <>
        <p>
          用途 {text(data.task)} · 输入消息 {items(request.messages).length} 条
          · 声明工具 {items(request.tools).length} 个 · 输出预算{" "}
          {text(request.max_output_tokens)}
        </p>
        <details>
          <summary>查看实际模型输入与请求参数</summary>
          <DetailValue label="请求" value={request} />
        </details>
      </>
    );
  }
  if (kind === "model_end") {
    const result = object(data.result);
    return (
      <>
        <p>
          结果 {text(result.status)} · 输入 {text(result.prompt_tokens)} Token ·
          输出 {text(result.completion_tokens)} Token · 合计{" "}
          {text(result.total_tokens)} Token
        </p>
        <DetailValue label="模型回复" value={result.content} />
        {items(result.tool_calls).length > 0 && (
          <DetailValue label="模型提出的工具调用" value={result.tool_calls} />
        )}
      </>
    );
  }
  if (kind === "provider_start")
    return (
      <>
        <p>
          协议 {text(data.protocol)} · 状态 {text(data.dispatch)}
        </p>
        <details>
          <summary>查看实际发给 Provider 的请求</summary>
          <DetailValue label="请求正文" value={data.body} />
        </details>
      </>
    );
  if (kind === "provider_response") {
    const body = object(data.body);
    return (
      <>
        <p>
          HTTP {text(data.http_status)} · 模型 {text(body.model)}
        </p>
        <DetailValue label="上游报告用量" value={body.usage} />
        <details>
          <summary>查看 Provider 返回的可读部分</summary>
          <DetailValue label="响应正文" value={body} />
        </details>
      </>
    );
  }
  if (kind === "social_delivery")
    return (
      <p>
        确认投递的内部事件 #{text(data.event_id)} · 回执{" "}
        {text(data.social_operation_id)}
      </p>
    );
  if (kind.endsWith("_error"))
    return (
      <>
        <p>错误类别：{text(data.error_category)}</p>
        <DetailValue label="失败分类" value={data.failure} />
      </>
    );
  return <DetailValue label="记录的具体内容" value={data} />;
}

export function TraceStepDetail({ id }: { id: number }) {
  const { data, error, loading } = useQuery<Row>("read_execution_trace", {
    entry_id: id,
  });
  const evidence = object(data?.payload);
  const detail = object(evidence.data);
  return (
    <section
      className="trace-step-detail"
      aria-label={`记录 #${id} 的具体操作`}
    >
      <strong>
        记录 #{id} · {text(data?.kind)}
      </strong>
      {loading && <Empty>正在读取具体操作…</Empty>}
      {error != null && <ErrorNote error={error} />}
      {data && !data.payload && (
        <p className="small">这一步的具体内容未保存或已过期。</p>
      )}
      {data != null && Boolean(data.payload) && (
        <>
          {data.payload_status === "redacted" && (
            <p className="small">
              媒体或不透明 Provider 字段已脱敏；以下为仍可读取的记录。
            </p>
          )}
          <RecordedOperation kind={String(data.kind)} data={detail} />
        </>
      )}
    </section>
  );
}
