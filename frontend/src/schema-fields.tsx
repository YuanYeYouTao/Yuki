import type { Row } from "./api";
import { initialSchemaValue, resolve } from "./schema-values";
import { useState } from "react";

type Options = {
  templates?: boolean;
  labels?: Record<string, string>;
  omit?: string[];
  choices?: (name: string, values: string[]) => string[];
};

function TemplateField({
  id,
  label,
  value,
  change,
  initial,
  children,
}: {
  id: string;
  label: string;
  value: unknown;
  change: (value: unknown) => void;
  initial: () => unknown;
  children: React.ReactNode;
}) {
  const [template, setTemplate] = useState(
    typeof value === "string" && value.startsWith("$"),
  );
  return (
    <fieldset className="config-array">
      <legend>{label}</legend>
      <label htmlFor={`${id}-mode`}>
        参数来源
        <select
          id={`${id}-mode`}
          className="form-control"
          value={template ? "template" : "literal"}
          onChange={(e) => {
            const enabled = e.target.value === "template";
            setTemplate(enabled);
            change(enabled ? "" : initial());
          }}
        >
          <option value="literal">直接填写</option>
          <option value="template">内置变量或前一步结果</option>
        </select>
      </label>
      {template ? (
        <label className="form-group" htmlFor={id}>
          原模板表达式
          <input
            id={id}
            className="form-control"
            required
            value={String(value ?? "")}
            onChange={(e) => change(e.target.value)}
            placeholder="${alias.field} 或 $automation_id"
          />
        </label>
      ) : (
        children
      )}
    </fieldset>
  );
}

function ValueField({
  id,
  label,
  value,
  change,
}: {
  id: string;
  label: string;
  value: unknown;
  change: (value: unknown) => void;
}) {
  const kindOf = (v: unknown) =>
    v == null ? "null" : typeof v === "object" ? "json" : typeof v;
  const [kind, setKind] = useState(kindOf(value));
  const [draft, setDraft] = useState(
    typeof value === "object"
      ? JSON.stringify(value ?? null, null, 2)
      : String(value ?? ""),
  );
  return (
    <div className="form-group">
      <label htmlFor={`${id}-kind`}>{label} · 值类型</label>
      <select
        id={`${id}-kind`}
        className="form-control"
        value={kind}
        onChange={(e) => {
          const next = e.target.value;
          setKind(next);
          const initial =
            next === "string"
              ? ""
              : next === "number"
                ? 0
                : next === "boolean"
                  ? false
                  : next === "json"
                    ? {}
                    : null;
          setDraft(
            typeof initial === "object"
              ? JSON.stringify(initial, null, 2)
              : String(initial),
          );
          change(initial);
        }}
      >
        {[
          ["string", "文本"],
          ["number", "数值"],
          ["boolean", "开关"],
          ["null", "空值"],
          ["json", "对象或数组"],
        ].map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
      {kind === "boolean" ? (
        <label htmlFor={id}>
          {label}
          <select
            id={id}
            className="form-control"
            value={String(value)}
            onChange={(e) => change(e.target.value === "true")}
          >
            <option value="false">false</option>
            <option value="true">true</option>
          </select>
        </label>
      ) : (
        kind !== "null" && (
          <label htmlFor={id}>
            {label}
            <textarea
              key={kind}
              id={id}
              className="form-control"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                try {
                  const next =
                    kind === "string"
                      ? e.target.value
                      : JSON.parse(e.target.value);
                  if (
                    kind === "number" &&
                    (typeof next !== "number" || !Number.isFinite(next))
                  )
                    throw new Error();
                  if (
                    kind === "json" &&
                    (next == null || typeof next !== "object")
                  )
                    throw new Error();
                  e.target.setCustomValidity("");
                  change(next);
                } catch {
                  e.target.setCustomValidity("请填写所选类型的有效值。");
                }
              }}
            />
          </label>
        )
      )}
    </div>
  );
}

