import { stamp, text } from "./format";
import { useQuery } from "./hooks";
import { useState } from "react";
import type { ReactNode } from "react";
import type { Row } from "./api";
import type { Intent } from "./actions";
import {
  Badge,
  Empty,
  ErrorNote,
  JsonNote,
  QueryList,
  Section,
  Table,
} from "./components";
import { Traces } from "./traces";

export interface PageProps {
  allowed: (method: string) => boolean;
  act: (intent: Intent) => void;
  refresh: number;
  conversation: string;
}
const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });
const status = (value: unknown) => <Badge value={value} />;

export function Health({ refresh }: { refresh: number }) {
  const health = useQuery<Row>("read_health", {}, refresh),
    system = useQuery<Row>("read_system", {}, refresh);
  return (
    <>
      <Section title="现在的 Yuki">
        {system.error != null && <ErrorNote error={system.error} />}
        {system.data ? (
          <div className="metric-grid">
            {[
              ["版本", system.data.version],
              ["会话", (system.data.conversations as Row)?.count],
              ["连接入口", (system.data.presences as Row)?.count],
              ["等待重启的配置", (system.data.pending_restart as Row)?.count],
            ].map(([label, value]) => (
              <div className="vital-card" key={String(label)}>
                <span className="small">{String(label)}</span>
                <strong>{text(value)}</strong>
              </div>
            ))}
          </div>
        ) : (
          system.loading && <Empty>正在读取状态…</Empty>
        )}
      </Section>
      <Section title="运行状态">
        {health.error != null && <ErrorNote error={health.error} />}
        {health.data && (
          <>
            <p>
              数据库 <Badge value={health.data.database} />
            </p>
            <Table
              rows={(health.data.components || []) as Row[]}
              columns={[
                ["name", "组件"],
                ["enabled", "启用", status],
                ["running", "运行", status],
                ["healthy", "健康", status],
                ["error_category", "问题"],
                ["checked_at", "检查时间", stamp],
              ]}
            />
          </>
        )}
      </Section>
    </>
  );
}

export function Models({ allowed, refresh }: PageProps) {
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
      <Section title="实际调用与用量">
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
      </Section>
    </>
  );
}

