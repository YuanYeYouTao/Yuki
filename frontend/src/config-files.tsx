import { useState } from "react";
import { SchemaFields } from "./schema-fields";
import { AutonomyParameterFields } from "./autonomy-parameters";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { Badge, Empty, ErrorNote, Section } from "./components";

const names: Record<string, string> = {
  model_profiles: "模型 Profile 与任务路由",
  system_prompt: "主人格提示词（System Prompt）",
  bot_persona: "共享人格提示词（当前参与组装）",
  autonomous_model: "自主机会 · 热更新参数",
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
            labels={names}
            omit={["id", "headers", "thinking_enabled"]}
            choices={(name, values) =>
              ["reasoning_effort", "effort_levels"].includes(name)
                ? values.filter((value) => !["none", "minimal"].includes(value))
                : values
            }
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
  const hotReload = fields.apply_mode === "hot_reload";
  const [hasDocument, setHasDocument] = useState(fields.document != null);
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
      {fileId === "autonomous_model" && fields.parameter_schema ? (
        hasDocument ? (
          <AutonomyParameterFields
            fields={fields}
            document={document}
            change={setDocument}
          />
        ) : (
          <>
            <Empty>
              磁盘参数格式无效。可在服务器修正，或明确以默认参数建立新草稿。
            </Empty>
            <button
              className="btn-secondary"
              onClick={() => {
                setDocument(structuredClone(fields.defaults as Row));
                setHasDocument(true);
              }}
            >
              使用默认参数建立草稿
            </button>
          </>
        )
      ) : fileId === "model_profiles" &&
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
        {hotReload
          ? "保存后由原控制器下一次采样加载；回执只证明文件保存。刷新页面核对生效值，控制器未运行时不会冒充已加载。不会重算历史、重跑或重置已有工作。"
          : "保存后需重启应用才会加载。不会重写历史聊天或重跑已有工作；实际注入内容可在执行轨迹中查看。"}
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
            disabled={
              fields.writable_directory === false || (hotReload && !hasDocument)
            }
            onClick={() =>
              props.act({
                method: "save_config_file",
                label: `保存${names[fileId]}`,
                revision: Number(fields.revision),
                payload: {
                  action: "save",
                  resource_id: fileId,
                  spec:
                    fileId === "model_profiles" || hotReload
                      ? { document }
                      : { content },
                },
                review:
                  fileId === "model_profiles" || hotReload ? document : content,
                hint: hotReload
                  ? "核对原文件版本并原子保存。saved_pending_reload 表示已保存，等待原控制器下一轮加载；实际生效请刷新核对。"
                  : "将核对刚读取的文件版本并原子保存。持久回执中的 saved_pending_restart 表示已保存，尚需重启加载。",
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
