import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigFile } from "./config-files";
import type { Intent } from "./actions";

const props = {
  allowed: () => true,
  act: vi.fn<(intent: Intent) => void>(),
  refresh: 0,
  conversation: "",
};
function file(fields: unknown) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ data: { fields }, problem: null }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

it("edits persona as exact text and carries the original file revision to review", async () => {
  const fetch = file({
    file_id: "system_prompt",
    revision: 12345,
    exists: true,
    valid: true,
    matches_loaded: true,
    content: "原文",
  });
  const act = vi.fn();
  render(<ConfigFile fileId="system_prompt" props={{ ...props, act }} />);
  const user = userEvent.setup();
  const text = await screen.findByRole("textbox", {
    name: "主人格提示词（System Prompt）",
  });
  await user.clear(text);
  await user.type(text, '第一行\n保留 "引号" 和 <标签>');
  await user.click(screen.getByRole("button", { name: "检查并保存" }));
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(act).toHaveBeenCalledWith(
    expect.objectContaining({
      method: "save_config_file",
      revision: 12345,
      payload: {
        action: "save",
        resource_id: "system_prompt",
        spec: { content: '第一行\n保留 "引号" 和 <标签>' },
      },
    }),
  );
  expect(screen.getByText(/保存后需重启应用才会加载/)).toBeInTheDocument();
});

it("edits schema fields and routes without discarding server environment references", async () => {
  const fetch = file({
    file_id: "model_profiles",
    revision: 8765,
    valid: true,
    matches_loaded: false,
    profile_schema: {
      properties: {
        provider: { type: "string" },
        model: { type: "string" },
        timeout_seconds: { type: "number" },
        protocol: { $ref: "#/$defs/Protocol" },
        headers: { type: "object" },
      },
      $defs: {
        Protocol: { enum: ["chat_completions", "responses"], type: "string" },
      },
    },
    tasks: ["chat_agent", "memory_extraction"],
    document: {
      schema_version: 3,
      profiles: {
        main: {
          provider: "openai",
          model: "m1",
          timeout_seconds: 60,
          protocol: "chat_completions",
          model_env: "LLM_MODEL",
          base_url_env: "LLM_BASE_URL",
          api_key_env: "LLM_API_KEY",
        },
        other: {
          provider: "openai",
          model: "m2",
          base_url: "https://api.example.test/v1",
          api_key_env: "OTHER_KEY",
        },
      },
      routes: { chat_agent: "main", memory_extraction: "main" },
    },
  });
  const act = vi.fn();
  render(<ConfigFile fileId="model_profiles" props={{ ...props, act }} />);
  const user = userEvent.setup();
  await user.click(await screen.findByText("高级参数与能力声明"));
  const timeout = await screen.findByRole("spinbutton", { name: "超时（秒）" });
  await user.clear(timeout);
  await user.type(timeout, "90");
  await user.selectOptions(
    screen.getByRole("combobox", { name: "主对话使用的模型" }),
    "other",
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "接口协议" }),
    "responses",
  );
  await user.click(screen.getByRole("button", { name: "检查并保存" }));
  const intent = act.mock.calls[0][0] as Intent;
  const document = (
    intent.payload.spec as {
      document: {
        profiles: Record<string, unknown>;
        routes: Record<string, string>;
      };
    }
  ).document;
  expect(document.profiles.main).toEqual({
    provider: "openai",
    model: "m1",
    timeout_seconds: 90,
    protocol: "responses",
    model_env: "LLM_MODEL",
    base_url_env: "LLM_BASE_URL",
    api_key_env: "LLM_API_KEY",
  });
  expect(document.routes).toEqual({
    chat_agent: "other",
    memory_extraction: "main",
  });
  expect(intent.revision).toBe(8765);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("headers")).not.toBeInTheDocument();
});