export function Work({ allowed, act, refresh, conversation }: PageProps) {
  const [id, setId] = useState<number | null>(null);
  const detail = useQuery<Row>(
    "read_automation",
    { automation_id: id },
    refresh,
    id != null && allowed("read_automation"),
  );
  const fields = detail.data?.fields as Row | undefined;
  function automationAction(row: Row, action: string, label: string) {
    act({
      method: "mutate_automation",
      label,
      revision: Number(row.revision),
      payload: { resource_id: String(row.automation_id), action },
    });
  }
  return (
    <>
      <Section title="持久工作">
        <QueryList
          method="list_work"
          args={{ include_content: allowed("read_execution_trace") }}
          refresh={refresh}
          onRow={flatten}
          columns={[
            ["resource_id", "Work"],
            ["conversation_id", "会话"],
            ["goal", "目标"],
            ["state", "状态", status],
            ["model_requests", "模型请求"],
            ["tool_calls", "工具调用"],
            ["active_seconds", "活跃秒数"],
            ["sent_messages", "已发消息"],
            ["reason", "原因"],
          ]}
          actions={(row) => (
            <a
              className="file-open"
              href={`#audit?work=${encodeURIComponent(String(row.resource_id))}`}
            >
              执行轨迹
            </a>
          )}
        />
      </Section>
      <Section title="定时与自动化">
        <div className="settings-actions">
          <button
            className="btn-primary"
            disabled={!allowed("mutate_automation")}
            onClick={() =>
              act({
                method: "mutate_automation",
                label: "创建自动化",
                revision: 0,
                payload: {
                  action: "create",
                  resource_id: "yuki",
                  spec: {
                    owner_id: "",
                    conversation_id: conversation || null,
                    script: {},
                  },
                },
                edit: "spec",
                hint: "指定委托人的内部 Person ID，并填写完整脚本。投递目标必须明确声明。",
              })
            }
          >
            新建
          </button>
        </div>
        <QueryList
          method="list_automations"
          refresh={refresh}
          columns={[
            ["automation_id", "编号"],
            ["name", "名称"],
            ["status", "状态", status],
            ["run_count", "已执行"],
            ["target_kind", "投递类型"],
            ["target_id", "投递目标"],
            ["route_state", "路由状态"],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <>
              <button
                className="btn-secondary"
                disabled={!allowed("read_automation")}
                onClick={() => setId(Number(row.automation_id))}
              >
                详情
              </button>
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_automation")}
                onClick={() =>
                  automationAction(
                    row,
                    row.status === "paused" ? "resume" : "pause",
                    row.status === "paused" ? "恢复任务" : "暂停任务",
                  )
                }
              >
                {row.status === "paused" ? "恢复" : "暂停"}
              </button>
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_automation")}
                onClick={() => automationAction(row, "run_now", "立即执行")}
              >
                执行
              </button>
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_automation")}
                onClick={() => automationAction(row, "cancel", "取消自动化")}
              >
                取消
              </button>
            </>
          )}
        />
      </Section>
      {id != null && (
        <Section title={`自动化 #${id}`}>
          {detail.error != null && <ErrorNote error={detail.error} />}
          {fields && (
            <>
              <div className="vital-pair">
                <div className="vital-card">
                  下次执行
                  <br />
                  {stamp(fields.next_run_at)}
                </div>
                <div className="vital-card">
                  连续失败
                  <br />
                  {text(fields.consecutive_failures)}
                </div>
              </div>
              <JsonNote title="时间安排" value={fields.schedule} />
              <JsonNote title="脚本" value={fields.script} />
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_automation")}
                onClick={() =>
                  act({
                    method: "mutate_automation",
                    label: "更新自动化脚本",
                    revision: Number(fields.revision),
                    payload: {
                      action: "update",
                      resource_id: String(id),
                      spec: fields.script,
                    },
                    edit: "spec",
                    hint: "保留原任务的创建者与投递场景，按刚读取的版本验证脚本。",
                  })
                }
              >
                编辑脚本
              </button>
              <h3>最近执行（最多 20 次）</h3>
              <Table
                rows={fields.runs as Row[]}
                columns={[
                  ["id", "执行 ID"],
                  ["status", "状态", status],
                  ["scheduled_for", "计划时间", stamp],
                  ["model_calls", "模型调用"],
                  ["tool_calls", "工具调用"],
                  ["sent_messages", "消息"],
                  ["error_category", "原因"],
                ]}
              />
              <h3>执行步骤（最多 200 项）</h3>
              <Table
                rows={fields.steps as Row[]}
                columns={[
                  ["run_id", "执行 ID"],
                  ["step_id", "步骤"],
                  ["capability", "能力"],
                  ["status", "状态", status],
                  ["error_category", "原因"],
                ]}
              />
            </>
          )}
        </Section>
      )}
    </>
  );
}

export function Autonomy({ refresh, conversation }: PageProps) {
  const { data, error, loading } = useQuery<Row>(
    "read_participation",
    {},
    refresh,
  );
  const fields = data?.fields as Row | undefined;
  return (
    <>
      <Section title="自主参与 · 当前状态">
        {error != null && <ErrorNote error={error} />}
        {loading && <Empty>正在翻阅…</Empty>}
        {fields && (
          <>
            <div className="vital-pair">
              <div className="vital-card">
                语义观察 <Badge value={fields.observer_configured} />
              </div>
              <div className="vital-card">
                控制器 <Badge value={fields.running} />
                <br />
                <span className="small">
                  模型参数版本 {text(fields.model_profile)}
                </span>
              </div>
            </div>
            <Table
              rows={fields.scopes as Row[]}
              columns={[
                ["conversation_id", "会话"],
                ["generation", "代次"],
                ["last_human_at", "最后真人消息", stamp],
                ["last_self_message_at", "最后自主发言", stamp],
                ["observations", "语义观察"],
                ["candidates", "候选机会"],
                ["feedback", "反馈"],
                ["pending", "待处理", status],
                ["capacity_blocked", "容量受阻", status],
              ]}
            />
            <p className="small">
              这是当前已加载的有限状态快照。旧 Jev
              判定没有完整历史记录，不能据此当作历史统计。
            </p>
          </>
        )}
      </Section>
      <Section title="已接纳的自主轮与反馈">
        <QueryList
          method="list_participation_runs"
          args={conversation ? { conversation_id: conversation } : {}}
          refresh={refresh}
          onRow={flatten}
          columns={[
            ["created_at", "苏醒时间", stamp],
            ["conversation_id", "会话"],
            ["trigger_kind", "触发类型"],
            ["state", "结果", status],
            ["feedback", "反馈"],
            ["resource_id", "Run"],
          ]}
        />
      </Section>
      <Traces refresh={refresh} />
    </>
  );
}

