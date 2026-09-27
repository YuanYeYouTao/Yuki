import { useState } from "react";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { Badge, Empty, ErrorNote, Section } from "./components";

const names: Record<string, string> = {
  model_profiles: "模型 Profile 与任务路由",
  system_prompt: "System Prompt 模板",
  bot_persona: "共享人格原文",
  provider: "供应商",
  protocol: "协议",
  base_url: "服务地址",
  api_key_env: "密钥环境变量名",
  model: "模型名称",
  timeout_seconds: "超时（秒）",
  max_retries: "最多重试次数",
  default_temperature: "默认温度",
  default_max_output_tokens: "默认输出预算",
  max_output_tokens_limit: "输出预算上限",
  reasoning_effort: "思考强度",
  structured_output_mode: "结构化输出方式",
  capabilities: "能力声明",
  base_url_env: "服务地址环境变量名",
  model_env: "模型环境变量名",
  reasoning_effort_env: "思考强度环境变量名",
  wire_options: "协议参数覆盖",
  thinking_mode: "旧版思考选项",
};

function resolve(field: Row, root: Row): Row {
  if (field.$ref)
    return resolve(
      ((root.$defs as Row)?.[String(field.$ref).split("/").pop()!] ||
        {}) as Row,
      root,
    );
  if (field.anyOf)
    return resolve(
      (field.anyOf as Row[]).find((item) => item.type !== "null") || {},
      root,
    );
  return field;
}

function SchemaFields({
  values,
  schema,
  root,
  change,
  prefix,
}: {
  values: Row;
  schema: Row;
  root: Row;
  change: (values: Row) => void;
  prefix: string;
}) {
  const properties = (schema.properties || {}) as Record<string, Row>;
  const required = (schema.required || []) as string[];
  function set(name: string, value: unknown) {
    const next = { ...values };
    if (value === undefined) delete next[name];
    else next[name] = value;
    change(next);
  }
  return (
    <div className="config-fields">
      {Object.entries(properties).map(([name, raw]) => {
        if (["id", "headers", "thinking_enabled"].includes(name)) return null;
        const field = resolve(raw, root);
        const value = values[name];
        const options = (field.enum as string[] | undefined)?.filter(
          (option) =>
            name !== "reasoning_effort" ||
            !["none", "minimal"].includes(option),
        );
        const id = `${prefix}-${name}`;
        if (field.type === "object")
          return (
            <details key={name} className="json-note">
              <summary>{names[name] || name}</summary>
              {value == null ? (
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => set(name, {})}
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
                    change={(next) => set(name, next)}
                  />
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => set(name, undefined)}
                  >
                    使用供应商默认参数
                  </button>
                </>
              )}
            </details>
          );
        if (field.type === "array") {
          const items = resolve((field.items || {}) as Row, root);
          const choices = (items.enum as string[] | undefined)?.filter(
            (option) =>
              name !== "effort_levels" || !["none", "minimal"].includes(option),
          );
          if (choices)
            return (
              <fieldset key={name} className="config-checkboxes">
                <legend>{names[name] || name}</legend>
                {choices.map((option) => (
                  <label key={option}>
                    <input
                      type="checkbox"
                      checked={((value || []) as string[]).includes(option)}
                      onChange={(e) =>
                        set(
                          name,
                          choices.filter((item) =>
                            item === option
                              ? e.target.checked
                              : ((value || []) as string[]).includes(item),
                          ),
                        )
                      }
                    />
                    {option}
                  </label>
                ))}
                {!required.includes(name) && (
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => set(name, undefined)}
                  >
                    使用默认值
                  </button>
                )}
              </fieldset>
            );
        }
        return (
          <label key={name} htmlFor={id} className="form-group">
            {names[name] || name}
            {required.includes(name) ? " *" : ""}
            {options || field.type === "boolean" ? (
              <select
                id={id}
                className="form-control"
                value={value == null ? "" : String(value)}
                onChange={(e) =>
                  set(
                    name,
                    e.target.value === ""
                      ? undefined
                      : field.type === "boolean"
                        ? e.target.value === "true"
                        : e.target.value,
                  )
                }
              >
                <option value="">使用配置默认值</option>
                {(options || ["true", "false"]).map((option) => (
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
                value={value == null ? "" : String(value)}
                placeholder={field.default == null ? "" : String(field.default)}
                onChange={(e) =>
                  set(
                    name,
                    e.target.value === ""
                      ? undefined
                      : field.type === "integer" || field.type === "number"
                        ? Number(e.target.value)
                        : e.target.value,
                  )
                }
              />
            )}
          </label>
        );
      })}
    </div>
  );
}