it("chooses a concrete model connection for a task", async () => {
  file({
    file_id: "model_profiles",
    revision: 4,
    valid: true,
    profile_schema: {
      properties: { provider: { type: "string" }, model: { type: "string" } },
    },
    tasks: ["chat_agent"],
    document: {
      schema_version: 3,
      profiles: {
        primary: {
          provider: "openai",
          model: "one",
          base_url: "https://api.example.test/v1",
          api_key_env: "OPENAI_KEY",
        },
        backup: {
          provider: "deepseek",
          model: "two",
          base_url: "https://api.deepseek.com",
          api_key_env: "DEEPSEEK_KEY",
        },
      },
      routes: { chat_agent: "primary" },
    },
  });
  const act = vi.fn();
  render(<ConfigFile fileId="model_profiles" props={{ ...props, act }} />);
  const user = userEvent.setup();
  await user.selectOptions(
    await screen.findByRole("combobox", { name: "主对话使用的模型" }),
    "backup",
  );
  expect(
    screen.getByRole("combobox", { name: "主对话使用的模型" }),
  ).toHaveValue("backup");
  await user.click(screen.getByRole("button", { name: "检查并保存" }));
  const intent = act.mock.calls[0][0] as Intent;
  expect(
    (intent.payload.spec as { document: { routes: Record<string, string> } })
      .document.routes.chat_agent,
  ).toBe("backup");
});

it("accepts an API key in the connection form without showing it in the review", async () => {
  file({
    file_id: "model_profiles",
    revision: 9,
    valid: true,
    profile_schema: { properties: {} },
    tasks: ["chat_agent"],
    document: {
      schema_version: 3,
      profiles: {
        main: {
          provider: "openai",
          protocol: "responses",
          base_url: "https://api.openai.com/v1",
          model: "test",
          api_key_env: "LLM_API_KEY",
        },
      },
      routes: { chat_agent: "main" },
    },
  });
  const act = vi.fn();
  render(<ConfigFile fileId="model_profiles" props={{ ...props, act }} />);
  await userEvent.type(
    await screen.findByLabelText(/^API Key/),
    "private-test-key",
  );
  await userEvent.click(screen.getByRole("button", { name: "检查并保存" }));
  const intent = act.mock.calls[0][0] as Intent;
  const spec = intent.payload.spec as {
    document: { profiles: Record<string, { api_key_env: string }> };
    api_keys: Record<string, string>;
  };
  const alias = spec.document.profiles.main.api_key_env;
  expect(alias).toMatch(/^YUKI_WEBUI_KEY_[A-F0-9]{32}$/);
  expect(spec.api_keys[alias]).toBe("private-test-key");
  expect(JSON.stringify(intent.review)).not.toContain("private-test-key");
});

it("starts a new configuration with an opaque connection ID and all task routes", async () => {
  file({
    file_id: "model_profiles",
    revision: 0,
    valid: true,
    profile_schema: { properties: {} },
    tasks: ["chat_agent", "memory_extraction"],
    document: { schema_version: 3, profiles: {}, routes: {} },
  });
  render(<ConfigFile fileId="model_profiles" props={props} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "添加模型连接" }),
  );
  const routes = [
    screen.getByRole("combobox", { name: "主对话使用的模型" }),
    screen.getByRole("combobox", { name: "记忆提取使用的模型" }),
  ];
  const connectionId = (routes[0] as HTMLSelectElement).value;
  expect(connectionId).toMatch(/^connection_[a-f0-9]{12}$/);
  expect(routes[1]).toHaveValue(connectionId);
  expect(screen.getByText(`内部连接编号：${connectionId}`)).toBeInTheDocument();
});

it("offers a Gemini 3.8 Flash native connection with its verified input capabilities", async () => {
  file({
    file_id: "model_profiles",
    revision: 0,
    valid: true,
    profile_schema: {
      properties: {
        reasoning_effort: { $ref: "#/$defs/ReasoningEffort" },
      },
      $defs: {
        ReasoningEffort: {
          enum: ["none", "minimal", "low", "medium", "high", "max"],
          type: "string",
        },
      },
    },
    tasks: ["chat_agent"],
    document: { schema_version: 3, profiles: {}, routes: {} },
  });
  render(<ConfigFile fileId="model_profiles" props={props} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "添加模型连接" }),
  );
  await userEvent.selectOptions(
    screen.getByRole("combobox", { name: "供应商" }),
    "gemini",
  );
  expect(screen.getByRole("combobox", { name: "接口协议" })).toHaveValue(
    "gemini",
  );
  expect(screen.getByRole("textbox", { name: "模型 ID" })).toHaveValue(
    "gemini-3.8-flash",
  );
  expect(screen.getByRole("textbox", { name: "API Base URL" })).toHaveValue(
    "https://generativelanguage.googleapis.com/v1beta",
  );
  await userEvent.click(screen.getByText("高级参数与能力声明"));
  expect(screen.getByRole("combobox", { name: "思考强度" })).toHaveValue(
    "medium",
  );
  expect(screen.queryByRole("option", { name: "max" })).not.toBeInTheDocument();
});