export function Memory({ allowed, act, refresh }: PageProps) {
  return (
    <>
      <Section title="长期记忆">
        <div className="settings-actions">
          {[
            ["rebuild_memory", "重建", "plan"],
            ["dream_memory", "梦境整理", "plan"],
            ["maintain_memory", "维护索引", "run"],
          ].map(([method, label, action]) => (
            <button
              key={method}
              className="btn-secondary"
              disabled={!allowed(method)}
              onClick={() =>
                act({
                  method,
                  label,
                  revision: 0,
                  payload: { action, resource_id: "yuki", spec: {} },
                  edit: "spec",
                })
              }
            >
              {label}
            </button>
          ))}
        </div>
        <QueryList
          method="list_memory_facts"
          refresh={refresh}
          columns={[
            ["fact_id", "编号"],
            ["category", "类别"],
            ["scope_type", "范围"],
            ["content", "内容", (v, row) => text(v || row.excerpt)],
            ["status", "状态", status],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <>
              {["confirm", "quarantine"].map((action) => (
                <button
                  key={action}
                  className="btn-secondary"
                  disabled={!allowed("mutate_memory")}
                  onClick={() =>
                    act({
                      method: "mutate_memory",
                      label: action === "confirm" ? "确认记忆" : "隔离记忆",
                      revision: Number(row.revision),
                      payload: { action, resource_id: String(row.fact_id) },
                    })
                  }
                >
                  {action === "confirm" ? "确认" : "隔离"}
                </button>
              ))}
            </>
          )}
        />
      </Section>
      <Section title="证据">
        <QueryList
          method="list_memory_evidence"
          refresh={refresh}
          columns={[
            ["evidence_id", "证据"],
            ["fact_id", "记忆"],
            ["relation", "关系"],
            ["excerpt", "原文摘录"],
          ]}
        />
      </Section>
      <Section title="记忆工作">
        <QueryList
          method="list_memory_jobs"
          refresh={refresh}
          columns={[
            ["job_id", "任务"],
            ["kind", "类型"],
            ["status", "状态", status],
            ["operation", "进度"],
          ]}
        />
      </Section>
    </>
  );
}

export function Tools({ allowed, act, refresh }: PageProps) {
  const [plugin, setPlugin] = useState("");
  const runtime = useQuery<Row>(
    "read_plugin_runtime",
    { plugin_id: plugin },
    refresh,
    !!plugin,
  );
  function mutation(
    method: string,
    row: Row,
    resource: string,
    action: string,
    label: string,
  ) {
    act({
      method,
      label,
      revision: Number(row.revision),
      payload: { resource_id: String(row[resource]), action },
    });
  }
  return (
    <>
      <Section title="插件">
        <QueryList
          method="list_plugins"
          refresh={refresh}
          columns={[
            ["name", "名称"],
            ["version", "版本"],
            ["status", "状态", status],
            ["enabled", "启用", status],
          ]}
          actions={(row) => (
            <>
              <button
                className="btn-secondary"
                onClick={() => setPlugin(String(row.plugin_id))}
              >
                运行详情
              </button>
              {[
                ["enable", "启用"],
                ["disable", "停用"],
                ["doctor", "诊断"],
              ].map(([action, label]) => (
                <button
                  key={action}
                  className="btn-secondary"
                  disabled={!allowed("mutate_plugin")}
                  onClick={() =>
                    mutation("mutate_plugin", row, "plugin_id", action, label)
                  }
                >
                  {label}
                </button>
              ))}
              <button
                className="btn-secondary"
                disabled={!allowed("mutate_plugin")}
                onClick={() =>
                  act({
                    method: "mutate_plugin",
                    label: "审核插件授权",
                    revision: Number(row.revision),
                    payload: {
                      resource_id: row.plugin_id,
                      action: "approve",
                      spec: {},
                    },
                    edit: "spec",
                    hint: "逐项检查 manifest hash 和所需权限后提交。",
                  })
                }
              >
                授权
              </button>
            </>
          )}
        />
        {runtime.error != null && <ErrorNote error={runtime.error} />}
        {runtime.data && (
          <JsonNote title={`${plugin} · 实际运行状态`} value={runtime.data} />
        )}
      </Section>
      <Section title="MCP">
        <QueryList
          method="list_mcp_servers"
          refresh={refresh}
          columns={[
            ["server_id", "服务"],
            ["enabled", "启用", status],
            ["healthy", "健康", status],
            ["tool_count", "工具"],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <>
              {[
                ["enable", "启用"],
                ["disable", "停用"],
                ["refresh", "刷新工具"],
                ["reconnect", "重连"],
              ].map(([action, label]) => (
                <button
                  key={action}
                  className="btn-secondary"
                  disabled={!allowed("mutate_mcp")}
                  onClick={() =>
                    mutation("mutate_mcp", row, "server_id", action, label)
                  }
                >
                  {label}
                </button>
              ))}
            </>
          )}
        />
      </Section>
    </>
  );
}

