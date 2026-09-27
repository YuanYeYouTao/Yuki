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
    name: "System Prompt 模板",
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
        },
        other: { provider: "openai", model: "m2" },
      },
      routes: { chat_agent: "main", memory_extraction: "main" },
    },
  });
  const act = vi.fn();
  render(<ConfigFile fileId="model_profiles" props={{ ...props, act }} />);
  const user = userEvent.setup();
  const timeout = await screen.findByRole("spinbutton", { name: "超时（秒）" });
  await user.clear(timeout);
  await user.type(timeout, "90");
  await user.selectOptions(
    screen.getByRole("combobox", { name: "chat_agent" }),
    "other",
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "协议" }),
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
  });
  expect(document.routes).toEqual({
    chat_agent: "other",
    memory_extraction: "main",
  });
  expect(intent.revision).toBe(8765);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("headers")).not.toBeInTheDocument();
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
