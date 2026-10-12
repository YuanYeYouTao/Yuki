import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { Empty, ErrorNote, Section } from "./components";
import { SchemaFields } from "./schema-fields";
import { initialSchemaValue } from "./schema-values";
import { OwnerPicker } from "./owner-picker";

const labels = {
  name: "任务名称",
  goal: "完整任务要求",
  constraints: "任务约束",
  timezone: "时区",
  trigger: "时间安排",
  type: "安排类型",
  seconds: "间隔秒数",
  local_datetime: "执行时间（含时区）",
  hour: "小时",
  minute: "分钟",
  weekdays: "星期（一=1，日=7）",
  delivery: "任务目标",
  target: "投递要求",
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
  const schema = catalog.task as Row;
  const [task, setTask] = useState<Row>(() =>
    structuredClone(initial || (initialSchemaValue(schema, schema) as Row)),
  );
  const [ownerKind, setOwnerKind] = useState(creatorKind || "person");
  const [owner, setOwner] = useState("");
  const [conversation, setConversation] = useState(props.conversation);
  const [maxRuns, setMaxRuns] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const spec =
          automationId == null
            ? {
                task,
                owner_id: ownerKind === "self" ? "self" : owner,
                conversation_id: conversation || null,
                ...(maxRuns ? { max_runs: Number(maxRuns) } : {}),
              }
            : { task };
        props.act({
          method: "mutate_automation",
          label: automationId == null ? "创建安排" : "更新任务要求",
          revision,
          payload: {
            action: automationId == null ? "create" : "update",
            resource_id: String(automationId ?? "yuki"),
            spec,
          },
          review: spec,
          hint: "到期由主 Agent 在真实目标会话中执行完整任务要求。",
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
              创建者
              <OwnerPicker
                kind="person"
                label="创建者"
                required
                value={owner}
                change={setOwner}
                refresh={props.refresh}
                empty="选择人物"
              />
            </label>
          )}
          <label className="form-group">
            场景会话
            <OwnerPicker
              kind="conversation"
              label="场景会话"
              required={ownerKind === "self"}
              value={conversation}
              change={setConversation}
              refresh={props.refresh}
              empty={
                ownerKind === "self"
                  ? "选择原群聊场景"
                  : "留空使用该人物私聊场景"
              }
            />
          </label>
          <label className="form-group">
            最多重复次数
            <input
              className="form-control"
              type="number"
              min={1}
              step={1}
              value={maxRuns}
              onChange={(e) => setMaxRuns(e.target.value)}
              placeholder="不限重复次数"
            />
          </label>
        </div>
      )}
      <SchemaFields
        schema={schema}
        root={schema}
        values={task}
        change={setTask}
        prefix="automation-task"
        labels={labels}
        omit={["version"]}
      />
      <div className="settings-actions">
        <button
          className="btn-primary"
          disabled={!props.allowed("mutate_automation")}
        >
          检查并提交安排
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
      title={automationId == null ? "新建安排" : `编辑安排 #${automationId}`}
    >
      {catalog.error != null && <ErrorNote error={catalog.error} />}
      {catalog.loading && <Empty>正在读取任务要求与安排格式…</Empty>}
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