export function Files({ refresh }: PageProps) {
  const [id, setId] = useState("");
  const file = useQuery<Row>(
    "read_workspace",
    { artifact_id: id },
    refresh,
    !!id,
  );
  const fields = file.data?.fields as Row | undefined;
  return (
    <>
      <Section title="Yuki 的共享工作区">
        <QueryList
          method="list_workspace"
          refresh={refresh}
          onRow={flatten}
          columns={[
            ["name", "文件"],
            ["size", "字节"],
            ["revision", "版本"],
            ["immutable", "快照", status],
            ["modified_at", "修改时间", stamp],
          ]}
          actions={(row) => (
            <>
              <button
                className="btn-secondary"
                onClick={() => setId(String(row.resource_id))}
              >
                打开
              </button>
              <a
                className="btn-secondary"
                href={`/api/control/files/workspace/${encodeURIComponent(String(row.resource_id))}`}
                download
              >
                下载
              </a>
            </>
          )}
        />
      </Section>
      {id && (
        <Section title={fields ? text(fields.name) : "文件内容"}>
          {file.error != null && <ErrorNote error={file.error} />}
          {fields && (
            <>
              <p className="small">
                版本 {text(fields.revision)} · SHA256 {text(fields.sha256)}
              </p>
              {fields.binary ? (
                <Empty>二进制文件，不能作为文本预览。</Empty>
              ) : (
                <pre className="file-preview">{text(fields.text)}</pre>
              )}
              {fields.truncated === true && (
                <p className="small">预览已截断。</p>
              )}
            </>
          )}
        </Section>
      )}
    </>
  );
}

