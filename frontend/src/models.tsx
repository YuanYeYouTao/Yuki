import { useState } from "react";
import { useQuery } from "./hooks";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { ConfigFile } from "./config-files";
import {
  Badge,
  Empty,
  ErrorNote,
  QueryList,
  Section,
  Table,
} from "./components";
import { stamp } from "./format";
const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const status = (value: unknown) => <Badge value={value} />;
const count = (value: unknown) => Number(value || 0).toLocaleString("zh-CN");
const knownCount = (value: unknown) => (value == null ? "—" : count(value));
const amount = (row: Row, key: string) => Math.max(0, Number(row[key] || 0));
const cacheRate = (usage: Row) => {
  const input = Number(usage.cache_reported_input_tokens || 0);
  const cached = Number(usage.cache_reported_cached_tokens || 0);
  return input > 0 ? `${((cached / input) * 100).toFixed(1)}%` : "—";
};
const confirmedCacheShare = (usage: Row) => {
  const input = Number(usage.input_tokens || 0);
  const cached = Number(usage.cached_input_tokens || 0);
  return input > 0 ? `${((cached / input) * 100).toFixed(1)}%` : "—";
};
function CacheReport({ usage }: { usage: Row }) {
  const missing = Number(usage.cache_unreported_calls || 0);
  return (
    <span>
      {missing > 0 ? "已确认缓存占已记录输入" : "缓存命中率"}{" "}
      {missing > 0 ? confirmedCacheShare(usage) : cacheRate(usage)} · 命中{" "}
      {count(usage.cached_input_tokens)} Token
      {missing > 0 &&
        ` · 已报告子集命中率 ${cacheRate(usage)}（分母 ${count(usage.cache_reported_input_tokens)} 输入 Token） · ${count(missing)} 次调用未报告缓存量`}
    </span>
  );
}
function tokenParts(row: Row) {
  const input = amount(row, "input_tokens");
  const reported = Math.min(input, amount(row, "cache_reported_input_tokens"));
  const cached = Math.min(
    reported,
    amount(row, "cache_reported_cached_tokens"),
  );
  const classifiedWrite = Math.min(
    reported - cached,
    amount(row, "cache_write_classified_input_tokens"),
  );
  const unclassifiedWrite = Math.min(
    input - reported,
    Math.max(0, amount(row, "cache_write_input_tokens") - classifiedWrite),
  );
  return {
    cached,
    write: classifiedWrite + unclassifiedWrite,
    uncached: reported - cached - classifiedWrite,
    unknown: input - reported - unclassifiedWrite,
    output: amount(row, "output_tokens"),
  };
}
function timeBuckets(
  rows: Row[],
  since: unknown,
  until: unknown,
  window: string,
) {
  const start = new Date(String(since));
  const end = new Date(String(until));
  if (!Number.isFinite(start.valueOf()) || !Number.isFinite(end.valueOf())) {
    return rows;
  }
  const hourly = window === "24h";
  if (hourly) start.setUTCMinutes(0, 0, 0);
  else start.setUTCHours(0, 0, 0, 0);
  const source = new Map(rows.map((row) => [String(row.at), row]));
  const result: Row[] = [];
  for (let at = start; at < end && result.length < 32;) {
    const key = hourly
      ? `${at.toISOString().slice(0, 13)}:00:00Z`
      : at.toISOString().slice(0, 10);
    result.push(source.get(key) || { at: key });
    at = new Date(at.valueOf() + (hourly ? 3_600_000 : 86_400_000));
  }
  return result;
}
const bucketLabel = (at: unknown) => {
  const value = String(at || "");
  return value.includes("T") ? value.slice(11, 16) : value.slice(5);
};
function UsageChart({
  rows,
  metric,
  title,
  since,
  until,
  window,
}: {
  rows: Row[];
  metric: "calls" | "total_tokens";
  title: string;
  since: unknown;
  until: unknown;
  window: string;
}) {
  if (!rows.length) return <Empty>这个时间范围没有可绘制的调用记录。</Empty>;
  const buckets = timeBuckets(rows, since, until, window);
  if (!buckets.length) return <Empty>这个时间范围没有可绘制的调用记录。</Empty>;
  const tokenTotal = (row: Row) => {
    const parts = tokenParts(row);
    return (
      parts.cached + parts.write + parts.uncached + parts.unknown + parts.output
    );
  };
  const maximum = Math.max(
    1,
    ...buckets.map((row) =>
      metric === "calls"
        ? amount(row, "calls")
        : Math.max(amount(row, "total_tokens"), tokenTotal(row)),
    ),
  );
  const points = buckets.map((row, index) => ({
    x: ((index + 0.5) / buckets.length) * 1000,
    y: 174 - (amount(row, "calls") / maximum) * 160,
  }));
  const areaLine = points.reduce(
    (path, point, index) =>
      index === 0
        ? `M ${point.x} ${point.y}`
        : `${path} C ${(points[index - 1].x + point.x) / 2} ${points[index - 1].y}, ${(points[index - 1].x + point.x) / 2} ${point.y}, ${point.x} ${point.y}`,
    "",
  );
  const axisStep = Math.max(1, Math.ceil(buckets.length / 6));
  return (
    <div className="usage-plot">
      <header>
        <h3>{title}</h3>
        <strong>
          {count(rows.reduce((sum, row) => sum + amount(row, metric), 0))}
        </strong>
      </header>
      <div className="usage-chart">
        <div className="usage-chart-scale" aria-hidden="true">
          <span>{count(maximum)}</span>
          <span>{count(Math.round(maximum / 2))}</span>
          <span>0</span>
        </div>
        <div className="usage-chart-body">
          <div className="usage-chart-plot" role="list" aria-label={title}>
            {metric === "calls" && (
              <svg
                className="usage-area"
                viewBox="0 0 1000 180"
                preserveAspectRatio="none"
                aria-hidden="true"
              >
                <path
                  className="usage-area-fill"
                  d={`${areaLine} L ${points.at(-1)?.x} 174 L ${points[0].x} 174 Z`}
                />
                <path className="usage-area-stroke" d={areaLine} />
              </svg>
            )}
            {buckets.map((row, index) => {
              const at = String(row.at || "");
              const value = amount(row, metric);
              const parts = tokenParts(row);
              const stackHeight = (tokenTotal(row) / maximum) * 100;
              const cache = `已确认缓存占已记录输入 ${confirmedCacheShare(row)}；已报告子集命中率 ${cacheRate(row)}；${count(row.cache_unreported_calls)} 次调用未报告缓存量`;
              const detail =
                metric === "calls"
                  ? `${count(value)} 次调用`
                  : `${count(row.total_tokens)} Token；缓存读取 ${count(parts.cached)}；Claude 缓存写入 ${count(parts.write)}；其余未命中输入 ${count(parts.uncached)}；缓存状态未知输入 ${count(parts.unknown)}；输出 ${count(parts.output)}`;
              return (
                <div
                  className={`usage-chart-slot ${index > buckets.length / 2 ? "tooltip-left" : ""}`}
                  role="listitem"
                  key={at}
                  tabIndex={0}
                  aria-label={`${at} UTC：${detail}；${cache}`}
                >
                  {metric === "total_tokens" && (
                    <div
                      className="usage-token-stack"
                      style={{ height: `${stackHeight}%` }}
                      aria-hidden="true"
                    >
                      {(
                        [
                          "cached",
                          "write",
                          "uncached",
                          "unknown",
                          "output",
                        ] as const
                      ).map(
                        (part) =>
                          parts[part] > 0 && (
                            <span
                              key={part}
                              className={`usage-token-${part}`}
                              style={{
                                height: `${(parts[part] / tokenTotal(row)) * 100}%`,
                              }}
                            />
                          ),
                      )}
                    </div>
                  )}
                  <div className="usage-chart-tooltip" aria-hidden="true">
                    <strong>{at} UTC</strong>
                    <b>
                      {metric === "calls"
                        ? `${count(value)} 次调用`
                        : `${count(row.total_tokens)} Token`}
                    </b>
                    {metric === "total_tokens" && (
                      <>
                        <span>
                          <i className="usage-token-cached" />
                          缓存读取 {count(parts.cached)}
                        </span>
                        <span>
                          <i className="usage-token-write" />
                          Claude 缓存写入 {count(parts.write)}
                        </span>
                        <span>
                          <i className="usage-token-uncached" />
                          其余未命中 {count(parts.uncached)}
                        </span>
                        {parts.unknown > 0 && (
                          <span>
                            <i className="usage-token-unknown" />
                            缓存状态未知 {count(parts.unknown)}
                          </span>
                        )}
                        <span>
                          <i className="usage-token-output" />
                          输出 {count(parts.output)}
                        </span>
                      </>
                    )}
                    <small>{cache}</small>
                  </div>
                </div>
              );
            })}
          </div>
          <div className="usage-chart-axis" aria-hidden="true">
            {buckets.map((row, index) => (
              <span key={String(row.at)}>
                {index % axisStep === 0 || index === buckets.length - 1
                  ? bucketLabel(row.at)
                  : ""}
              </span>
            ))}
          </div>
        </div>
      </div>
      {metric === "total_tokens" && (
        <div className="usage-legend" aria-label="Token 图例">
          <span>
            <i className="usage-token-cached" />
            缓存读取
          </span>
          <span>
            <i className="usage-token-write" />
            Claude 缓存写入
          </span>
          <span>
            <i className="usage-token-uncached" />
            其余未命中
          </span>
          <span>
            <i className="usage-token-unknown" />
            缓存状态未知
          </span>
          <span>
            <i className="usage-token-output" />
            输出
          </span>
        </div>
      )}
    </div>
  );
}

