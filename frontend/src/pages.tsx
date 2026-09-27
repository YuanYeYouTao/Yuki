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
import { ConfigFile } from "./config-files";
export { Models } from "./models";
import { Work } from "./work";
export { Work };
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

export function Autonomy(props: PageProps) {
  const { refresh, conversation } = props;
  const [origin, selectOrigin] = useState("semantic_observation");
  const [run, selectRun] = useState("");
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
          actions={(row) => (
            <button
              className="btn-secondary"
              disabled={!props.allowed("list_participation_feedback")}
              onClick={() => selectRun(String(row.resource_id))}
            >
              全部反馈
            </button>
          )}
        />
      </Section>
      {run && (
        <Section title={`自主轮 ${run} · 原反馈历史`}>
          <QueryList
            method="list_participation_feedback"
            args={{
              run_id: run,
              include_content: props.allowed("read_execution_trace"),
            }}
            refresh={refresh}
            onRow={flatten}
            columns={[
              ["sequence", "顺序"],
              ["outcome", "实际结果", status],
              ["created_at", "提交时间", stamp],
              [
                "actual_targets",
                "实际目标",
                (value) =>
                  value == null ? (
                    "未授权读取"
                  ) : (
                    <JsonNote title="目标引用" value={value} />
                  ),
              ],
              [
                "effects",
                "实际效果引用",
                (value) =>
                  value == null ? (
                    "未授权读取"
                  ) : (
                    <JsonNote title="原效果与调用" value={value} />
                  ),
              ],
              [
                "considered_sources",
                "使用来源",
                (value) =>
                  value == null ? (
                    "未授权读取"
                  ) : (
                    <JsonNote title="原内部来源" value={value} />
                  ),
              ],
            ]}
            actions={(row) => (
              <>
                {Array.isArray(row.effects) &&
                  [
                    ...new Set(
                      row.effects
                        .map(
                          (ref) =>
                            /^work-model:([^:]+):\d+$/.exec(String(ref))?.[1],
                        )
                        .filter(Boolean),
                    ),
                  ].map((work) => (
                    <a
                      className="file-open"
                      key={String(work)}
                      href={`#audit?work=${encodeURIComponent(String(work))}`}
                    >
                      工作 {String(work).slice(0, 8)} 的执行轨迹
                    </a>
                  ))}
              </>
            )}
          />
          <p className="small">
            按原 sequence
            追加反馈。效果引用保留原回执身份；结果未知不会变成已发送。
          </p>
        </Section>
      )}
      <Section title="决策时间线">
        <label className="form-group">
          记录类型
          <select
            className="form-control"
            value={origin}
            onChange={(e) => selectOrigin(e.target.value)}
          >
            <option value="semantic_observation">Jev 实际语义观察</option>
            <option value="participation_decision">Host 提议与接纳</option>
          </select>
        </label>
        <p className="small">
          沿原诊断保留期限查询；上线前未保存、过期或隐私删除的判定不会补写。没有记录不表示没有观察。
        </p>
      </Section>
      <Traces
        refresh={refresh}
        scope={{
          origin,
          ...(conversation ? { conversation_id: conversation } : {}),
        }}
        title="语义与接纳记录"
      />
      <ConfigFile fileId="autonomous_model" props={props} />
    </>
  );
}

import { Memory } from "./memory";
export { Memory };

export { Tools } from "./tools";

import { Files } from "./workspace";
export { Files };

export { Identity } from "./identity";

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

export function Audit({ allowed, refresh, conversation }: PageProps) {
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
      : params.get("event")
        ? { source_event_id: Number(params.get("event")) }
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
        <p className="small">
          重建与梦境的执行管理，请在记忆页读取原计划版本后操作。
        </p>
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

export function Persona(props: PageProps) {
  const { refresh } = props;
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
      <ConfigFile fileId="system_prompt" props={props} />
      <ConfigFile fileId="bot_persona" props={props} />
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
    persona: () => <Persona {...props} />,
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
