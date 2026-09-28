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

function UsageSummary({ refresh }: { refresh: number }) {
  const [window, setWindow] = useState("24h");
  const summary = useQuery<Row>(
    "read_model_usage_summary",
    { window },
    refresh,
  );
  const usage = (summary.data?.fields || summary.data) as Row | null;
  const models = (usage?.models || []) as Row[];
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
            输入中已有 {count(usage.cached_input_tokens)} Token 命中缓存
            {Number(usage.input_tokens) > 0
              ? `（${Math.round((Number(usage.cached_input_tokens) / Number(usage.input_tokens)) * 100)}%）`
              : ""}
            ，不会重复加到总量。
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
          <details>
            <summary>按 Provider 和模型查看</summary>
            <Table
              rows={models}
              columns={[
                ["provider", "Provider"],
                ["model", "模型"],
                ["calls", "调用"],
                ["total_tokens", "Token", count],
                ["input_tokens", "输入", count],
                ["cached_input_tokens", "其中缓存", count],
                ["output_tokens", "输出", count],
                ["missing_usage_calls", "未报总量", count],
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
      <Section title="已加载的模型与路由">
        {catalog.error != null && <ErrorNote error={catalog.error} />}
        {fields ? (
          <>
            <Table
              rows={fields.profiles as Row[]}
              columns={[
                ["id", "Profile"],
                ["provider", "Provider"],
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
                ["profile_id", "使用 Profile"],
              ]}
            />
            <p className="small">
              Profile 文件变更于重启生效。凭据与请求头不显示。
            </p>
          </>
        ) : (
          <Empty>
            {allowed("read_model_catalog")
              ? "正在读取…"
              : "未授予配置读取权限。"}
          </Empty>
        )}
      </Section>
      <ConfigFile fileId="model_profiles" props={props} />
      <UsageSummary refresh={refresh} />
      <Section title="调用明细">
        <QueryList
          method="list_model_usage"
          refresh={refresh}
          onRow={flatten}
          columns={[
            ["created_at", "时间", stamp],
            ["task", "任务"],
            ["profile_id", "Profile"],
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