it("does not request file contents without the separate content grant", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  render(
    <ConfigFile
      fileId="bot_persona"
      props={{ ...props, allowed: () => false }}
    />,
  );
  await waitFor(() =>
    expect(screen.getByText("需要文件配置正文读取权限。")).toBeInTheDocument(),
  );
  expect(fetch).not.toHaveBeenCalled();
});

it("disables saving a readonly startup directory", async () => {
  file({
    file_id: "bot_persona",
    revision: 1,
    valid: true,
    content: "人格",
    writable_directory: false,
  });
  const act = vi.fn();
  render(<ConfigFile fileId="bot_persona" props={{ ...props, act }} />);
  expect(
    await screen.findByRole("button", { name: "检查并保存" }),
  ).toBeDisabled();
  expect(screen.getByText(/配置目录当前不可写/)).toBeInTheDocument();
  expect(act).not.toHaveBeenCalled();
});

it("reviews original hot parameter revision and preserves unrelated schema fields", async () => {
  const fetch = file({
    file_id: "autonomous_model",
    revision: 73,
    valid: true,
    apply_mode: "hot_reload",
    matches_loaded: false,
    parameter_schema: {
      properties: {
        intrinsic_interval_seconds: {
          type: "number",
          minimum: 1,
          maximum: 86400,
          default: 600,
        },
        pressure_bias: {
          type: "number",
          minimum: -20,
          maximum: 20,
          default: 0.2,
        },
      },
    },
    document: { intrinsic_interval_seconds: 600, pressure_bias: 0.2 },
    loaded_document: { intrinsic_interval_seconds: 600, pressure_bias: 0.2 },
  });
  const act = vi.fn();
  render(<ConfigFile fileId="autonomous_model" props={{ ...props, act }} />);
  const user = userEvent.setup();
  const interval = await screen.findByRole("spinbutton", {
    name: "无来源基率分母（秒）",
  });
  await user.clear(interval);
  await user.type(interval, "120");
  await user.click(screen.getByRole("button", { name: "检查并保存" }));
  expect(act).toHaveBeenCalledWith(
    expect.objectContaining({
      revision: 73,
      payload: {
        action: "save",
        resource_id: "autonomous_model",
        spec: {
          document: { intrinsic_interval_seconds: 120, pressure_bias: 0.2 },
        },
      },
      hint: expect.stringContaining("saved_pending_reload"),
    }),
  );
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(
    screen.queryByText(/保存后需重启应用才会加载/),
  ).not.toBeInTheDocument();
});

it("requires explicit default draft to repair invalid hot parameters", async () => {
  file({
    file_id: "autonomous_model",
    revision: 74,
    valid: false,
    apply_mode: "hot_reload",
    parameter_schema: {
      properties: { pressure_bias: { type: "number", default: 0.2 } },
    },
    defaults: { pressure_bias: 0.2 },
    loaded_document: { pressure_bias: 0.4 },
  });
  const act = vi.fn();
  render(<ConfigFile fileId="autonomous_model" props={{ ...props, act }} />);
  expect(
    await screen.findByRole("button", { name: "检查并保存" }),
  ).toBeDisabled();
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "使用默认参数建立草稿" }),
  );
  expect(screen.getByRole("button", { name: "检查并保存" })).toBeEnabled();
  expect(act).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "检查并保存" }));
  expect(act.mock.calls[0][0].payload.spec).toEqual({
    document: { pressure_bias: 0.2 },
  });
});
