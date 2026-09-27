import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { Empty, ErrorNote, Section } from "./components";
import { SchemaFields } from "./schema-fields";
import { initialSchemaValue } from "./schema-values";

const labels = {
  name: "任务名称",
  timezone: "时区",
  schedule: "时间安排",
  type: "安排类型",
  seconds: "间隔秒数",
  local_datetime: "执行时间（含时区）",
  hour: "小时",
  minute: "分钟",
  weekdays: "星期（一=1，日=7）",
  context: "上下文",
  scene: "场景",
  include_relationship: "包含关系",
  include_memories: "包含记忆",
  history_limit: "历史条数",
  limits: "执行额度",
  max_steps: "最多步骤",
  max_llm_calls: "最多模型调用",
  max_tool_calls: "最多工具调用",
  max_messages: "最多消息",
  timeout_seconds: "超时秒数",
  agent_budget_managed: "使用主 Agent 工作预算",
  instruction: "工作目标",
  context_profile: "上下文场景",
  delivery_target: "投递目标",
  max_model_requests: "最多模型请求",
  target: "发送目标",
  text: "消息文本",
};

function Editor({
  catalog,
  initial,
  automationId,
  creatorKind,
  revision,
  props,
  close,
}: {
  catalog: Row;
  initial?: Row;
  automationId?: number;
  creatorKind?: string;
  revision: number;
  props: PageProps;
  close: () => void;
}) {
  const schema = catalog.script as Row;
  const capabilities = catalog.capabilities as Row[];
  const [script, setScript] = useState<Row>(() =>
    structuredClone(initial || (initialSchemaValue(schema, schema) as Row)),
  );
  const [ownerKind, setOwnerKind] = useState(creatorKind || "person"),
    [owner, setOwner] = useState("");
  const [conversation, setConversation] = useState(props.conversation),
    [maxRuns, setMaxRuns] = useState("");
  const steps = (script.steps || []) as Row[];
  const permitted = (c: Row) =>
    ownerKind !== "self" ||
    ((c.permitted_levels || []) as string[]).includes("self");
  function step(index: number, value: Row) {
    setScript({
      ...script,
      steps: steps.map((item, i) => (i === index ? value : item)),
    });
  }
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const spec =
          automationId == null
            ? {
                script,
                owner_id: ownerKind === "self" ? "self" : owner,
                conversation_id: conversation || null,
                ...(maxRuns ? { max_runs: Number(maxRuns) } : {}),
              }
            : script;
        props.act({
          method: "mutate_automation",
          label: automationId == null ? "创建自动化" : "更新自动化脚本",
          revision,
          payload: {
            action: automationId == null ? "create" : "update",
            resource_id: String(automationId ?? "yuki"),
            spec,
          },
          review: spec,
          hint: "按原任务 schema 校验；主体权限、投递目标及预算在原执行层核验。",
        });
      }}
    >
      {automationId == null && (
        <div className="schema-grid">
          <label className="form-group">
            创建者
            <select
              className="form-control"
              value={ownerKind}
              onChange={(e) => setOwnerKind(e.target.value)}
            >
              <option value="person">人物</option>
              <option value="self">Yuki 自身</option>
            </select>
          </label>
          {ownerKind === "person" && (
            <label className="form-group">
              创建者 Person UUID
              <input
                className="form-control"
                required
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
                placeholder="canonical UUID"
              />
            </label>
          )}
          <label className="form-group">
            场景 Conversation UUID
            <input
              className="form-control"
              required={ownerKind === "self"}
              value={conversation}
              onChange={(e) => setConversation(e.target.value)}
              placeholder={
                ownerKind === "self"
                  ? "填写现有群聊 Conversation UUID"
                  : "留空使用该人物私聊场景"
              }
            />
          </label>
          <label className="form-group">
            最多执行次数
            <input
              className="form-control"
              type="number"
              min={1}
              step={1}
              value={maxRuns}
              onChange={(e) => setMaxRuns(e.target.value)}
              placeholder="不额外限制"
            />
          </label>
        </div>
      )}
      <SchemaFields
        schema={schema}
        root={schema}
        values={script}
        change={setScript}
        prefix="automation-script"
        labels={labels}
        omit={["version", "steps"]}
      />
      <p className="small">
        SELF 任务使用现有群聊场景。Agent 的调用额度由原 Work
        管理时，请在执行额度中启用
        agent_budget_managed；否则步骤额度需落在脚本总额度内。
      </p>
      <h3>执行步骤</h3>
      {steps.map((item, index) => {
        const capability = capabilities.find((c) => c.name === item.call);
        const argsSchema = capability?.schema as Row | undefined;
        return (
          <fieldset key={index} className="config-array">
            <legend>步骤 {index + 1}</legend>
            <div className="schema-grid">
              <label className="form-group">
                步骤标识 {index + 1}
                <input
                  className="form-control"
                  required
                  pattern="[a-z][a-z0-9_]{0,31}"
                  maxLength={32}
                  value={String(item.id || "")}
                  onChange={(e) => step(index, { ...item, id: e.target.value })}
                />
              </label>
              <label className="form-group">
                能力 {index + 1}
                <select
                  className="form-control"
                  required
                  value={String(item.call || "")}
                  onChange={(e) => {
                    const selected = capabilities.find(
                      (c) => c.name === e.target.value,
                    )!;
                    step(index, {
                      ...item,
                      call: selected.name,
                      arguments: initialSchemaValue(
                        selected.schema as Row,
                        selected.schema as Row,
                      ),
                    });
                  }}
                >
                  <option value="">选择已登记能力</option>
                  {!capability && item.call != null && (
                    <option value={String(item.call)}>
                      {String(item.call)}（当前未登记）
                    </option>
                  )}
                  {capabilities.map((c) => (
                    <option
                      value={String(c.name)}
                      key={String(c.name)}
                      disabled={!permitted(c)}
                    >
                      {String(c.name)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="form-group">
                结果别名 {index + 1}
                <input
                  className="form-control"
                  pattern="[a-z][a-z0-9_]{0,31}"
                  maxLength={32}
                  value={String(item.save_as || "")}
                  onChange={(e) =>
                    step(index, { ...item, save_as: e.target.value || null })
                  }
                />
              </label>
            </div>
            {capability && (
              <p className="small">
                {String(capability.description)} · 权限{" "}
                {String(capability.permission)} · 效果 {String(capability.risk)}
              </p>
            )}
            {capability && !permitted(capability) && (
              <Empty>
                原 Registry 不允许 SELF 委托此能力。请选择其他能力。
              </Empty>
            )}
            {argsSchema ? (
              <SchemaFields
                schema={argsSchema}
                root={argsSchema}
                values={(item.arguments || {}) as Row}
                change={(arguments_) =>
                  step(index, { ...item, arguments: arguments_ })
                }
                prefix={`automation-step-${index}`}
                labels={labels}
                templates
              />
            ) : (
              <Empty>
                此能力当前未登记，无法修改其参数。重新选择能力或删除步骤。
              </Empty>
            )}
            <button
              type="button"
              className="btn-secondary"
              onClick={() =>
                setScript({
                  ...script,
                  steps: steps.filter((_, i) => i !== index),
                })
              }
            >
              删除步骤 {index + 1}
            </button>
          </fieldset>
        );
      })}
      <div className="settings-actions">
        <button
          type="button"
          className="btn-secondary"
          disabled={steps.length >= 16}
          onClick={() =>
            setScript({
              ...script,
              steps: [
                ...steps,
                { id: `step_${steps.length + 1}`, call: "", arguments: {} },
              ],
            })
          }
        >
          添加步骤
        </button>
        <button
          className="btn-primary"
          disabled={
            !props.allowed("mutate_automation") ||
            !steps.length ||
            steps.some(
              (item) =>
                !capabilities.some((c) => c.name === item.call && permitted(c)),
            )
          }
        >
          检查并提交脚本
        </button>
        <button type="button" className="btn-secondary" onClick={close}>
          收起编辑
        </button>
      </div>
    </form>
  );
}

export function AutomationEditor({
  props,
  initial,
  automationId,
  creatorKind,
  revision = 0,
  close,
}: {
  props: PageProps;
  initial?: Row;
  automationId?: number;
  creatorKind?: string;
  revision?: number;
  close: () => void;
}) {
  const catalog = useQuery<Row>(
    "read_automation_schema",
    {},
    props.refresh,
    props.allowed("read_automation_schema"),
  );
  return (
    <Section
      title={
        automationId == null ? "新建自动化" : `编辑自动化 #${automationId}`
      }
    >
      {catalog.error != null && <ErrorNote error={catalog.error} />}
      {catalog.loading && <Empty>正在读取原脚本与已登记能力…</Empty>}
      {catalog.data && (
        <Editor
          catalog={catalog.data.fields as Row}
          initial={initial}
          automationId={automationId}
          creatorKind={creatorKind}
          revision={revision}
          props={props}
          close={close}
        />
      )}
      {!props.allowed("read_automation_schema") && (
        <Empty>需要自动化目录读取权限。</Empty>
      )}
    </Section>
  );
}