export function Identity({ allowed, act, refresh }: PageProps) {
  return (
    <>
      <Section title="用户身份">
        <QueryList
          method="list_persons"
          refresh={refresh}
          columns={[
            ["person_id", "Person"],
            ["enabled", "启用", status],
            ["binding_count", "绑定"],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <button
              className="btn-secondary"
              disabled={
                !row.person_id ||
                !allowed(row.enabled ? "disable_person" : "enable_person")
              }
              onClick={() =>
                act({
                  method: row.enabled ? "disable_person" : "enable_person",
                  label: row.enabled ? "停用用户" : "启用用户",
                  revision: Number(row.revision),
                  payload: {},
                  target: { kind: "person", id: row.person_id },
                })
              }
            >
              {row.enabled ? "停用" : "启用"}
            </button>
          )}
        />
      </Section>
      <Section title="群与空间">
        <QueryList
          method="list_spaces"
          refresh={refresh}
          columns={[
            ["name", "名称"],
            ["space_id", "Space"],
            ["enabled", "启用", status],
            ["autonomous_enabled", "自主参与", status],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <button
              className="btn-secondary"
              disabled={
                !row.space_id ||
                !allowed(row.enabled ? "disable_space" : "enable_space")
              }
              onClick={() =>
                act({
                  method: row.enabled ? "disable_space" : "enable_space",
                  label: row.enabled ? "停用空间" : "启用空间",
                  revision: Number(row.revision),
                  payload: {},
                  target: { kind: "space", id: row.space_id },
                })
              }
            >
              {row.enabled ? "停用" : "启用"}
            </button>
          )}
        />
      </Section>
      <Section title="连接入口">
        <QueryList
          method="list_presences"
          refresh={refresh}
          columns={[
            ["presence_id", "Presence"],
            ["platform", "平台"],
            ["connection_state", "连接", status],
            ["enabled", "启用", status],
            ["ingest_eligible", "接入", status],
          ]}
          actions={(row) => (
            <>
              {[
                ["start_presence", "启动"],
                ["stop_presence", "停止"],
              ].map(([method, label]) => (
                <button
                  key={method}
                  className="btn-secondary"
                  disabled={!allowed(method)}
                  onClick={() =>
                    act({
                      method,
                      label,
                      revision: Number(row.revision),
                      payload: {},
                      target: { kind: "presence", id: row.presence_id },
                    })
                  }
                >
                  {label}
                </button>
              ))}
              <button
                className="btn-secondary"
                disabled={!allowed("set_presence_ingest")}
                onClick={() =>
                  act({
                    method: "set_presence_ingest",
                    label: row.ingest_eligible ? "停止接入" : "开启接入",
                    revision: Number(row.revision),
                    payload: { ingest_eligible: !row.ingest_eligible },
                    target: { kind: "presence", id: row.presence_id },
                  })
                }
              >
                切换接入
              </button>
            </>
          )}
        />
      </Section>
    </>
  );
}

export function SettingsPage({ allowed, act, refresh }: PageProps) {
  const [scopeType, setScopeType] = useState("global"),
    [scopeId, setScopeId] = useState("");
  const scope =
    scopeType === "user"
      ? { person_id: scopeId }
      : scopeType === "group"
        ? { space_id: scopeId }
        : {};
  const [savedScope, setSavedScope] = useState<Row>({});
  return (
    <>
      <Section title="有效配置">
        <form
          className="search-line"
          onSubmit={(e) => {
            e.preventDefault();
            setSavedScope(scope);
          }}
        >
          <select
            className="form-control"
            aria-label="配置范围"
            value={scopeType}
            onChange={(e) => setScopeType(e.target.value)}
          >
            <option value="global">全局</option>
            <option value="group">Space</option>
            <option value="user">Person</option>
          </select>
          {scopeType !== "global" && (
            <input
              className="form-control"
              required
              placeholder="内部 ID"
              aria-label="范围内部 ID"
              value={scopeId}
              onChange={(e) => setScopeId(e.target.value)}
            />
          )}
          <button className="btn-secondary">查看</button>
        </form>
        <QueryList
          method="list_effective_configs"
          args={{ scope: savedScope }}
          refresh={refresh}
          columns={[
            ["key", "配置"],
            ["value", "当前生效"],
            ["saved_value", "保存值"],
            ["source", "来源"],
            ["saved_source", "保存值来源"],
            ["apply_mode", "生效方式"],
            ["pending_restart", "待重启", status],
            ["version", "版本"],
          ]}
          actions={(row) => {
            const kind = savedScope.person_id
                ? "user"
                : savedScope.space_id
                  ? "group"
                  : "global",
              id = savedScope.person_id || savedScope.space_id || "";
            return (
              <>
                <button
                  className="btn-secondary"
                  disabled={
                    !allowed("set_config") || row.apply_mode === "immutable"
                  }
                  onClick={() =>
                    act({
                      method: "set_config",
                      label: `修改 ${row.key}`,
                      revision: Number(row.version || 0),
                      payload: {
                        key: row.key,
                        scope_type: kind,
                        scope_id: id,
                        value: row.saved_value ?? row.value ?? "",
                      },
                      edit: "value",
                      valueKind:
                        row.apply_mode === "secret"
                          ? "secret"
                          : typeof (row.saved_value ?? row.value) === "number"
                            ? "number"
                            : typeof (row.saved_value ?? row.value) ===
                                "boolean"
                              ? "boolean"
                              : typeof (row.saved_value ?? row.value) ===
                                  "string"
                                ? "string"
                                : undefined,
                    })
                  }
                >
                  {row.apply_mode === "secret" ? "替换凭据" : "编辑"}
                </button>
                <button
                  className="btn-secondary"
                  disabled={
                    !allowed("unset_config") ||
                    row.apply_mode === "immutable" ||
                    row.version === null
                  }
                  onClick={() =>
                    act({
                      method: "unset_config",
                      label: `删除 ${row.key} 的覆盖`,
                      revision: Number(row.version || 0),
                      payload: { key: row.key, scope_type: kind, scope_id: id },
                    })
                  }
                >
                  取消覆盖
                </button>
              </>
            );
          }}
        />
      </Section>
      <Section title="配置规则">
        <QueryList
          method="list_config_specs"
          refresh={refresh}
          columns={[
            ["key", "键"],
            ["display_name", "名称"],
            ["description", "说明"],
            ["value_type", "类型"],
            ["apply_mode", "生效"],
            ["allowed_scopes", "允许范围"],
            ["minimum", "最小值"],
            ["maximum", "最大值"],
          ]}
        />
      </Section>
    </>
  );
}