function ModelDocument({
  fields,
  document,
  change,
}: {
  fields: Row;
  document: Row;
  change: (document: Row) => void;
}) {
  const profiles = document.profiles as Record<string, Row>;
  const routes = document.routes as Record<string, string>;
  const [selected, select] = useState(Object.keys(profiles)[0] || "");
  const [newId, setNewId] = useState("");
  const [error, setError] = useState("");
  const schema = fields.profile_schema as Row;
  function update(profile: Row) {
    change({ ...document, profiles: { ...profiles, [selected]: profile } });
  }
  function add() {
    if (!/^[a-zA-Z0-9_.-]+$/.test(newId) || Object.hasOwn(profiles, newId)) {
      setError("请输入未使用的 Profile ID（字母、数字、点、短横线或下划线）。");
      return;
    }
    change({
      ...document,
      profiles: {
        ...profiles,
        [newId]: {
          provider: "openai",
          protocol: "chat_completions",
          model: "",
          timeout_seconds: 60,
          max_retries: 2,
          default_temperature: 1,
          default_max_output_tokens: 4096,
          capabilities: ["reasoning", "tools", "structured_output"],
        },
      },
    });
    select(newId);
    setNewId("");
    setError("");
  }
  const profile = profiles[selected];
  return (
    <>
      <div className="settings-actions">
        <label className="form-group">
          编辑 Profile
          <select
            className="form-control"
            value={selected}
            onChange={(e) => select(e.target.value)}
          >
            {Object.keys(profiles).map((id) => (
              <option key={id}>{id}</option>
            ))}
          </select>
        </label>
        <label className="form-group">
          新 Profile ID
          <input
            className="form-control"
            value={newId}
            onChange={(e) => setNewId(e.target.value)}
            autoComplete="off"
          />
        </label>
        <button type="button" className="btn-secondary" onClick={add}>
          添加 Profile
        </button>
      </div>
      {error && (
        <p role="alert" className="error-note">
          {error}
        </p>
      )}
      {profile && (
        <>
          <h3>{selected}</h3>
          <SchemaFields
            values={profile}
            schema={schema}
            root={schema}
            prefix={`profile-${selected}`}
            change={update}
          />
          <details className="json-note">
            <summary>环境变量引用</summary>
            <p className="small">
              引用会覆盖同名直接值；只填写变量名，值在服务器读取。
            </p>
            {["base_url_env", "model_env", "reasoning_effort_env"].map(
              (name) => (
                <label className="form-group" key={name}>
                  {names[name]}
                  <input
                    className="form-control"
                    value={String(profile[name] || "")}
                    autoComplete="off"
                    onChange={(e) => {
                      const next = { ...profile };
                      if (e.target.value) next[name] = e.target.value;
                      else delete next[name];
                      update(next);
                    }}
                  />
                </label>
              ),
            )}
          </details>
          <button
            className="btn-secondary"
            type="button"
            disabled={Object.values(routes).includes(selected)}
            onClick={() => {
              const next = { ...profiles };
              delete next[selected];
              change({ ...document, profiles: next });
              select(Object.keys(next)[0] || "");
            }}
          >
            删除此 Profile
          </button>
          {Object.values(routes).includes(selected) && (
            <p className="small">
              任务仍引用此 Profile；先调整下面的路由再删除。
            </p>
          )}
        </>
      )}
      <h3>所有任务的路由</h3>
      <div className="config-fields">
        {(fields.tasks as string[]).map((task) => (
          <label className="form-group" key={task}>
            {task}
            <select
              className="form-control"
              value={routes[task] || ""}
              onChange={(e) =>
                change({
                  ...document,
                  routes: { ...routes, [task]: e.target.value },
                })
              }
            >
              <option value="">请选择 Profile</option>
              {Object.keys(profiles).map((id) => (
                <option key={id}>{id}</option>
              ))}
            </select>
          </label>
        ))}
      </div>
      <p className="small">
        保存前会核验全部路由、能力声明、协议参数与服务器环境变量。思考保持应用规定的下限。密钥值不读取；已有自定义请求头原样保留，由服务器配置。
      </p>
    </>
  );
}

