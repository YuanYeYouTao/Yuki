import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import {
  Badge,
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
  Table,
} from "./components";
import { SchemaFields } from "./schema-fields";
import { stamp } from "./format";

const labels: Record<string, string> = {
  poll_interval_seconds: "轮询间隔（秒）",
  initial_sync_mode: "首次同步方式",
  replay_recent_limit: "首次回放数量",
  events_per_repository: "单仓库读取数量",
  max_events_per_poll: "每轮处理数量",
  coalesce: "整合事件",
  request_timeout_seconds: "请求超时（秒）",
  repositories: "订阅仓库",
  repository: "仓库（owner/name）",
  enabled: "启用",
  event_types: "事件类型",
  branches: "分支",
  ignored_actors: "忽略的操作者",
  ignore_bots: "忽略机器人",
  ignore_draft_pull_requests: "忽略草稿 PR",
  default_branch_only: "仅默认分支",
  targets: "通知目标",
  target_type: "目标类型",
  target_id: "平台投递目标 ID",
  ask_agent: "邀请 Yuki 处理",
  send_text: "发送文本",
  send_card: "发送卡片",
};
const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const badge = (value: unknown) => <Badge value={value} />;

function ConfigDraft({ fields, props }: { fields: Row; props: PageProps }) {
  const [values, change] = useState(fields.values as Row);
  const schema = fields.schema as Row;
  return (
    <>
      <p className="small">
        版本 {String(fields.revision)} · 配置校验 <Badge value={fields.valid} />
        。保存到 Host 原配置表，生效时间由插件自身的读取逻辑决定。
      </p>
      {fields.valid === false && (
        <p role="alert" className="error-note">
          已保存的配置与当前 schema 不一致。修正并保存时会删除不再声明的旧字段。
        </p>
      )}
      <SchemaFields
        schema={schema}
        root={schema}
        values={values}
        change={change}
        prefix={`plugin-${fields.plugin_id}`}
        labels={labels}
      />
      {props.allowed("configure_plugin") && (
        <button
          className="btn-primary"
          onClick={() =>
            props.act({
              method: "configure_plugin",
              label: "保存插件配置",
              revision: Number(fields.revision),
              payload: {
                resource_id: fields.plugin_id,
                action: "save",
                spec: {
                  scope_type: fields.scope_type,
                  owner_id: fields.owner_id,
                  values,
                },
              },
              review: {
                scope_type: fields.scope_type,
                owner_id: fields.owner_id,
                values,
              },
              hint: "将执行插件完整 schema 校验并核对当前范围的版本。密钥由服务器的 Secrets 管理，不在此表单保存。",
            })
          }
        >
          检查并保存
        </button>
      )}
    </>
  );
}

function Observation({
  pluginId,
  props,
}: {
  pluginId: string;
  props: PageProps;
}) {
  const [cursor, setCursor] = useState<string | null>(null);
  const [stack, setStack] = useState<(string | null)[]>([]);
  const [selected, select] = useState("");
  const [seenRefresh, markRefresh] = useState(props.refresh);
  if (seenRefresh !== props.refresh) {
    markRefresh(props.refresh);
    setCursor(null);
    setStack([]);
    select("");
  }
  const query = useQuery<Row>(
    "read_plugin_observation",
    { plugin_id: pluginId, cursor, limit: 10 },
    props.refresh,
    props.allowed("read_plugin_observation"),
  );
  const fields = query.data?.fields as Row | undefined;
  const repositoryView =
    Array.isArray(fields?.repositories) &&
    fields.repositories.every(
      (row) =>
        row !== null &&
        typeof row === "object" &&
        typeof row.repository === "string",
    );
  const repositories = (repositoryView ? fields?.repositories : []) as Row[];
  const detail = repositories.find((row) => row.repository === selected);
  return (
    <Section title={`${pluginId} · 监控队列`}>
      <p className="small">
        只读插件的持久队列，不触发 GitHub
        请求、轮询或发送。没有队列记录时显示未知，不当作零。
      </p>
      {!props.allowed("read_plugin_observation") && (
        <Empty>需要插件配置与状态正文读取权限。</Empty>
      )}
      {query.error != null && <ErrorNote error={query.error} />}
      {query.loading && <Empty>正在读取插件状态…</Empty>}
      {fields && (
        <>
          {(fields.config as Row)?.poll_interval_seconds != null && (
            <p className="small">
              轮询间隔 {String((fields.config as Row)?.poll_interval_seconds)}{" "}
              秒 · 事件整合 <Badge value={(fields.config as Row)?.coalesce} />
            </p>
          )}
          {repositoryView ? (
            <>
              <Table
                rows={repositories}
                columns={[
                  ["repository", "仓库"],
                  ["queue_available", "队列", badge],
                  ["pending_count", "待处理"],
                  ["last_poll_at", "最近轮询", stamp],
                  ["last_success_at", "最近成功", stamp],
                  ["paused_until", "暂停至", stamp],
                  ["consecutive_failures", "连续失败"],
                  ["error_category", "诊断"],
                  ["state_error", "状态错误"],
                ]}
                actions={(row) => (
                  <button
                    className="btn-secondary"
                    onClick={() => select(String(row.repository))}
                  >
                    游标与投递详情
                  </button>
                )}
              />
              {detail && (
                <JsonNote title={`${selected} · 原队列投影`} value={detail} />
              )}
              <div className="pagination">
                <button
                  className="btn-secondary"
                  disabled={!stack.length || query.loading}
                  onClick={() => {
                    setCursor(stack.at(-1) ?? null);
                    setStack(stack.slice(0, -1));
                    select("");
                  }}
                >
                  上一页
                </button>
                <span className="small">第 {stack.length + 1} 页</span>
                <button
                  className="btn-secondary"
                  disabled={!fields.next_cursor || query.loading}
                  onClick={() => {
                    setStack([...stack, cursor]);
                    setCursor(String(fields.next_cursor));
                    select("");
                  }}
                >
                  下一页
                </button>
              </div>
            </>
          ) : (
            <JsonNote title="插件只读状态" value={fields} />
          )}
        </>
      )}
    </Section>
  );
}