export function Assets({ allowed, act, refresh }: PageProps) {
  function action(
    method: string,
    row: Row,
    key: string,
    value: string,
    label: string,
  ) {
    act({
      method,
      label,
      revision: Number(row.revision),
      payload: { action: value, resource_id: row[key] },
    });
  }
  return (
    <>
      <Section title="表情库">
        <QueryList
          method="list_emoji_assets"
          refresh={refresh}
          columns={[
            ["asset_id", "表情"],
            ["status", "状态", status],
            ["enabled", "启用", status],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <>
              {[
                ["pin", "固定"],
                ["unpin", "取消固定"],
                ["reject", "拒绝"],
                ["ban", "禁用"],
              ].map(([value, label]) => (
                <button
                  key={value}
                  className="btn-secondary"
                  disabled={!allowed("mutate_emoji")}
                  onClick={() =>
                    action("mutate_emoji", row, "asset_id", value, label)
                  }
                >
                  {label}
                </button>
              ))}
            </>
          )}
        />
      </Section>
      <Section title="语音">
        <QueryList
          method="list_speech_profiles"
          refresh={refresh}
          columns={[
            ["profile_id", "音色"],
            ["status", "状态", status],
            ["enabled", "启用", status],
            ["revision", "版本"],
          ]}
          actions={(row) => (
            <button
              className="btn-secondary"
              disabled={!allowed("mutate_speech")}
              onClick={() =>
                action(
                  "mutate_speech",
                  row,
                  "profile_id",
                  row.enabled ? "disable" : "enable",
                  row.enabled ? "禁用音色" : "启用音色",
                )
              }
            >
              {row.enabled ? "禁用" : "启用"}
            </button>
          )}
        />
      </Section>
    </>
  );
}

