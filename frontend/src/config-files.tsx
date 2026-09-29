import { useState } from "react";
import { SchemaFields } from "./schema-fields";
import { AutonomyParameterFields } from "./autonomy-parameters";
import type { Row } from "./api";
import type { PageProps } from "./pages";
import { useQuery } from "./hooks";
import { Badge, Empty, ErrorNote, Section } from "./components";

const names: Record<string, string> = {
  model_profiles: "模型接入与任务用途",
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
const taskNames: Record<string, string> = {
  chat_agent: "主对话",
  memory_extraction: "记忆提取",
  memory_self_reflection: "自省",
  memory_consolidation: "记忆整理",
  memory_dream: "记忆归纳",
  memory_attribution: "记忆归属",
  relationship_evaluation: "关系评估",
  emoji_replacement: "表情替换",
  automation_text_generation: "自动化文案",
  automation_agent: "自动化执行",
  plugin_agent_session: "插件会话",
  utility_structured: "结构化任务",
  conversation_compaction: "会话压缩",
};
const providerPresets = [
  {
    id: "deepseek",
    label: "DeepSeek",
    protocol: "responses",
    url: "https://api.deepseek.com",
  },
  {
    id: "openai",
    label: "OpenAI",
    protocol: "responses",
    url: "https://api.openai.com/v1",
  },
  {
    id: "anthropic",
    label: "Anthropic",
    protocol: "anthropic_messages",
    url: "https://api.anthropic.com",
  },
  {
    id: "gemini",
    label: "Google Gemini",
    protocol: "gemini",
    url: "https://generativelanguage.googleapis.com/v1beta",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    protocol: "chat_completions",
    url: "https://openrouter.ai/api/v1",
  },
  { id: "qwen", label: "阿里云 Qwen", protocol: "chat_completions", url: "" },
  {
    id: "moonshot",
    label: "Moonshot / Kimi",
    protocol: "chat_completions",
    url: "",
  },
  { id: "zhipu", label: "智谱 GLM", protocol: "chat_completions", url: "" },
  { id: "doubao", label: "火山豆包", protocol: "chat_completions", url: "" },
  { id: "minimax", label: "MiniMax", protocol: "chat_completions", url: "" },
  {
    id: "siliconflow",
    label: "硅基流动",
    protocol: "chat_completions",
    url: "",
  },
  { id: "together", label: "Together", protocol: "chat_completions", url: "" },
  { id: "groq", label: "Groq", protocol: "chat_completions", url: "" },
  { id: "mistral", label: "Mistral", protocol: "chat_completions", url: "" },
  { id: "xai", label: "xAI", protocol: "chat_completions", url: "" },
  {
    id: "azure_openai",
    label: "Azure OpenAI v1",
    protocol: "chat_completions",
    url: "",
  },
  {
    id: "openai_compatible",
    label: "其他 OpenAI 兼容服务",
    protocol: "chat_completions",
    url: "",
  },
] as const;
const connectionLabel = (profile: Row) => {
  const provider = String(profile.provider || "");
  const name =
    providerPresets.find((item) => item.id === provider)?.label || provider;
  return `${name || "未选供应商"} · ${String(profile.model || "未选模型")}`;
};
const protocolLabels: Record<string, string> = {
  chat_completions: "Chat Completions",
  responses: "Responses",
  anthropic_messages: "Claude Messages",
  gemini: "Gemini GenerateContent",
};
const protocolsFor = (provider: unknown) =>
  provider === "anthropic"
    ? ["anthropic_messages"]
    : provider === "gemini"
      ? ["gemini"]
      : ["deepseek", "openai", "openai_compatible"].includes(String(provider))
        ? ["chat_completions", "responses"]
        : ["chat_completions"];
const presetValues = (provider: string): Row => {
  const preset = providerPresets.find((item) => item.id === provider);
  const gemini = provider === "gemini";
  return {
    provider,
    protocol: preset?.protocol || "chat_completions",
    base_url: preset?.url || "",
    model: gemini ? "gemini-3.8-flash" : "",
    api_key_env: "",
    reasoning_effort: "low",
    search_mode: "external",
    capabilities: gemini
      ? [
          "reasoning",
          "tools",
          "structured_output",
          "image_input",
          "long_context",
        ]
      : ["reasoning", "tools", "structured_output"],
  };
};

function ModelDocument({
  fields,
  document,
  change,
  keyInputs,
  changeKey,
}: {
  fields: Row;
  document: Row;
  change: (document: Row) => void;
  keyInputs: Record<string, string>;
  changeKey: (id: string, value: string) => void;
}) {
  const profiles = document.profiles as Record<string, Row>;
  const routes = document.routes as Record<string, string>;
  const resolvedProfiles = (fields.resolved_profiles || {}) as Record<
    string,
    Row
  >;
  const labelOf = (id: string) => {
    const label = connectionLabel({
      ...(resolvedProfiles[id] || {}),
      ...(profiles[id] || {}),
    });
    const uses = Object.entries(routes)
      .filter(([, connection]) => connection === id)
      .map(([task]) => taskNames[task] || task);
    if (document.search_connection === id)
      uses.push(
        fields.search_backend === "deepseek_anthropic"
          ? "联网搜索"
          : "预选搜索连接",
      );
    const purpose = uses.length
      ? `${uses[0]}${uses.length > 1 ? `等 ${uses.length} 项` : ""}`
      : "未分配用途";
    return `${label}（${purpose}）`;
  };
  const [selected, select] = useState(Object.keys(profiles)[0] || "");
  const [newProvider, setNewProvider] = useState("");
  const schema = fields.profile_schema as Row;
  function update(profile: Row) {
    change({ ...document, profiles: { ...profiles, [selected]: profile } });
  }
  function add() {
    if (!newProvider) return;
    const id = `connection_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
    change({
      ...document,
      routes:
        Object.keys(profiles).length === 0
          ? Object.fromEntries(
              (fields.tasks as string[]).map((task) => [task, id]),
            )
          : routes,
      profiles: {
        ...profiles,
        [id]: {
          timeout_seconds: 120,
          max_retries: 2,
          default_temperature: 0.7,
          default_max_output_tokens: 8192,
          ...presetValues(newProvider),
        },
      },
    });
    select(id);
    setNewProvider("");
  }
  const profile = profiles[selected];
  const resolved = resolvedProfiles[selected];
  const legacyGeminiBudget =
    profile?.protocol === "gemini" &&
    String(profile.model) === "gemini-3.8-flash" &&
    (profile.wire_options as Row | undefined)?.reasoning === "budget";
  const displayedEffort = String(
    profile?.reasoning_effort_env
      ? resolved?.reasoning_effort || profile.reasoning_effort || "low"
      : profile?.reasoning_effort || "low",
  );
  function setEffort(value: string) {
    const next: Row = { ...profile, reasoning_effort: value };
    delete next.reasoning_effort_env;
    if (legacyGeminiBudget) {
      const options = { ...(profile.wire_options as Row) };
      options.reasoning = "gemini";
      delete options.thinking_budget_tokens;
      next.wire_options = options;
    }
    update(next);
  }
  const savedKeyProfiles = (fields.saved_api_key_profiles || []) as string[];
  return (
    <>
      <div className="provider-intro">
        <strong>1. 添加或编辑模型连接</strong>
        <span>
          选择供应商，填写 API 地址、模型和 API Key；密钥只发送给 Yuki
          服务器保存，不在页面回显。
        </span>
      </div>
      <div className="provider-profile-list" aria-label="模型连接">
        {Object.entries(profiles).map(([id, item]) => (
          <button
            type="button"
            key={id}
            className={`provider-profile ${selected === id ? "selected" : ""}`}
            aria-pressed={selected === id}
            onClick={() => select(id)}
          >
            <strong>{labelOf(id)}</strong>
            <span>
              {keyInputs[id]
                ? "有待保存的新 API Key"
                : savedKeyProfiles.includes(id) &&
                    String(item.api_key_env || "").startsWith("YUKI_WEBUI_KEY_")
                  ? "API Key 已保存"
                  : item.api_key_env
                    ? "使用服务器已有密钥"
                    : "尚未设置密钥"}
            </span>
            <small>
              {Object.values(routes).filter((route) => route === id).length}{" "}
              个任务用途
              {document.search_connection === id
                ? fields.search_backend === "deepseek_anthropic"
                  ? " · 联网搜索"
                  : " · 预选搜索连接"
                : ""}
            </small>
          </button>
        ))}
      </div>
      <div className="settings-actions">
        <label className="form-group">
          新连接供应商
          <select
            className="form-control"
            value={newProvider}
            onChange={(event) => setNewProvider(event.target.value)}
          >
            <option value="">先选择供应商</option>
            {providerPresets.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="form-group">
          当前模型连接
          <select
            className="form-control"
            value={selected}
            onChange={(e) => select(e.target.value)}
          >
            {Object.keys(profiles).map((id) => (
              <option key={id} value={id}>
                {labelOf(id)}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="btn-secondary"
          disabled={!newProvider}
          onClick={add}
        >
          添加模型连接
        </button>
      </div>
      {profile && (
        <>
          <h3>编辑 {labelOf(selected)}</h3>
          <div className="provider-basic-fields">
            <label className="form-group">
              供应商
              <select
                className="form-control"
                value={String(profile.provider || "")}
                onChange={(event) => {
                  const next: Row = {
                    ...profile,
                    ...presetValues(event.target.value),
                  };
                  delete next.base_url_env;
                  delete next.model_env;
                  delete next.reasoning_effort_env;
                  delete next.wire_options;
                  update(next);
                  changeKey(selected, "");
                }}
              >
                {!providerPresets.some(
                  (item) => item.id === profile.provider,
                ) && (
                  <option value={String(profile.provider || "")}>
                    {String(profile.provider || "当前供应商")}
                  </option>
                )}
                {providerPresets.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-group">
              接口协议
              <select
                className="form-control"
                value={String(profile.protocol || "chat_completions")}
                onChange={(event) =>
                  update({ ...profile, protocol: event.target.value })
                }
              >
                {protocolsFor(profile.provider).map((protocol) => (
                  <option value={protocol} key={protocol}>
                    {protocolLabels[protocol]}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-group">
              API Base URL
              <input
                className="form-control"
                type="url"
                value={String(profile.base_url || resolved?.base_url || "")}
                onChange={(event) => {
                  const next: Row = {
                    ...profile,
                    base_url: event.target.value,
                  };
                  delete next.base_url_env;
                  update(next);
                }}
                placeholder="https://api.example.com/v1"
                autoComplete="url"
              />
            </label>
            <label className="form-group">
              模型 ID
              <input
                className="form-control"
                value={String(profile.model || resolved?.model || "")}
                onChange={(event) => {
                  const next: Row = { ...profile, model: event.target.value };
                  delete next.model_env;
                  update(next);
                }}
                placeholder="输入供应商提供的模型 ID"
                autoComplete="off"
              />
            </label>
            <label className="form-group">
              API Key
              <input
                className="form-control"
                type="password"
                value={keyInputs[selected] || ""}
                onChange={(event) => changeKey(selected, event.target.value)}
                placeholder={
                  profile.api_key_env ? "留空沿用已保存的密钥" : "粘贴 API Key"
                }
                autoComplete="new-password"
              />
              <small>只在保存时提交新输入；页面不会取回原密钥。</small>
            </label>
          </div>
          {profile.protocol === "gemini" && (
            <label className="form-group">
              思考强度
              <select
                className="form-control"
                aria-label="思考强度"
                value={displayedEffort}
                onChange={(event) => setEffort(event.target.value)}
              >
                <option value="low">低</option>
                <option value="medium">中</option>
                <option value="high">高</option>
              </select>
              <small>
                {profile.reasoning_effort_env
                  ? `当前由服务器环境变量 ${String(profile.reasoning_effort_env)} 覆盖；在这里改档会解除覆盖。`
                  : "保存后新模型请求使用所选档位；进行中的请求保持原档位。"}
                {legacyGeminiBudget &&
                  " 当前旧连接使用固定预算；改档会切换为 thinkingLevel。"}
              </small>
            </label>
          )}
          {profile.protocol === "gemini" &&
            (profile.reasoning_effort_env || legacyGeminiBudget) && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setEffort(displayedEffort)}
              >
                使用当前档位并移除旧覆盖
              </button>
            )}
          <label className="form-group">
            此连接的联网搜索
            <select
              className="form-control"
              aria-label="此连接的联网搜索"
              value={String(profile.search_mode || "inherit")}
              onChange={(event) => {
                const mode = event.target.value;
                const capabilities = Array.isArray(profile.capabilities)
                  ? [...profile.capabilities]
                  : [];
                const next: Row = {
                  ...profile,
                  search_mode: mode === "inherit" ? null : mode,
                };
                if (
                  ["native", "both"].includes(mode) &&
                  !capabilities.includes("native_web_search")
                ) {
                  capabilities.push("native_web_search");
                  next.capabilities = capabilities;
                }
                update(next);
              }}
            >
              {!profile.search_mode && (
                <option value="inherit">沿用部署搜索设置（旧连接）</option>
              )}
              <option value="external">仅外部搜索</option>
              {profile.protocol === "gemini" && (
                <option value="bridge">Gemini 独立原生搜索桥（推荐）</option>
              )}
              {profile.protocol === "gemini" &&
                profile.search_mode === "native" && (
                  <option value="native">旧直接原生模式（可能不兼容）</option>
                )}
              {profile.protocol === "gemini" &&
                profile.search_mode === "both" && (
                  <option value="both">旧组合模式（可能不兼容）</option>
                )}
              {(profile.protocol === "anthropic_messages" ||
                (profile.protocol === "responses" &&
                  profile.provider !== "deepseek")) && (
                <option value="native">仅供应商原生搜索</option>
              )}
              {profile.protocol === "responses" &&
                profile.provider !== "deepseek" && (
                  <option value="both">原生搜索与外部搜索</option>
                )}
            </select>
            <small>
              {profile.protocol === "gemini"
                ? "独立搜索桥会让主模型保留稳定的 web_search/read_webpage 函数，仅在调用 web_search 时另发只含 Google 搜索的 Gemini 请求；需要部署配置 Tavily 用于网页读取和失败降级。原有直接原生模式可能与函数声明不兼容。"
                : profile.protocol === "anthropic_messages"
                  ? "Claude 原生搜索与外部 web_search 同名，因此只能二选一。"
                  : "外部搜索需在部署中配置；原生搜索需供应商和模型实际支持。"}
              原生搜索由供应商执行，可能产生额外费用。
            </small>
          </label>
          <details className="json-note">
            <summary>高级参数与能力声明</summary>
            <SchemaFields
              values={profile}
              labels={names}
              omit={[
                "id",
                "headers",
                "thinking_enabled",
                "thinking_mode",
                "provider",
                "protocol",
                "base_url",
                "model",
                "api_key_env",
                "search_mode",
                ...(profile.protocol === "gemini" ? ["reasoning_effort"] : []),
              ]}
              choices={(name, values) => {
                if (!["reasoning_effort", "effort_levels"].includes(name))
                  return values;
                const supported =
                  profile.provider === "gemini" &&
                  String(profile.model) === "gemini-3.8-flash"
                    ? ["low", "medium", "high"]
                    : values.filter(
                        (value) => !["none", "minimal"].includes(value),
                      );
                return values.filter((value) => supported.includes(value));
              }}
              schema={schema}
              root={schema}
              prefix={`profile-${selected}`}
              change={update}
            />
            <details className="json-note">
              <summary>沿用服务器环境变量</summary>
              <p className="small">
                仅用于已有部署。编辑上面的地址或模型会改用直接填写的值。
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
            <label className="form-group">
              已有密钥的环境变量名
              <input
                className="form-control"
                value={String(profile.api_key_env || "")}
                onChange={(event) =>
                  update({ ...profile, api_key_env: event.target.value })
                }
                autoComplete="off"
              />
            </label>
            <p className="small">内部连接编号：{selected}</p>
          </details>
          <button
            className="btn-secondary"
            type="button"
            disabled={
              Object.values(routes).includes(selected) ||
              document.search_connection === selected
            }
            onClick={() => {
              const next = { ...profiles };
              delete next[selected];
              change({ ...document, profiles: next });
              select(Object.keys(next)[0] || "");
            }}
          >
            删除此模型连接
          </button>
          {(Object.values(routes).includes(selected) ||
            document.search_connection === selected) && (
            <p className="small">
              仍有任务或联网搜索使用此连接；先调整下面的用途再删除。
            </p>
          )}
        </>
      )}
      <div className="provider-intro">
        <strong>2. 联网搜索使用的模型连接</strong>
        <span>
          若部署使用 DeepSeek 搜索桥，请独立选择一条官方 DeepSeek
          连接；切换主对话模型不会改动此选择。
        </span>
      </div>
      <label className="form-group">
        搜索连接
        <select
          className="form-control"
          aria-label="搜索连接"
          value={String(document.search_connection || "")}
          onChange={(event) =>
            change({
              ...document,
              search_connection: event.target.value || null,
            })
          }
        >
          <option value="">未选择</option>
          {Object.entries(profiles)
            .filter(([, item]) => item.provider === "deepseek")
            .map(([id]) => (
              <option key={id} value={id}>
                {labelOf(id)}
              </option>
            ))}
        </select>
        <small>
          {fields.search_backend === "deepseek_anthropic"
            ? "当前部署使用 DeepSeek 搜索桥，保存时必须选一条官方 DeepSeek 连接。搜索桥固定调用 deepseek-flash；此处取用连接的 API Key。"
            : "当前部署使用其他搜索后端；此选择会保留，供以后切换搜索后端使用。"}
        </small>
      </label>
      <div className="provider-intro">
        <strong>3. 为任务选择模型</strong>
        <span>每个用途直接选择上面配置的模型；保存后立即用于新任务。</span>
      </div>
      {profile && (
        <div className="settings-actions">
          <button
            type="button"
            className="btn-secondary"
            onClick={() =>
              change({
                ...document,
                routes: Object.fromEntries(
                  (fields.tasks as string[]).map((task) => [task, selected]),
                ),
              })
            }
          >
            全部用途使用当前模型连接
          </button>
          <span className="small">
            将下方全部 {(fields.tasks as string[]).length} 个用途指向{" "}
            {connectionLabel({ ...(resolved || {}), ...profile })}
            ；保存后用于新任务。
          </span>
        </div>
      )}
      <div className="provider-routes">
        {(fields.tasks as string[]).map((task) => (
          <label className="provider-route" key={task}>
            <span>
              <strong>{taskNames[task] || task}</strong>
              <small>{task}</small>
            </span>
            <select
              className="form-control"
              aria-label={`${taskNames[task] || task}使用的模型`}
              value={routes[task] || ""}
              onChange={(e) =>
                change({
                  ...document,
                  routes: { ...routes, [task]: e.target.value },
                })
              }
            >
              <option value="">请选择模型连接</option>
              {Object.keys(profiles).map((id) => (
                <option key={id} value={id}>
                  {labelOf(id)}
                </option>
              ))}
            </select>
          </label>
        ))}
      </div>
      <p className="small">
        保存前会核验全部用途、能力声明和协议参数。已有自定义请求头保留在服务器；API
        Key 不会回显。
      </p>
    </>
  );
}

function Draft({ fields, props }: { fields: Row; props: PageProps }) {
  const fileId = String(fields.file_id);
  const [document, setDocument] = useState<Row>((fields.document || {}) as Row);
  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState("");
  const [content, setContent] = useState(String(fields.content || ""));
  const hotReload = fields.apply_mode === "hot_reload";
  const [hasDocument, setHasDocument] = useState(fields.document != null);
  function saveModelDocument() {
    const next = structuredClone(document);
    const profiles = next.profiles as Record<string, Row>;
    if (fields.search_backend === "deepseek_anthropic") {
      const searchId = String(next.search_connection || "");
      if (!searchId)
        throw new Error("当前使用 DeepSeek 搜索桥，请先选择独立的搜索连接。");
      const searchProfile = profiles[searchId];
      const resolved = (fields.resolved_profiles || {}) as Record<string, Row>;
      let official = false;
      try {
        const endpoint = new URL(
          String(searchProfile?.base_url || resolved[searchId]?.base_url || ""),
        );
        official =
          endpoint.protocol === "https:" &&
          endpoint.hostname === "api.deepseek.com";
      } catch {
        official = false;
      }
      if (searchProfile?.provider !== "deepseek" || !official)
        throw new Error("搜索连接必须是官方 DeepSeek 连接。");
    }
    const apiKeys: Record<string, string> = {};
    for (const [id, profile] of Object.entries(profiles)) {
      if (!profile.model && !profile.model_env)
        throw new Error(`${id} 缺少模型 ID。`);
      if (!profile.base_url && !profile.base_url_env)
        throw new Error(`${id} 缺少 API Base URL。`);
      if (!profile.api_key_env && !keyInputs[id]?.trim())
        throw new Error(`${id} 缺少 API Key。`);
      delete profile.thinking_mode;
    }
    for (const [id, value] of Object.entries(keyInputs)) {
      if (!value.trim() || !profiles[id]) continue;
      const alias = `YUKI_WEBUI_KEY_${crypto.randomUUID().replaceAll("-", "").toUpperCase()}`;
      profiles[id].api_key_env = alias;
      apiKeys[alias] = value.trim();
    }
    return { document: next, api_keys: apiKeys };
  }
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
          keyInputs={keyInputs}
          changeKey={(id, value) =>
            setKeyInputs((current) => ({ ...current, [id]: value }))
          }
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
          : fileId === "model_profiles"
            ? "保存成功后立即用于新任务；本轮已开始的请求保持原连接。持久 Work 下次激活会按新连接开新链，已执行工具不会重跑。"
            : "保存后需重启应用才会加载。不会重写历史聊天或重跑已有工作；实际注入内容可在执行轨迹中查看。"}
      </p>
      {fields.writable_directory === false && (
        <p className="error-note">
          配置目录当前不可写。请使用可写的启动文件目录；operator
          声明仍应保留在只读配置目录。
        </p>
      )}
      {saveError && (
        <p className="error-note" role="alert">
          {saveError}
        </p>
      )}
      {props.allowed("save_config_file") &&
        (fileId !== "model_profiles" || fields.profile_schema) && (
          <button
            className="btn-primary"
            disabled={
              fields.writable_directory === false || (hotReload && !hasDocument)
            }
            onClick={() => {
              let modelSave: ReturnType<typeof saveModelDocument> | null = null;
              try {
                modelSave =
                  fileId === "model_profiles" ? saveModelDocument() : null;
                setSaveError("");
              } catch (error) {
                setSaveError(
                  error instanceof Error ? error.message : "配置内容不完整。",
                );
                return;
              }
              props.act({
                method: "save_config_file",
                label: `保存${names[fileId]}`,
                revision: Number(fields.revision),
                payload: {
                  action: "save",
                  resource_id: fileId,
                  spec:
                    modelSave || hotReload
                      ? modelSave || { document }
                      : { content },
                },
                review:
                  fileId === "model_profiles" || hotReload
                    ? modelSave?.document || document
                    : content,
                hint: hotReload
                  ? "核对原文件版本并原子保存。saved_pending_reload 表示已保存，等待原控制器下一轮加载；实际生效请刷新核对。"
                  : fileId === "model_profiles"
                    ? "核对原文件版本并保存；applied 表示模型连接与任务用途已在当前进程生效，新任务使用新连接。"
                    : "将核对刚读取的文件版本并原子保存。持久回执中的 saved_pending_restart 表示已保存，尚需重启加载。",
              });
            }}
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
