import type { Row } from "./api";
import { SchemaFields } from "./schema-fields";
import { Table } from "./components";

const groups: [string, Record<string, string>][] = [
  [
    "无来源机会与群活跃度",
    {
      intrinsic_interval_seconds: "无来源基率分母（秒）",
      human_activity_decay_seconds: "真人活动衰减（秒）",
      human_activity_half_saturation: "真人活动半饱和值",
      silence_rise_seconds: "冷场上升时标（秒）",
      silence_decay_seconds: "沉寂衰减时标（秒）",
    },
  ],
  [
    "自主倾向与负反馈权重",
    {
      pressure_bias: "基础倾向",
      tendency_weight: "SELF 意愿权重",
      activity_weight: "近期活动成本",
      compute_weight: "计算成本",
      no_reply_weight: "沉默选择成本",
      work_fast_weight: "近期 Work 密度成本",
      work_slow_weight: "长期 Work 密度成本",
      exposure_weight: "未被接住的暴露成本",
      reception_weight: "真实回应收益",
    },
  ],
  [
    "有来源机会",
    {
      source_interval_seconds: "有来源基率分母（秒）",
      source_participation_minimum: "参与倾向下限",
      source_familiarity_weight: "熟悉度权重",
      source_invitation_weight: "邀请权重",
      source_extension_weight: "接续权重",
      source_self_weight: "SELF 权重",
      source_closed_weight: "楼层关闭成本",
      source_social_weight: "社交反馈权重",
      source_opening_seconds: "开场时标（秒）",
      source_opening_floor: "开场底值",
      independent_source_factor: "独立来源系数",
    },
  ],
  [
    "活动、计算与沉默衰减",
    {
      activity_decay_seconds: "近期活动衰减（秒）",
      activity_increment: "活动增量",
      social_context_decay_seconds: "社交情境衰减（秒）",
      social_context_increment: "情境增量",
      compute_decay_seconds: "计算衰减（秒）",
      compute_increment: "计算增量",
      no_reply_decay_seconds: "沉默选择衰减（秒）",
      no_reply_increment: "沉默选择增量",
    },
  ],
  [
    "Work 密度与接收反馈",
    {
      work_fast_decay_seconds: "近期 Work 衰减（秒）",
      work_slow_decay_seconds: "长期 Work 衰减（秒）",
      exposure_rise_seconds: "暴露上升时标（秒）",
      exposure_decay_seconds: "暴露衰减（秒）",
      reception_decay_seconds: "真实回应衰减（秒）",
      unanchored_reception_weight: "无锚点回应权重",
      unanchored_reception_decay_seconds: "无锚点回应衰减（秒）",
    },
  ],
  [
    "意愿与来源支持",
    {
      willingness_decay_seconds: "SELF 意愿衰减（秒）",
      willingness_magnitude: "SELF 意愿幅度",
      conversation_source_decay_seconds: "聊天来源支持衰减（秒）",
      recall_source_decay_seconds: "回忆来源支持衰减（秒）",
      contact_source_decay_seconds: "联系来源支持衰减（秒）",
    },
  ],
];
const labels = Object.assign({}, ...groups.map(([, entries]) => entries));

export function AutonomyParameterFields({
  fields,
  document,
  change,
}: {
  fields: Row;
  document: Row;
  change: (document: Row) => void;
}) {
  const schema = fields.parameter_schema as Row;
  const properties = schema.properties as Row;
  const used = new Set(Object.keys(labels));
  const remaining = Object.keys(properties).filter((key) => !used.has(key));
  const sections = remaining.length
    ? [
        ...groups,
        [
          "其他参数",
          Object.fromEntries(remaining.map((key) => [key, key])),
        ] as [string, Record<string, string>],
      ]
    : groups;
  return (
    <>
      <p className="small">
        所有数值边界来自正在使用的独立库
        schema。基率分母是机会率的倒数，不是固定等待时间；这份配置全局应用于当前控制器。
      </p>
      {sections.map(([title, entries], index) => (
        <details className="json-note" key={title} open={index === 0}>
          <summary>{title}</summary>
          <SchemaFields
            values={document}
            schema={{
              ...schema,
              properties: Object.fromEntries(
                Object.keys(entries)
                  .filter((key) => properties[key])
                  .map((key) => [key, properties[key]]),
              ),
            }}
            root={schema}
            labels={entries}
            prefix={`autonomy-${title}`}
            change={change}
          />
        </details>
      ))}
      {fields.loaded_document != null && (
        <details className="json-note">
          <summary>目前生效的参数</summary>
          <Table
            rows={Object.entries(fields.loaded_document as Row).map(
              ([key, value]) => ({ key, label: labels[key] || key, value }),
            )}
            columns={[
              ["label", "参数"],
              ["value", "生效值"],
              ["key", "配置键"],
            ]}
          />
        </details>
      )}
    </>
  );
}