export function Audit({ allowed, act, refresh, conversation }: PageProps) {
  const [operation, setOperation] = useState<string | null>(null);
  const [operationKind, setOperationKind] = useState("control");
  const receipt = useQuery<Row>(
    "read_operation",
    { operation_id: operation },
    refresh,
    !!operation,
  );
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const scope = params.get("turn")
    ? { turn_id: params.get("turn") }
    : params.get("work")
      ? { work_id: params.get("work") }
      : {};
  return (
    <>
      <Traces scope={scope} refresh={refresh} />
      <Section title="管理操作与持久回执">
        <label className="form-group">
          记录类型
          <select
            className="form-control"
            value={operationKind}
            onChange={(event) => {
              setOperationKind(event.target.value);
              setOperation(null);
            }}
          >
            <option value="control">管理操作</option>
            <option value="rebuild">记忆重建</option>
            <option value="dream">Dream</option>
          </select>
        </label>
        <QueryList
          method="list_operations"
          args={{ kind: operationKind }}
          refresh={refresh}
          columns={[
            ["operation_id", "操作"],
            ["status", "状态", status],
            ["progress", "进度"],
            ["error_category", "原因"],
            ["updated_at", "更新时间", stamp],
          ]}
          actions={(row) => (
            <button
              className="btn-secondary"
              disabled={!allowed("read_operation")}
              onClick={() => setOperation(String(row.operation_id))}
            >
              查看回执
            </button>
          )}
        />
        {receipt.error != null && <ErrorNote error={receipt.error} />}
        {receipt.data && (
          <JsonNote title="原操作当前回执" value={receipt.data} />
        )}
        <button
          className="btn-secondary"
          disabled={!allowed("cancel_operation")}
          onClick={() =>
            act({
              method: "cancel_operation",
              label: "取消长期操作",
              revision: 0,
              payload: { action: "cancel", resource_id: "" },
              edit: "payload",
              hint: "填写支持取消的 rebuild 或 dream 操作 ID，以及刚读取的资源版本。结果未知的操作应先核对持久回执。",
            })
          }
        >
          取消长期操作
        </button>
      </Section>
      <Section title="管理审计">
        <QueryList
          method="list_audit_events"
          refresh={refresh}
          columns={[
            ["audit_id", "编号"],
            ["created_at", "时间", stamp],
            ["operation", "动作"],
            ["target_type", "目标类型"],
            ["success", "结果", status],
            ["error_category", "原因"],
          ]}
        />
      </Section>
      {conversation && (
        <Section title="当前会话的 Social 投递">
          <QueryList
            method="list_social_receipts"
            args={{ conversation_id: conversation }}
            refresh={refresh}
            columns={[
              ["event_id", "内部事件"],
              ["action", "动作"],
              ["status", "状态", status],
              ["target_kind", "目标类型"],
              ["target_id", "目标"],
              ["error_category", "原因"],
              ["updated_at", "更新时间", stamp],
            ]}
          />
        </Section>
      )}
    </>
  );
}

export function Persona({ refresh }: { refresh: number }) {
  const { data, error, loading } = useQuery<Row>("read_persona", {}, refresh);
  return (
    <>
      {loading && <Empty>正在读取…</Empty>}
      {error != null && <ErrorNote error={error} />}
      {data && (
        <div className="paper-note">
          <h2>当前加载的人格提示词</h2>
          <pre className="persona-text">
            {text((data.fields as Row).system_prompt)}
          </pre>
          <p className="small">单次轮次实际注入的上下文请在执行轨迹中查看。</p>
        </div>
      )}
    </>
  );
}
export function Notebook({ props }: { props: PageProps }) {
  const [tab, setTab] = useState("status");
  const tabs = [
    ["status", "状态"],
    ["persona", "人格"],
    ["memory", "记忆"],
    ["work", "工作"],
    ["files", "文件"],
  ];
  const contents: Record<string, () => ReactNode> = {
    status: () => <Health refresh={props.refresh} />,
    persona: () => <Persona refresh={props.refresh} />,
    memory: () => <Memory {...props} />,
    work: () => <Work {...props} />,
    files: () => <Files {...props} />,
  };
  return (
    <>
      <div className="tab-bar" role="tablist" aria-label="手帐页">
        {tabs.map(([key, label]) => (
          <button
            className={`tab-button ${tab === key ? "active" : ""}`}
            key={key}
            id={`notebook-tab-${key}`}
            role="tab"
            aria-selected={tab === key}
            aria-controls="notebook-content"
            tabIndex={tab === key ? 0 : -1}
            onKeyDown={(event) => {
              const index = tabs.findIndex(([value]) => value === tab);
              const next =
                event.key === "ArrowRight"
                  ? (index + 1) % tabs.length
                  : event.key === "ArrowLeft"
                    ? (index + tabs.length - 1) % tabs.length
                    : event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? tabs.length - 1
                        : null;
              if (next !== null) {
                event.preventDefault();
                setTab(tabs[next][0]);
                document
                  .getElementById(`notebook-tab-${tabs[next][0]}`)
                  ?.focus();
              }
            }}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <div
        id="notebook-content"
        role="tabpanel"
        aria-labelledby={`notebook-tab-${tab}`}
        className="tab-content active"
      >
        <div className="panel-content">{contents[tab]()}</div>
      </div>
    </>
  );
}
