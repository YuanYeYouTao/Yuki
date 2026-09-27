import { expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PluginDetails } from "./plugin-details";
import type { Intent } from "./actions";

const schema = {
  type: "object",
  properties: {
    repositories: { type: "array", items: { $ref: "#/$defs/Repository" } },
    coalesce: { type: "boolean", default: true },
  },
  $defs: {
    Repository: {
      type: "object",
      required: ["repository", "targets"],
      properties: {
        repository: { type: "string" },
        targets: {
          type: "array",
          minItems: 1,
          items: { $ref: "#/$defs/Target" },
        },
      },
    },
    Target: {
      type: "object",
      required: ["target_type", "target_id"],
      properties: {
        target_type: { type: "string", enum: ["group", "private"] },
        target_id: { type: "string" },
        ask_agent: { type: "boolean", default: true },
      },
    },
  },
};
const props = {
  allowed: () => true,
  act: vi.fn<(intent: Intent) => void>(),
  refresh: 0,
  conversation: "",
};

function responses() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    const args = JSON.parse(String(init?.body));
    const method = String(url).split("/").pop();
    const data =
      method === "read_plugin_configuration"
        ? {
            fields: {
              plugin_id: "github-monitor",
              revision: args.scope_type === "global" ? 7654 : 9876,
              scope_type: args.scope_type,
              owner_id: args.owner_id,
              valid: true,
              schema,
              values: { repositories: [], coalesce: true },
            },
          }
        : method === "read_plugin_observation"
          ? {
              fields: {
                config: { poll_interval_seconds: 60, coalesce: true },
                repositories: [
                  {
                    repository: "Owner/Repo",
                    pending_count: 3,
                    queue_available: true,
                    consecutive_failures: 2,
                    error_category: "github_cursor_payload_conflict",
                    inflight: { deliveries: [{ source_event_id: 6912 }] },
                  },
                ],
                next_cursor: null,
              },
            }
          : {
              items: [
                {
                  resource_id: "1",
                  fields: {
                    outbox_id: 1,
                    source_event_id: 6912,
                    status: "failed",
                    revision: 123,
                    can_retry: true,
                  },
                },
                {
                  resource_id: "2",
                  fields: {
                    outbox_id: 2,
                    source_event_id: 6913,
                    status: "uncertain",
                    revision: 124,
                    can_retry: false,
                  },
                },
              ],
              next_cursor: null,
            };
    return new Response(JSON.stringify({ data, problem: null }), {
      status: 200,
    });
  });
}

it("edits nested repository targets through original scope/version and reviews without auto-saving", async () => {
  const fetch = responses(),
    act = vi.fn(),
    user = userEvent.setup();
  render(<PluginDetails pluginId="github-monitor" props={{ ...props, act }} />);
  await screen.findByRole("button", { name: "检查并保存" });
  await user.click(screen.getByRole("button", { name: "添加项" }));
  await user.type(
    screen.getByRole("textbox", { name: "仓库（owner/name） *" }),
    "Owner/Repo",
  );
  const targetSet = screen.getByRole("group", { name: "通知目标 *" });
  await user.click(within(targetSet).getByRole("button", { name: "添加项" }));
  await user.selectOptions(
    screen.getByRole("combobox", { name: "目标类型 *" }),
    "private",
  );
  await user.type(
    screen.getByRole("textbox", { name: "平台投递目标 ID *" }),
    "1001",
  );
  await user.click(screen.getByRole("button", { name: "检查并保存" }));
  const intent = act.mock.calls[0][0] as Intent;
  expect(intent.method).toBe("configure_plugin");
  expect(intent.revision).toBe(7654);
  expect(intent.payload.spec).toEqual({
    scope_type: "global",
    owner_id: null,
    values: {
      coalesce: true,
      repositories: [
        {
          repository: "Owner/Repo",
          targets: [
            { target_type: "private", target_id: "1001", ask_agent: true },
          ],
        },
      ],
    },
  });
  expect(
    fetch.mock.calls.every(([url]) => String(url).includes("/queries/")),
  ).toBe(true);
});

it("switches canonical owner explicitly and resets the old scope draft", async () => {
  const fetch = responses(),
    act = vi.fn(),
    user = userEvent.setup();
  render(<PluginDetails pluginId="github-monitor" props={{ ...props, act }} />);
  await screen.findByRole("button", { name: "检查并保存" });
  await user.selectOptions(
    screen.getByRole("combobox", { name: "配置范围" }),
    "group",
  );
  await user.type(
    screen.getByRole("textbox", { name: "内部 Space ID" }),
    "11111111-1111-4111-8111-111111111111",
  );
  expect(
    screen.queryByRole("button", { name: "检查并保存" }),
  ).not.toBeInTheDocument();
  expect(
    fetch.mock.calls.filter(([url]) =>
      String(url).endsWith("read_plugin_configuration"),
    ),
  ).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "读取此范围" }));
  await waitFor(() =>
    expect(screen.getByText(/版本 9876/)).toBeInTheDocument(),
  );
  await user.click(screen.getByRole("button", { name: "检查并保存" }));
  expect(act.mock.calls[0][0].payload.spec).toEqual(
    expect.objectContaining({
      scope_type: "group",
      owner_id: "11111111-1111-4111-8111-111111111111",
    }),
  );
});

it("displays queue diagnostics and retries only the original proven-unsent outbox", async () => {
  responses();
  const act = vi.fn(),
    user = userEvent.setup();
  render(<PluginDetails pluginId="github-monitor" props={{ ...props, act }} />);
  await screen.findByText("github_cursor_payload_conflict");
  await user.click(screen.getByRole("button", { name: "游标与投递详情" }));
  expect(screen.getByText(/"source_event_id": 6912/)).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "重试" })).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "重试" }));
  expect(act).toHaveBeenCalledWith(
    expect.objectContaining({
      method: "mutate_plugin",
      revision: 123,
      payload: { resource_id: "1", action: "retry" },
    }),
  );
});

it("does not request plugin content or render write controls without their grants", async () => {
  const fetch = responses();
  render(
    <PluginDetails
      pluginId="github-monitor"
      props={{ ...props, allowed: () => false }}
    />,
  );
  expect(fetch).not.toHaveBeenCalled();
  expect(
    screen.queryByRole("button", { name: "检查并保存" }),
  ).not.toBeInTheDocument();
});

it("keeps another plugin's arbitrary observation as JSON without assuming a repository array", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(
    async (url) =>
      new Response(
        JSON.stringify({
          data: String(url).endsWith("read_plugin_observation")
            ? { fields: { repositories: { custom: "value" } } }
            : {
                fields: {
                  plugin_id: "other",
                  values: {},
                  schema: {},
                  revision: 1,
                },
              },
          problem: null,
        }),
        { status: 200 },
      ),
  );
  render(
    <PluginDetails
      pluginId="other"
      props={{ ...props, allowed: (method) => method !== "list_plugin_outbox" }}
    />,
  );
  expect(await screen.findByText(/"custom": "value"/)).toBeInTheDocument();
});
