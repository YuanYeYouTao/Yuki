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
const cacheRate = (usage: Row) => {
  const input = Number(usage.cache_reported_input_tokens || 0);
  const cached = Number(usage.cache_reported_cached_tokens || 0);
  return input > 0 ? `${((cached / input) * 100).toFixed(1)}%` : "—";
};
function CacheReport({ usage }: { usage: Row }) {
  return (
    <span>
      缓存命中率 {cacheRate(usage)} · 命中 {count(usage.cached_input_tokens)}{" "}
      Token
      {Number(usage.cache_unreported_calls || 0) > 0 &&
        ` · ${count(usage.cache_unreported_calls)} 次调用未报告缓存量`}
    </span>
  );
}
function UsageBars({
  rows,
  metric,
  title,
}: {
  rows: Row[];
  metric: "calls" | "total_tokens";
  title: string;
}) {
  if (!rows.length) return <Empty>这个时间范围没有可绘制的调用记录。</Empty>;
  const maximum = Math.max(1, ...rows.map((row) => Number(row[metric] || 0)));
  return (
    <div className="usage-plot">
      <h3>{title}</h3>
      <div className="usage-bars" role="list" aria-label={title}>
        {rows.map((row) => {
          const at = String(row.at || "");
          const label = at.includes("T") ? at.slice(11, 16) : at.slice(5);
          const value = Number(row[metric] || 0);
          const cache = `缓存命中率 ${cacheRate(row)}；${count(row.cache_unreported_calls)} 次调用未报告缓存量`;
          return (
            <div
              className="usage-bar-item"
              role="listitem"
              key={at}
              aria-label={`${at} UTC：${count(value)}${metric === "calls" ? " 次调用" : " Token"}；${cache}`}
              title={`${at} UTC · ${cache}`}
            >
              <span className="usage-bar-value">{count(value)}</span>
              <div
                className="usage-bar"
                style={{ height: `${Math.max(4, (value / maximum) * 100)}%` }}
              />
              <small>{label}</small>
            </div>
          );
        })}
      </div>
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
              <span>模型调用</span>
              <strong>{count(usage.calls)}</strong>
            </div>
          </div>
          <p className="small">
            <CacheReport usage={usage} />
            。缓存 Token 已包含在输入中，不重复计入总量。
          </p>
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
            <UsageBars rows={buckets} metric="calls" title="API 调用次数" />
            <UsageBars
              rows={buckets}
              metric="total_tokens"
              title="Token 用量"
            />
          </div>
          <div className="usage-model-grid">
            {models.map((model) => (
              <article
                className="usage-model-card"
                key={`${model.provider}:${model.model}`}
              >
                <h3>
                  {String(model.provider)} · {String(model.model)}
                </h3>
                <p>
                  {count(model.calls)} 次调用 · {count(model.total_tokens)}{" "}
                  Token
                </p>
                <p className="small">
                  <CacheReport usage={model} />
                </p>
              </article>
            ))}
          </div>
          <details>
            <summary>按模型、用途和连接查看明细</summary>
            <h3>模型</h3>
            <Table
              rows={models}
              columns={[
                ["provider", "Provider"],
                ["model", "模型"],
                ["calls", "调用"],
                ["total_tokens", "Token", count],
                ["input_tokens", "输入", count],
                ["cached_input_tokens", "其中缓存", count],
                ["cache_reported_input_tokens", "缓存率分母", count],
                ["cache_unreported_calls", "未报缓存", count],
                [
                  "cache_reported_cached_tokens",
                  "缓存率",
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
                ["calls", "调用", count],
                ["total_tokens", "Token", count],
                [
                  "cache_reported_cached_tokens",
                  "缓存命中率",
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
                ["calls", "调用", count],
                ["total_tokens", "Token", count],
                [
                  "cache_reported_cached_tokens",
                  "缓存命中率",
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
              模型连接配置变更于重启生效。凭据与请求头不显示。
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
            ["completion_tokens", "输出"],
            ["total_tokens", "合计"],
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
