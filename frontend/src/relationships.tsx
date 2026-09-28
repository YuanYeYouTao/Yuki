import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { Empty, ErrorNote, QueryList, Section, Table } from "./components";
import { stamp, text } from "./format";
import { Traces } from "./traces";
import { OwnerPicker } from "./owner-picker";

const flatten = (row: Row): Row => ({ ...row, ...((row.fields as Row) || {}) });

function RelationshipDetails({
  person,
  props,
}: {
  person: string;
  props: PageProps;
}) {
  const result = useQuery<Row>(
    "read_relationship",
    { person_id: person },
    props.refresh,
    props.allowed("read_relationship"),
  );
  const fields = result.data?.fields as Row | undefined;
  const [section, selectSection] = useState("events");
  const [source, selectSource] = useState<number | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  return (
    <>
      <Section title="人物关系详情">
        <p className="small">Person {person}</p>
        {result.error != null && <ErrorNote error={result.error} />}
        {result.loading && <Empty>正在读取…</Empty>}
        {fields && (
          <>
            <Table
              rows={[fields]}
              columns={[
                ["affection_score", "好感"],
                ["trust_score", "原始信任"],
                ["stage", "阶段"],
                ["updated_at", "更新", stamp],
                ["last_automatic_change_at", "最近自动变化", stamp],
                ["revision", "版本"],
              ]}
            />
            <p className="small">
              分数来自原关系记录。阶段使用原领域映射；页面不调用关系评估、不初始化关系。有效信任仍由原运行时策略决定。
            </p>
            <div className="schema-grid">
              {[
                ["set_affection", "设置好感", 0, 100],
                ["set_trust", "设置信任", 0, 100],
                ["adjust_affection", "调整好感", -20, 20],
              ].map(([action, label, min, max]) => (
                <form
                  className="form-group"
                  key={String(action)}
                  onSubmit={(e) => {
                    e.preventDefault();
                    props.act({
                      method: "mutate_relationship",
                      label: String(label),
                      revision: Number(fields.revision),
                      payload: {
                        action,
                        resource_id: person,
                        spec: { value: Number(values[String(action)]) },
                      },
                    });
                  }}
                >
                  <label>
                    {String(label)}
                    <input
                      className="form-control"
                      type="number"
                      min={Number(min)}
                      max={Number(max)}
                      step="1"
                      required
                      value={values[String(action)] ?? ""}
                      onChange={(e) =>
                        setValues({
                          ...values,
                          [String(action)]: e.target.value,
                        })
                      }
                    />
                  </label>
                  <button
                    className="btn-secondary"
                    disabled={!props.allowed("mutate_relationship")}
                  >
                    检查并提交
                  </button>
                </form>
              ))}
            </div>
          </>
        )}
      </Section>
      <Section title="原关系历史与后台评估">
        <label className="form-group">
          历史类别
          <select
            className="form-control"
            value={section}
            onChange={(e) => {
              selectSection(e.target.value);
              selectSource(null);
            }}
          >
            <option value="events">已提交变化</option>
            <option value="jobs">评估任务</option>
          </select>
        </label>
        {props.allowed("list_relationship_history") && (
          <QueryList
            method="list_relationship_history"
            args={{ person_id: person, section }}
            refresh={props.refresh}
            onRow={flatten}
            columns={
              section === "events"
                ? [
                    ["id", "历史"],
                    ["created_at", "时间", stamp],
                    ["change_type", "来源"],
                    ["affection_before", "原好感"],
                    ["affection_delta", "好感变化"],
                    ["affection_after", "现好感"],
                    ["trust_delta", "信任变化"],
                    ["trust_after", "现信任"],
                    ["reason_code", "原因"],
                    ["source_event_id", "内部来源事件"],
                  ]
                : [
                    ["id", "任务"],
                    ["trigger_event_id", "内部触发事件"],
                    ["status", "状态"],
                    ["attempts", "失败次数"],
                    ["next_attempt_at", "下次尝试", stamp],
                    ["error_category", "错误类别"],
                  ]
            }
            actions={(row) => {
              const source = row.source_event_id ?? row.trigger_event_id;
              return source != null ? (
                <button
                  className="btn-secondary"
                  disabled={!props.allowed("list_execution_trace")}
                  onClick={() => selectSource(Number(source))}
                >
                  来源 #{text(source)}
                </button>
              ) : (
                <span className="small">手动变更，无聊天事件</span>
              );
            }}
          />
        )}
      </Section>
      {source != null && (
        <Traces
          scope={{ source_event_id: source }}
          refresh={props.refresh}
          title={`内部事件 #${source} 的原执行`}
        />
      )}
    </>
  );
}

export function Relationships(props: PageProps) {
  const [person, select] = useState("");
  const [draft, setDraft] = useState("");
  return (
    <>
      <Section title="Yuki 与人物的关系">
        <form
          className="search-line"
          onSubmit={(e) => {
            e.preventDefault();
            select(draft.trim());
          }}
        >
          <label>
            人物
            <OwnerPicker
              kind="person"
              label="人物"
              required
              value={draft}
              change={setDraft}
              refresh={props.refresh}
              empty="选择人物"
            />
          </label>
          <button className="btn-secondary">查看人物</button>
        </form>
        {props.allowed("list_relationships") ? (
          <QueryList
            method="list_relationships"
            refresh={props.refresh}
            onRow={flatten}
            columns={[
              ["person_id", "Person"],
              ["affection_score", "好感"],
              ["trust_score", "原始信任"],
              ["stage", "阶段"],
              ["updated_at", "更新", stamp],
            ]}
            actions={(row) => (
              <button
                className="btn-secondary"
                onClick={() => {
                  select(String(row.person_id));
                  setDraft(String(row.person_id));
                }}
              >
                详情与历史
              </button>
            )}
          />
        ) : (
          <Empty>需要关系读取权限。</Empty>
        )}
      </Section>
      {person && (
        <RelationshipDetails key={person} person={person} props={props} />
      )}
    </>
  );
}