function MappingField({
  name,
  field,
  root,
  value,
  change,
  id,
  label,
  options,
  depth,
}: {
  name: string;
  field: Row;
  root: Row;
  value: unknown;
  change: (v: unknown) => void;
  id: string;
  label: string;
  options: Options;
  depth: number;
}) {
  const [key, setKey] = useState("");
  const values = (
    value && typeof value === "object" && !Array.isArray(value) ? value : {}
  ) as Row;
  return (
    <fieldset className="config-array">
      <legend>{label}</legend>
      {Object.entries(values).map(([key, item]) => (
        <div className="config-array-item" key={key}>
          <Field
            name={key}
            raw={(field.additionalProperties || {}) as Row}
            root={root}
            value={item}
            change={(next) => change({ ...values, [key]: next })}
            prefix={id}
            required
            options={options}
            depth={depth + 1}
          />
          <button
            type="button"
            className="btn-secondary"
            onClick={() => {
              const next = { ...values };
              delete next[key];
              change(next);
            }}
          >
            移除 {key}
          </button>
        </div>
      ))}
      <label className="form-group" htmlFor={`${id}-key`}>
        {name} · 新字段
        <input
          id={`${id}-key`}
          className="form-control"
          maxLength={128}
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn-secondary"
        disabled={
          !key.trim() ||
          key in values ||
          Object.keys(values).length >= 256 ||
          ["__proto__", "constructor", "prototype"].includes(key)
        }
        onClick={() => {
          change({
            ...values,
            [key]: initialSchemaValue(
              (field.additionalProperties || {}) as Row,
              root,
            ),
          });
          setKey("");
        }}
      >
        添加字段
      </button>
    </fieldset>
  );
}