export function PluginDetails({
  pluginId,
  props,
}: {
  pluginId: string;
  props: PageProps;
}) {
  const [scope, setScope] = useState("global"),
    [owner, setOwner] = useState("");
  const [selection, select] = useState({
    scope_type: "global",
    owner_id: null as string | null,
  });
  const query = useQuery<Row>(
    "read_plugin_configuration",
    { plugin_id: pluginId, ...selection },
    props.refresh,
    props.allowed("read_plugin_configuration"),
  );
  const fields = query.data?.fields as Row | undefined;
  const scopeReady =
    scope === selection.scope_type &&
    (scope === "global" || owner === selection.owner_id);
  return (
    <>
      <Section title={`${pluginId} · 配置`}>
        <div className="settings-actions">
          <label className="form-group">
            配置范围
            <select
              className="form-control"
              value={scope}
              onChange={(e) => setScope(e.target.value)}
            >
              <option value="global">全局</option>
              <option value="user">Person</option>
              <option value="group">Space</option>
            </select>
          </label>
          {scope !== "global" && (
            <label className="form-group">
              内部 {scope === "user" ? "Person" : "Space"} ID
              <input
                className="form-control"
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
                placeholder="canonical UUID"
              />
            </label>
          )}
          <button
            className="btn-secondary"
            disabled={
              !props.allowed("read_plugin_configuration") ||
              (scope !== "global" && !owner)
            }
            onClick={() =>
              select({
                scope_type: scope,
                owner_id: scope === "global" ? null : owner,
              })
            }
          >
            读取此范围
          </button>
        </div>
        <p className="small">
          范围所属身份使用内部 Person / Space
          ID；通知目标字段仍使用插件的外部投递合同。不同范围是否被插件消费，由其功能决定。
        </p>
        {!props.allowed("read_plugin_configuration") && (
          <Empty>需要插件配置正文读取权限。</Empty>
        )}
        {query.error != null && <ErrorNote error={query.error} />}
        {query.loading && <Empty>正在读取原配置…</Empty>}
        {!scopeReady && <Empty>范围已更改，请先读取此范围的配置。</Empty>}
        {fields && scopeReady && (
          <ConfigDraft
            key={`${pluginId}:${fields.scope_type}:${fields.owner_id}:${fields.revision}`}
            fields={fields}
            props={props}
          />
        )}
      </Section>
      <Observation pluginId={pluginId} props={props} />
      {props.allowed("list_plugin_outbox") && (
        <Section title={`${pluginId} · 通知投递`}>
          <QueryList
            method="list_plugin_outbox"
            args={{ plugin_id: pluginId }}
            refresh={props.refresh}
            onRow={flatten}
            columns={[
              ["outbox_id", "投递 ID"],
              ["source_event_id", "内部事件 ID"],
              ["part_type", "类型"],
              ["status", "状态", badge],
              ["attempts", "已尝试"],
              ["max_attempts", "预算"],
              ["last_error_category", "失败原因"],
              ["next_attempt_at", "下次尝试", stamp],
              ["has_platform_receipt", "平台回执", badge],
            ]}
            actions={(row) =>
              row.can_retry && props.allowed("mutate_plugin") ? (
                <button
                  className="btn-secondary"
                  onClick={() =>
                    props.act({
                      method: "mutate_plugin",
                      label: "重试未发送的通知",
                      revision: Number(row.revision),
                      payload: {
                        resource_id: String(row.outbox_id),
                        action: "retry",
                      },
                      review: {
                        outbox_id: row.outbox_id,
                        source_event_id: row.source_event_id,
                        status: row.status,
                        error: row.last_error_category,
                      },
                      hint: "仅把已确认未发送的失败交回原投递队列。结果未知、有平台回执或预算耗尽的通知不允许重试。",
                    })
                  }
                >
                  重试
                </button>
              ) : (
                <span className="small">—</span>
              )
            }
          />
        </Section>
      )}
    </>
  );
}