function Draft({ fields, props }: { fields: Row; props: PageProps }) {
  const fileId = String(fields.file_id);
  const [document, setDocument] = useState<Row>((fields.document || {}) as Row);
  const [content, setContent] = useState(String(fields.content || ""));
  return (
    <>
      {fields.exists === false && (
        <p className="small">
          尚未创建此已配置文件。保存会在服务器指定的目录中创建。
        </p>
      )}
      <p className="small">
        磁盘版本 {String(fields.revision)} · 校验 <Badge value={fields.valid} />{" "}
        · 与当前加载一致 <Badge value={fields.matches_loaded} />
      </p>
      {!fields.valid && (
        <p className="error-note" role="alert">
          磁盘配置未通过校验。修正后保存；当前运行配置不会随编辑改变。
        </p>
      )}
      {fileId === "model_profiles" &&
      fields.document &&
      fields.profile_schema ? (
        <ModelDocument
          fields={fields}
          document={document}
          change={setDocument}
        />
      ) : fileId === "model_profiles" ? (
        <Empty>磁盘文件无法安全解析，请在服务器修正格式后重新读取。</Empty>
      ) : (
        <label className="form-group">
          {names[fileId]}
          <textarea
            className="form-control code-editor persona-editor"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            spellCheck={false}
          />
        </label>
      )}
      <p className="small">
        保存后需重启应用才会加载。不会重写历史聊天或重跑已有工作；实际注入内容可在执行轨迹中查看。
      </p>
      {fields.writable_directory === false && (
        <p className="error-note">
          配置目录当前不可写。请使用可写的启动文件目录；operator
          声明仍应保留在只读配置目录。
        </p>
      )}
      {props.allowed("save_config_file") &&
        (fileId !== "model_profiles" || fields.profile_schema) && (
          <button
            className="btn-primary"
            disabled={fields.writable_directory === false}
            onClick={() =>
              props.act({
                method: "save_config_file",
                label: `保存${names[fileId]}`,
                revision: Number(fields.revision),
                payload: {
                  action: "save",
                  resource_id: fileId,
                  spec:
                    fileId === "model_profiles" ? { document } : { content },
                },
                review: fileId === "model_profiles" ? document : content,
                hint: "将核对刚读取的文件版本并原子保存。持久回执中的 saved_pending_restart 表示已保存，尚需重启加载。",
              })
            }
          >
            检查并保存
          </button>
        )}
    </>
  );
}

export function ConfigFile({
  fileId,
  props,
}: {
  fileId: string;
  props: PageProps;
}) {
  const { data, error, loading } = useQuery<Row>(
    "read_config_file",
    { file_id: fileId },
    props.refresh,
    props.allowed("read_config_file"),
  );
  return (
    <Section title={names[fileId]}>
      {error != null && <ErrorNote error={error} />}
      {loading && <Empty>正在读取磁盘文件…</Empty>}
      {!props.allowed("read_config_file") && (
        <Empty>需要文件配置正文读取权限。</Empty>
      )}
      {data && (
        <Draft
          key={`${fileId}:${String((data.fields as Row).revision)}`}
          fields={data.fields as Row}
          props={props}
        />
      )}
    </Section>
  );
}