function UsageSummary({ refresh }: { refresh: number }) {
  const [window, setWindow] = useState("24h");
  const summary = useQuery<Row>(
    "read_model_usage_summary",
    { window },
    refresh,
  );
  const usage = (summary.data?.fields || summary.data) as Row | null;
  const models = (usage?.models || []) as Row[];
  const profiles = (usage?.profiles || []) as Row[];
  const tasks = (usage?.tasks || []) as Row[];
  const buckets = (usage?.buckets || []) as Row[];
  const modelBuckets = (usage?.model_buckets || []) as Row[];
  return (
    <Section title="Token 用量">
      <div className="usage-range" role="group" aria-label="用量时间范围">
        {(
          [
            ["24h", "最近 24 小时"],
            ["7d", "最近 7 天"],
            ["30d", "最近 30 天"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={window === value ? "btn-primary" : "btn-secondary"}
            aria-pressed={window === value}
            onClick={() => setWindow(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {summary.error != null && <ErrorNote error={summary.error} />}
      {summary.loading && <Empty>正在读取用量…</Empty>}
      {usage && (
        <>
          <div className="usage-cards">
            <div>
              <span>总 Token</span>
              <strong>{count(usage.total_tokens)}</strong>
            </div>
            <div>
              <span>输入</span>
              <strong>{count(usage.input_tokens)}</strong>
            </div>
            <div>
              <span>输出</span>
              <strong>{count(usage.output_tokens)}</strong>
            </div>
            <div>
              <span>逻辑调用</span>
              <strong>{count(usage.calls)}</strong>
            </div>
          </div>
          <p className="small">
            已记录 HTTP 请求尝试 {knownCount(usage.physical_requests)} 次
            {Number(usage.physical_requests_unreported_calls) > 0 &&
              ` · ${count(usage.physical_requests_unreported_calls)} 次历史调用无请求次数记录`}
            {Number(usage.unknown_usage_requests) > 0 &&
              ` · ${count(usage.unknown_usage_requests)} 次请求尝试未确认 Token 用量`}
            。逻辑调用可能包含重试或供应商原生工具的续发请求。
          </p>
          {(Number(usage.native_search_invocations) > 0 ||
            Number(usage.native_search_unreported_calls) > 0) && (
            <p className="small usage-warning">
              原生搜索若被供应商执行，额外费用未知；已标记{" "}
              {count(usage.native_search_invocations)}
              次启用原生搜索的逻辑调用
              {Number(usage.native_search_unreported_calls) > 0 &&
                `，另有 ${count(usage.native_search_unreported_calls)} 次历史调用无法判定是否启用原生搜索`}
              。Token 用量不能代替供应商账单。
            </p>
          )}
          <p className="small">
            <CacheReport usage={usage} />
            。缓存状态未知的输入按已记录输入计入；若输入均已报告，确认占比是命中率下界。缓存
            Token 已包含在输入中，不重复计入总量。
          </p>
          {(Number(usage.cache_write_input_tokens) > 0 ||
            Number(usage.cache_write_unreported_calls) > 0) && (
            <p className="small">
              Claude 缓存写入已报告 {count(usage.cache_write_input_tokens)}{" "}
              Token
              {Number(usage.cache_write_5m_reported_calls) > 0 &&
                ` · 5 分钟写入已报告 ${count(usage.cache_write_5m_input_tokens)} Token`}
              {Number(usage.cache_write_1h_reported_calls) > 0 &&
                ` · 1 小时写入已报告 ${count(usage.cache_write_1h_input_tokens)} Token`}
              {Number(usage.cache_write_ttl_unreported_calls) > 0 &&
                ` · ${count(usage.cache_write_ttl_unreported_calls)} 次写入时长明细缺失或不一致`}
              {Number(usage.cache_write_unreported_calls) > 0 &&
                ` · ${count(usage.cache_write_unreported_calls)} 次 Claude 调用缺少完整缓存读写用量`}
              。写入、读取与其余输入有不同计费档位；此处不推算金额。
            </p>
          )}
          {Number(usage.missing_usage_calls) > 0 && (
            <p className="small usage-warning">
              {count(usage.missing_usage_calls)} 次调用的上游未报告总
              Token，实际 Token 可能更多。
            </p>
          )}
          <p className="small">
            {stamp(usage.since)} 至 {stamp(usage.until)} ·
            统计已入账的全部模型调用，包括未绑定聊天轮次的后台调用；不是供应商账单或剩余额度。
          </p>
          <p className="small">
            按 UTC 时段聚合；尚无可靠单价配置，因此不显示推算费用。
          </p>
          <div className="usage-plot-grid">
            <UsageChart
              rows={buckets}
              metric="calls"
              title="模型调用次数"
              since={usage.since}
              until={usage.until}
              window={window}
            />
            <UsageChart
              rows={buckets}
              metric="total_tokens"
              title="Token 用量"
              since={usage.since}
              until={usage.until}
              window={window}
            />
          </div>
          <div className="usage-model-grid">
            {models.map((model) => {
              const hourly = modelBuckets.filter(
                (row) =>
                  row.provider === model.provider && row.model === model.model,
              );
              return (
                <article
                  className="usage-model-card"
                  key={`${model.provider}:${model.model}`}
                >
                  <h3>
                    {String(model.provider)} · {String(model.model)}
                  </h3>
                  <p>
                    {count(model.calls)} 次逻辑调用 · 已记录 HTTP 请求尝试{" "}
                    {knownCount(model.physical_requests)} 次 ·{" "}
                    {count(model.total_tokens)} Token
                  </p>
                  {Number(model.physical_requests_unreported_calls) > 0 && (
                    <p className="small">
                      {count(model.physical_requests_unreported_calls)}{" "}
                      次历史调用无请求次数记录
                    </p>
                  )}
                  {Number(model.native_search_invocations) > 0 && (
                    <p className="small usage-warning">
                      原生搜索若被执行，额外费用未知
                    </p>
                  )}
                  <p className="small">
                    <CacheReport usage={model} />
                  </p>
                  {String(model.provider).toLowerCase() === "anthropic" && (
                    <p className="small">
                      缓存写入 {count(model.cache_write_input_tokens)} Token
                      {Number(model.cache_write_5m_reported_calls) > 0 &&
                        ` · 5 分钟 ${count(model.cache_write_5m_input_tokens)}`}
                      {Number(model.cache_write_1h_reported_calls) > 0 &&
                        ` · 1 小时 ${count(model.cache_write_1h_input_tokens)}`}
                      {Number(model.cache_write_ttl_unreported_calls) > 0 &&
                        ` · ${count(model.cache_write_ttl_unreported_calls)} 次时长明细不全`}
                      {Number(model.cache_write_unreported_calls) > 0 &&
                        ` · ${count(model.cache_write_unreported_calls)} 次读写明细不全`}
                    </p>
                  )}
                  {hourly.length > 0 && (
                    <div className="usage-plot-grid">
                      <UsageChart
                        rows={hourly}
                        metric="calls"
                        title="模型调用次数"
                        since={usage.since}
                        until={usage.until}
                        window={window}
                      />
                      <UsageChart
                        rows={hourly}
                        metric="total_tokens"
                        title="Token 用量"
                        since={usage.since}
                        until={usage.until}
                        window={window}
                      />
                    </div>
                  )}
                </article>
              );
            })}
          </div>
          <details>
            <summary>按模型、用途和连接查看明细</summary>
            <h3>模型</h3>
            <Table
              rows={models}
              columns={[
                ["provider", "Provider"],
                ["model", "模型"],
                ["calls", "逻辑调用", count],
                ["physical_requests", "已知 HTTP 请求尝试", knownCount],
                ["unknown_usage_requests", "未确认用量请求", count],
                ["total_tokens", "Token", count],
                ["input_tokens", "输入", count],
                ["cached_input_tokens", "其中缓存", count],
                ["cache_write_input_tokens", "Claude 缓存写入", count],
                ["cache_write_5m_input_tokens", "其中 5 分钟写入已报告", count],
                ["cache_write_1h_input_tokens", "其中 1 小时写入已报告", count],
                ["cache_write_ttl_unreported_calls", "写入时长明细不全", count],
                ["cache_write_unreported_calls", "Claude 读写未报", count],
                [
                  "cache_reported_uncached_tokens",
                  "确认缓存占已记录输入",
                  (_, row) => confirmedCacheShare(row),
                ],
                ["cache_reported_input_tokens", "缓存率分母", count],
                ["cache_unreported_calls", "未报缓存", count],
                [
                  "cache_reported_cached_tokens",
                  "已报子集命中率",
                  (_, row) => cacheRate(row),
                ],
                ["output_tokens", "输出", count],
                ["missing_usage_calls", "未报总量", count],
              ]}
            />
            <h3>任务用途</h3>
            <Table
              rows={tasks}
              columns={[
                ["task", "用途"],
                ["calls", "逻辑调用", count],
                ["physical_requests", "已知 HTTP 请求尝试", knownCount],
                ["total_tokens", "Token", count],
                ["cache_write_input_tokens", "Claude 缓存写入", count],
                [
                  "cache_reported_uncached_tokens",
                  "确认缓存占已记录输入",
                  (_, row) => confirmedCacheShare(row),
                ],
                [
                  "cache_reported_cached_tokens",
                  "已报子集命中率",
                  (_, row) => cacheRate(row),
                ],
                ["cache_unreported_calls", "未报缓存", count],
              ]}
            />
            <h3>模型连接</h3>
            <Table
              rows={profiles}
              columns={[
                ["profile_id", "内部连接编号"],
                ["provider", "供应商"],
                ["model", "模型"],
                ["calls", "逻辑调用", count],
                ["physical_requests", "已知 HTTP 请求尝试", knownCount],
                ["total_tokens", "Token", count],
                ["cache_write_input_tokens", "Claude 缓存写入", count],
                [
                  "cache_reported_uncached_tokens",
                  "确认缓存占已记录输入",
                  (_, row) => confirmedCacheShare(row),
                ],
                [
                  "cache_reported_cached_tokens",
                  "已报子集命中率",
                  (_, row) => cacheRate(row),
                ],
                ["cache_unreported_calls", "未报缓存", count],
              ]}
            />
          </details>
        </>
      )}
    </Section>
  );
}
export function Models(props: PageProps) {
  const { allowed, refresh } = props;
  const catalog = useQuery<Row>(
    "read_model_catalog",
    {},
    refresh,
    allowed("read_model_catalog"),
  );
  const fields = catalog.data?.fields as Row | undefined;
  return (
    <>
      <UsageSummary refresh={refresh} />
      <ConfigFile fileId="model_profiles" props={props} />
      <Section title="当前生效配置">
        {catalog.error != null && <ErrorNote error={catalog.error} />}
        {fields ? (
          <details>
            <summary>查看已加载的模型与任务路由</summary>
            <Table
              rows={fields.profiles as Row[]}
              columns={[
                ["id", "内部连接编号"],
                ["provider", "供应商"],
                ["protocol", "协议"],
                ["model", "模型"],
                ["max_output_tokens", "输出预算"],
                ["timeout_seconds", "超时（秒）"],
              ]}
            />
            <h3>任务路由</h3>
            <Table
              rows={fields.routes as Row[]}
              columns={[
                ["task", "任务"],
                ["profile_id", "使用连接"],
              ]}
            />
            <p className="small">
              {fields.apply_mode === "hot_reload"
                ? "新连接与任务路由保存后用于后续请求；正在运行的 Agent 保持本轮原连接。"
                : "模型连接配置变更于重启生效。"}
              凭据与请求头不显示。
            </p>
          </details>
        ) : (
          <Empty>
            {allowed("read_model_catalog")
              ? "正在读取…"
              : "未授予配置读取权限。"}
          </Empty>
        )}
      </Section>
      <Section title="调用明细">
        <QueryList
          method="list_model_usage"
          refresh={refresh}
          onRow={flatten}
          columns={[
            ["created_at", "时间", stamp],
            ["task", "任务"],
            ["profile_id", "内部连接编号"],
            ["model", "模型"],
            ["success", "成功", status],
            ["prompt_tokens", "输入"],
            ["cached_prompt_tokens", "缓存命中"],
            ["cache_creation_input_tokens", "Claude 缓存写入", knownCount],
            ["cache_creation_5m_input_tokens", "其中 5 分钟写入", knownCount],
            ["cache_creation_1h_input_tokens", "其中 1 小时写入", knownCount],
            ["completion_tokens", "输出"],
            ["total_tokens", "合计"],
            ["physical_request_count", "HTTP 请求尝试", knownCount],
            ["unknown_usage_request_count", "请求用量未知", knownCount],
            ["latency_seconds", "耗时（秒）"],
            ["error_category", "问题"],
          ]}
          actions={(row) =>
            row.turn_id ? (
              <a
                href={`#audit?turn=${encodeURIComponent(String(row.turn_id))}`}
                className="file-open"
              >
                查看轮次
              </a>
            ) : (
              <span className="small">未绑定轮次</span>
            )
          }
        />
        <p className="small">
          — 表示该项未报告；不能按 0 计算。缓存命中包含在输入中。
        </p>
      </Section>
    </>
  );
}
