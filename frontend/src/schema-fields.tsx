import type { Row } from "./api";

type Options = {
  labels?: Record<string, string>;
  omit?: string[];
  choices?: (name: string, values: string[]) => string[];
};

function resolve(field: Row, root: Row, depth = 0): Row {
  if (depth > 16) return {};
  if (field.$ref)
    return resolve(
      ((root.$defs as Row)?.[String(field.$ref).split("/").pop()!] ||
        {}) as Row,
      root,
      depth + 1,
    );
  if (field.anyOf)
    return resolve(
      (field.anyOf as Row[]).find((item) => item.type !== "null") || {},
      root,
      depth + 1,
    );
  return field;
}

function initial(raw: Row, root: Row, depth = 0): unknown {
  const field = resolve(raw, root);
  if (field.default !== undefined) return structuredClone(field.default);
  if (field.const !== undefined) return field.const;
  if (depth > 16) return null;
  if (field.type === "object") {
    return Object.fromEntries(
      Object.entries((field.properties || {}) as Row)
        .filter(
          ([name, prop]) =>
            ((field.required || []) as string[]).includes(name) ||
            (prop as Row).default !== undefined,
        )
        .map(([name, prop]) => [name, initial(prop as Row, root, depth + 1)]),
    );
  }
  if (field.type === "array") return [];
  if (field.enum) return (field.enum as unknown[])[0];
  if (field.type === "boolean") return false;
  if (field.type === "number" || field.type === "integer")
    return field.minimum ?? 0;
  return "";
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
  if (field.type === "object" && field.properties)
    return (
      <details className="json-note" open>
        <summary>{label}</summary>
        {value == null ? (
          <button
            type="button"
            className="btn-secondary"
            onClick={() => change(initial(field, root))}
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
            onClick={() => change([...entries, initial(items, root)])}
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
