import { expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigFile } from "./config-files";
import type { Row } from "./api";

function scene(document: Row) {
  const act = vi.fn();
  vi.spyOn(globalThis, "fetch").mockImplementation(
    async () =>
      new Response(
        JSON.stringify({
          data: {
            fields: {
              file_id: "model_profiles",
              revision: 1,
              valid: true,
              matches_loaded: true,
              document,
              tasks: ["chat_agent"],
              profile_schema: { type: "object", properties: {} },
              search_backend: "tavily",
              saved_api_key_profiles: ["main", "search"],
            },
          },
        }),
        { status: 200 },
      ),
  );
  render(
    <ConfigFile
      fileId="model_profiles"
      props={{
        allowed: () => true,
        act,
        refresh: 0,
        conversation: "",
      }}
    />,
  );
  return act;
}

const main = {
  provider: "openai_compatible",
  protocol: "chat_completions",
  base_url: "https://gateway.example/v1",
  model: "gemini-model",
  api_key_env: "SAVED_KEY",
  search_mode: "external",
  capabilities: ["tools"],
};
const search = {
  ...main,
  provider: "custom-search",
  protocol: "gemini",
  base_url: "https://gateway.example/v1beta",
  search_mode: "bridge",
};

it("keeps an existing connection's protocol and parameters when its provider is renamed", async () => {
  const act = scene({
    schema_version: 3,
    profiles: { main },
    routes: { chat_agent: "main" },
  });
  const provider = await screen.findByLabelText("供应商");
  await userEvent.clear(provider);
  await userEvent.type(provider, "agm");
  await userEvent.click(screen.getByRole("button", { name: "检查并保存" }));
  expect(act.mock.calls[0][0].payload.spec.document.profiles.main).toEqual({
    ...main,
    provider: "agm",
  });
});

it("saves Chat with an explicitly selected Gemini search connection without replacing the main route", async () => {
  const act = scene({
    schema_version: 3,
    profiles: { main, search },
    routes: { chat_agent: "main" },
  });
  await screen.findByLabelText("供应商");
  await userEvent.selectOptions(
    screen.getByLabelText("此连接的联网搜索"),
    "bridge",
  );
  await userEvent.selectOptions(
    screen.getByLabelText("本地 web_search 后端"),
    "model",
  );
  const selector = screen.getByLabelText("搜索连接");
  expect(
    within(selector).getByRole("option", {
      name: /custom-search · Gemini GenerateContent · gemini-model/,
    }),
  ).toBeInTheDocument();
  await userEvent.selectOptions(selector, "search");
  await userEvent.click(screen.getByRole("button", { name: "检查并保存" }));
  expect(act.mock.calls[0][0].payload.spec.document).toMatchObject({
    search_backend: "model",
    search_connection: "search",
    routes: { chat_agent: "main" },
    profiles: { main: { protocol: "chat_completions", search_mode: "bridge" } },
  });
});

it("does not activate a merely preselected legacy search connection", async () => {
  const document = {
    schema_version: 3,
    profiles: { main, search },
    routes: { chat_agent: "main" },
    search_connection: "search",
  };
  const act = scene(document);
  expect(await screen.findByLabelText("本地 web_search 后端")).toHaveValue(
    "inherit",
  );
  await userEvent.click(screen.getByRole("button", { name: "检查并保存" }));
  expect(act.mock.calls[0][0].payload.spec.document).toEqual(document);
});

it("creates an explicit AGM Gemini search connection sharing the saved key reference", async () => {
  const act = scene({
    schema_version: 3,
    profiles: { main: { ...main, provider: "agm" } },
    routes: { chat_agent: "main" },
  });
  await userEvent.click(
    await screen.findByRole("button", {
      name: "从此 AGM 连接创建 Gemini 搜索连接",
    }),
  );
  expect(screen.getByLabelText("接口协议")).toHaveValue("gemini");
  await userEvent.click(screen.getByRole("button", { name: "检查并保存" }));
  const saved = act.mock.calls[0][0].payload.spec;
  expect(saved.api_keys).toEqual({});
  expect(saved.document.profiles.main).toEqual({ ...main, provider: "agm" });
  expect(
    saved.document.profiles[saved.document.search_connection],
  ).toMatchObject({
    provider: "agm",
    protocol: "gemini",
    base_url: "https://gateway.example/v1beta",
    api_key_env: "SAVED_KEY",
    model: "gemini-model",
    search_mode: "bridge",
    wire_options: { reasoning: "gemini" },
  });
  expect(saved.document.search_backend).toBe("model");
  expect(saved.document.routes).toEqual({ chat_agent: "main" });
});

it("keeps a legacy DeepSeek search reference visible without opening unverified Responses search", async () => {
  const legacy = { ...main, provider: "deepseek", protocol: "responses" };
  const act = scene({
    schema_version: 3,
    profiles: { main, legacy },
    routes: { chat_agent: "main" },
    search_connection: "legacy",
  });
  const selector = await screen.findByLabelText("搜索连接");
  expect(selector).toHaveValue("legacy");
  expect(
    within(selector).getByRole("option", { name: /旧部署搜索引用/ }),
  ).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "检查并保存" }));
  expect(
    act.mock.calls[0][0].payload.spec.document.search_backend,
  ).toBeUndefined();
  expect(act.mock.calls[0][0].payload.spec.document.search_connection).toBe(
    "legacy",
  );
});

it("adds AGM as an explicit Gemini preset and keeps the protocol independently editable", async () => {
  const act = scene({
    schema_version: 3,
    profiles: { main },
    routes: { chat_agent: "main" },
  });
  await screen.findByLabelText("供应商");
  await userEvent.selectOptions(screen.getByLabelText("新连接供应商"), "agm");
  await userEvent.click(screen.getByRole("button", { name: "添加模型连接" }));
  expect(screen.getByLabelText("接口协议")).toHaveValue("gemini");
  expect(screen.getByLabelText("此连接的联网搜索")).toHaveValue("bridge");
  await userEvent.type(
    screen.getByLabelText("API Base URL"),
    "https://gateway.example/v1",
  );
  await userEvent.type(screen.getByLabelText("模型 ID"), "gemini-model");
  await userEvent.type(
    screen.getByLabelText("已有密钥的环境变量名"),
    "SAVED_KEY",
  );
  await userEvent.selectOptions(
    screen.getByLabelText("接口协议"),
    "chat_completions",
  );
  await userEvent.click(screen.getByRole("button", { name: "检查并保存" }));
  const saved = act.mock.calls[0][0].payload.spec.document;
  const added = Object.entries(saved.profiles as Record<string, Row>).find(
    ([id]) => id !== "main",
  )![1];
  expect(added).toMatchObject({
    provider: "agm",
    protocol: "chat_completions",
    search_mode: "bridge",
  });
});