function Field({
  name,
  raw,
  root,
  value,
  change,
  prefix,
  required,
  options,
  depth,
  literal = false,
}: {
  name: string;
  raw: Row;
  root: Row;
  value: unknown;
  change: (value: unknown) => void;
  prefix: string;
  required: boolean;
  options: Options;
  depth: number;
  literal?: boolean;
}) {
  const field = resolve(raw, root),
    id = `${prefix}-${name}`;
  const label = `${options.labels?.[name] || name}${required ? " *" : ""}`;
  const choices = (values: string[]) =>
    options.choices?.(name, values) || values;
  const reset = !required && (
    <button
      type="button"
      className="btn-secondary"
      onClick={() => change(undefined)}
    >
      使用默认值
    </button>
  );
  if (depth > 16)
    return (
      <p className="error-note">{label}：嵌套层级过深，请在服务器配置。</p>
    );
  if (
    options.templates &&
    !literal &&
    field.type &&
    field.type !== "string" &&
    field.const === undefined
  ) {
    return (
      <TemplateField
        id={id}
        label={label}
        value={value}
        change={change}
        initial={() => initialSchemaValue(field, root)}
      >
        <Field
          name={name}
          raw={raw}
          root={root}
          value={value}
          change={change}
          prefix={prefix}
          required={required}
          options={options}
          depth={depth}
          literal
        />
      </TemplateField>
    );
  }
  if (field.oneOf && field.discriminator) {
    const discriminator = String((field.discriminator as Row).propertyName);
    const variants = (field.oneOf as Row[]).map((item) => resolve(item, root));
    const tag = (item: Row) =>
      String(((item.properties as Row)[discriminator] as Row).const);
    const selected =
      variants.find(
        (item) => tag(item) === String((value as Row)?.[discriminator]),
      ) || variants[0];
    return (
      <fieldset className="config-array">
        <legend>{label}</legend>
        <label className="form-group" htmlFor={id}>
          {options.labels?.[discriminator] || discriminator}
          <select
            id={id}
            className="form-control"
            value={tag(selected)}
            onChange={(e) => {
              const next = variants.find(
                (item) => tag(item) === e.target.value,
              )!;
              change(initialSchemaValue(next, root));
            }}
          >
            {variants.map((item) => (
              <option key={tag(item)} value={tag(item)}>
                {tag(item)}
              </option>
            ))}
          </select>
        </label>
        <SchemaFields
          schema={selected}
          root={root}
          values={(value || {}) as Row}
          prefix={id}
          change={change}
          {...options}
          omit={[...(options.omit || []), discriminator]}
          depth={depth + 1}
        />
        {reset}
      </fieldset>
    );
  }
  if (field.const !== undefined)
    return (
      <p className="small">
        {label}：{String(field.const)}
      </p>
    );
  if (
    field.type === "object" &&
    !field.properties &&
    field.additionalProperties !== false
  )
    return (
      <MappingField
        name={name}
        field={field}
        root={root}
        value={value}
        change={change}
        id={id}
        label={label}
        options={options}
        depth={depth}
      />
    );
  if (!field.type && !field.enum)
    return <ValueField id={id} label={label} value={value} change={change} />;
  if (field.type === "object" && field.properties)
    return (
      <details className="json-note" open>
        <summary>{label}</summary>
        {value == null ? (
          <button
            type="button"
            className="btn-secondary"
            onClick={() => change(initialSchemaValue(field, root))}
          >
            添加覆盖
          </button>
        ) : (
          <>
            <SchemaFields
              values={value as Row}
              schema={field}
              root={root}
              prefix={id}
              change={change}
              {...options}
              depth={depth + 1}
            />
            {reset}
          </>
        )}
      </details>
    );
  if (field.type === "array") {
    const items = resolve((field.items || {}) as Row, root);
    const entries = Array.isArray(value) ? value : [];
    if (items.enum && field.uniqueItems)
      return (
        <fieldset className="config-checkboxes">
          <legend>{label}</legend>
          {choices(items.enum as string[]).map((option) => (
            <label key={option}>
              <input
                type="checkbox"
                checked={entries.includes(option)}
                onChange={(e) =>
                  change(
                    e.target.checked
                      ? [...entries, option]
                      : entries.filter((item) => item !== option),
                  )
                }
              />
              {option}
            </label>
          ))}
          {reset}
        </fieldset>
      );
    return (
      <fieldset className="config-array">
        <legend>{label}</legend>
        {entries.map((item, index) => (
          <div className="config-array-item" key={index}>
            <Field
              name={String(index + 1)}
              raw={items}
              root={root}
              value={item}
              prefix={id}
              required
              options={options}
              depth={depth + 1}
              change={(next) =>
                change(entries.map((entry, i) => (i === index ? next : entry)))
              }
            />
            <button
              type="button"
              className="btn-secondary"
              disabled={entries.length <= Number(field.minItems || 0)}
              onClick={() => change(entries.filter((_, i) => i !== index))}
            >
              删除第 {index + 1} 项
            </button>
          </div>
        ))}
        <div className="row-actions">
          <button
            type="button"
            className="btn-secondary"
            disabled={
              field.maxItems !== undefined &&
              entries.length >= Number(field.maxItems)
            }
            onClick={() =>
              change([...entries, initialSchemaValue(items, root)])
            }
          >
            添加项
          </button>
          {reset}
        </div>
      </fieldset>
    );
  }
  const enumValues = field.enum ? choices(field.enum as string[]) : undefined;
  if (
    field.type === "object" ||
    !["string", "integer", "number", "boolean"].includes(String(field.type))
  ) {
    return (
      <div className="json-note">
        <p>{label}：此字段结构需在服务器配置。</p>
        <pre>{JSON.stringify(value ?? null, null, 2)}</pre>
      </div>
    );
  }

  return (
    <label htmlFor={id} className="form-group">
      {label}
      {enumValues || field.type === "boolean" ? (
        <select
          id={id}
          className="form-control"
          value={value == null ? "" : String(value)}
          onChange={(e) =>
            change(
              e.target.value === ""
                ? undefined
                : field.type === "boolean"
                  ? e.target.value === "true"
                  : e.target.value,
            )
          }
        >
          <option value="">使用配置默认值</option>
          {(enumValues || ["true", "false"]).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      ) : field.type === "string" &&
        (Number(field.maxLength || 0) > 512 ||
          [
            "command",
            "text",
            "code",
            "instruction",
            "content",
            "description",
          ].includes(name)) ? (
        <textarea
          id={id}
          className="form-control"
          required={required}
          rows={5}
          maxLength={field.maxLength as number | undefined}
          value={value == null ? "" : String(value)}
          onChange={(e) => change(e.target.value)}
        />
      ) : (
        <input
          id={id}
          className="form-control"
          autoComplete="off"
          type={
            field.type === "integer" || field.type === "number"
              ? "number"
              : "text"
          }
          step={field.type === "integer" ? "1" : "any"}
          min={field.minimum as number | undefined}
          max={field.maximum as number | undefined}
          maxLength={field.maxLength as number | undefined}
          value={value == null ? "" : String(value)}
          placeholder={field.default == null ? "" : String(field.default)}
          onChange={(e) =>
            change(
              e.target.value === ""
                ? undefined
                : field.type === "integer" || field.type === "number"
                  ? Number(e.target.value)
                  : e.target.value,
            )
          }
        />
      )}
      {Boolean(field.description) && <small>{String(field.description)}</small>}
    </label>
  );
}

export function SchemaFields({
  values,
  schema,
  root,
  change,
  prefix,
  depth = 0,
  ...options
}: {
  values: Row;
  schema: Row;
  root: Row;
  change: (values: Row) => void;
  prefix: string;
  depth?: number;
} & Options) {
  const required = (schema.required || []) as string[];
  return (
    <div className="config-fields">
      {Object.entries((schema.properties || {}) as Row).map(([name, raw]) =>
        options.omit?.includes(name) ? null : (
          <Field
            key={name}
            name={name}
            raw={raw as Row}
            root={root}
            value={values[name]}
            prefix={prefix}
            required={required.includes(name)}
            options={options}
            depth={depth}
            change={(value) => {
              const next = { ...values };
              if (value === undefined) delete next[name];
              else next[name] = value;
              change(next);
            }}
          />
        ),
      )}
    </div>
  );
}
