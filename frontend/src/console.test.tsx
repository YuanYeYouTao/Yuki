import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { command, login, query } from "./api";
import { TraceContent } from "./traces";
import { ActionSheet } from "./actions";
import { Chat } from "./chat";
import { displayTrace } from "./trace-display";
import { Autonomy } from "./pages";

it("shows actual semantic dimensions and distinguishes them from admission", () => {
  render(
    <TraceContent
      row={{
        origin: "semantic_observation",
        payload: {
          data: {
            elapsed_seconds: 0.3,
            observation: {
              provider: "typesafe",
              model_revision: "jev-fixture",
              rubric_revision: "v6",
              input_tokens: 21,
              output_tokens: 5,
              invalid_dimensions: [],
              snapshot: {
                sequence: 4,
                scope: { generation: 2 },
                focus: {
                  ref: { event_id: "event:42" },
                  at: 1790000000,
                  author: "fixture",
                  kind: "human",
                  text: "原观察内容",
                },
                context: [],
              },
              answers: {
                interaction_mark: {
                  choice: "invite_yuki",
                  probabilities: { invite_yuki: 0.9, unknown: 0.1 },
                },
              },
            },
          },
        },
      }}
    />,
  );
  expect(screen.getByText("原观察内容")).toBeInTheDocument();
  expect(screen.getByText("interaction_mark")).toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "invite_yuki" })).toBeInTheDocument();
  expect(screen.getByText("90.0%")).toBeInTheDocument();
  expect(screen.getByText("10.0%")).toBeInTheDocument();
  expect(
    screen.getByText(/不等于 Host 已接纳或 Yuki 已发言/),
  ).toBeInTheDocument();
});

it("filters the original decision timeline by conversation and origin without execution", async () => {
  const requests: { method: string; body: Record<string, unknown> }[] = [];
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (url, options) => {
      const method = String(url).split("/").pop()!;
      requests.push({
        method,
        body: JSON.parse(String(options?.body || "{}")),
      });
      return new Response(
        JSON.stringify({
          data:
            method === "read_participation"
              ? { fields: { scopes: [], running: true } }
              : { items: [], next_cursor: null },
          problem: null,
        }),
        { status: 200 },
      );
    });
  const act = vi.fn();
  render(
    <Autonomy
      allowed={(method) => method !== "read_config_file"}
      act={act}
      refresh={0}
      conversation="canonical-fixture"
    />,
  );
  await waitFor(() =>
    expect(
      requests.some((item) => item.method === "list_execution_trace"),
    ).toBe(true),
  );
  expect(
    requests.find((item) => item.method === "list_execution_trace")?.body.scope,
  ).toEqual({
    origin: "semantic_observation",
    conversation_id: "canonical-fixture",
    descending: true,
  });
  const user = userEvent.setup();
  await user.selectOptions(
    screen.getByRole("combobox", { name: "记录类型" }),
    "participation_decision",
  );
  await waitFor(() =>
    expect(
      requests.some(
        (item) =>
          (item.body.scope as Record<string, unknown>)?.origin ===
          "participation_decision",
      ),
    ).toBe(true),
  );
  expect(act).not.toHaveBeenCalled();
  expect(
    fetch.mock.calls.every(([url]) => !String(url).includes("/commands/")),
  ).toBe(true);
});

function ok(value: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}
describe("management transport and evidence", () => {
  it.each([
    {
      kind: "string",
      initial: "old",
      draft: '引用"文本',
      expected: '引用"文本',
      role: "textbox",
    },
    {
      kind: "number",
      initial: 1,
      draft: "2.5",
      expected: 2.5,
      role: "spinbutton",
    },
  ] as const)(
    "submits scalar configuration controls without changing their types",
    async ({ kind, initial, draft, expected, role }) => {
      const fetch = vi
        .spyOn(globalThis, "fetch")
        .mockImplementation(() =>
          ok({ data: { success: true }, problem: null }),
        );
      render(
        <ActionSheet
          intent={{
            method: "set_config",
            label: "修改配置",
            revision: 0,
            edit: "value",
            valueKind: kind,
            payload: { key: "fixture", value: initial },
          }}
          close={() => {}}
          completed={() => {}}
        />,
      );
      const user = userEvent.setup();
      const field = screen.getByRole(role, { name: "新的值" });
      await user.clear(field);
      await user.type(field, draft);
      await user.click(screen.getByRole("button", { name: "提交" }));
      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
      expect(
        JSON.parse(String(fetch.mock.calls[0][1]?.body)).payload.value,
      ).toBe(expected);
    },
  );
  it("treats a non-JSON mutation response as unknown and preserves the original id", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("gateway error", { status: 502 }),
    );
    const requestId = crypto.randomUUID();
    await expect(
      command("disable_person", {
        request_id: requestId,
        expected_revision: 1,
        target: { kind: "person", id: crypto.randomUUID() },
        payload: {},
      }),
    ).rejects.toMatchObject({ code: "transport_unknown", requestId });
  });
  it.each([
    {
      instructions: "system",
      input: "user",
      output: [
        {
          type: "reasoning",
          summary: [{ type: "summary_text", text: "thought" }],
        },
        { type: "message", content: [{ type: "output_text", text: "reply" }] },
      ],
    },
    {
      system: "system",
      messages: [{ role: "user", content: "user" }],
      content: [
        { type: "thinking", thinking: "thought" },
        { type: "text", text: "reply" },
      ],
    },
    {
      systemInstruction: { parts: [{ text: "system" }] },
      contents: [{ role: "user", parts: [{ text: "user" }] }],
      candidates: [
        {
          content: {
            parts: [{ thought: true, text: "thought" }, { text: "reply" }],
          },
        },
      ],
    },
  ])(
    "shows every supported protocol's actual prompt and readable output",
    (body) => {
      const evidence = { data: { body }, redactions: [] };
      const original = JSON.stringify(evidence);
      const result = displayTrace(evidence);
      expect(result.prompts.map((value) => value.role)).toEqual([
        "system",
        "user",
      ]);
      expect(result.reasoning).toEqual(["thought"]);
      expect(result.replies).toEqual(["reply"]);
      expect(JSON.stringify(evidence)).toBe(original);
    },
  );
  it("never replays a lost mutation and retains the original request id", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValue(new TypeError("disconnected"));
    const requestId = crypto.randomUUID();
    await expect(
      command("disable_person", {
        request_id: requestId,
        expected_revision: 7,
        target: { kind: "person", id: crypto.randomUUID() },
        payload: {},
      }),
    ).rejects.toMatchObject({ code: "transport_unknown", requestId });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(
      (fetch.mock.calls[0][1]!.headers as Record<string, string>)[
        "X-Request-ID"
      ],
    ).toBe(requestId);
  });
  it("uses only the HttpOnly session cookie and per-session CSRF; never stores the credential", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((url, init) => {
        if (String(url).endsWith("/login")) return ok({ authenticated: true });
        if (String(url).endsWith("/session"))
          return ok({
            csrf: "fixture-csrf",
            content_access: { chat: false },
            surface: { methods: [] },
          });
        expect((init!.headers as Record<string, string>)["X-Yuki-CSRF"]).toBe(
          "fixture-csrf",
        );
        expect(init?.credentials).toBe("same-origin");
        return ok({ data: { version: "fixture" }, problem: null });
      });
    await login("fixture-only-credential");
    await query("read_system");
    expect(storage).not.toHaveBeenCalled();
    expect(
      fetch.mock.calls.some(([, options]) =>
        JSON.stringify(options?.headers).includes("fixture-only-credential"),
      ),
    ).toBe(false);
  });
  it("renders raw provider prompts and readable reasoning as text, never HTML", () => {
    const { container } = render(
      <TraceContent
        row={{
          payload: {
            data: {
              body: {
                messages: [
                  { role: "system", content: "<img src=x onerror=alert(1)>" },
                ],
              },
            },
            redactions: [],
          },
        }}
      />,
    );
    expect(
      screen.getAllByText("<img src=x onerror=alert(1)>").length,
    ).toBeGreaterThan(0);
    expect(container.querySelector("img")).toBeNull();
    render(
      <TraceContent
        row={{
          payload: {
            data: {
              body: {
                choices: [
                  {
                    message: {
                      content: "reply",
                      reasoning_content: "readable reasoning",
                    },
                  },
                ],
              },
            },
            redactions: [],
          },
        }}
      />,
    );
    expect(screen.getByText("readable reasoning")).toBeInTheDocument();
  });
  it("shows omitted evidence explicitly", () => {
    render(
      <TraceContent row={{ payload: null, payload_status: "omitted_size" }} />,
    );
    expect(
      screen.getByText("正文超过记录上限，仅保留索引。"),
    ).toBeInTheDocument();
  });
  it("keeps a submitted action immutable after a lost response and queries its receipt", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("lost"))
      .mockImplementation(() =>
        ok({ data: { status: "succeeded" }, problem: null }),
      );
    const user = userEvent.setup();
    render(
      <ActionSheet
        intent={{
          method: "mutate_plugin",
          label: "停用插件",
          revision: 12,
          payload: { action: "disable", resource_id: "fixture" },
        }}
        close={() => {}}
        completed={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "提交" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "提交" })).toBeNull();
    const first = JSON.parse(String(fetch.mock.calls[0][1]?.body));
    await user.click(screen.getByRole("button", { name: "查询原请求" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls[1][0]).toBe("/api/control/queries/read_operation");
    expect(JSON.parse(String(fetch.mock.calls[1][1]?.body)).request_id).toBe(
      first.request_id,
    );
  });
  it.each(["inbound", "outbound"])(
    "uses internal %s event ids to find linked execution turns",
    async (direction) => {
      const fetch = vi
        .spyOn(globalThis, "fetch")
        .mockImplementation((url, options) => {
          const body = JSON.parse(String(options?.body));
          if (String(url).endsWith("list_chat_events")) {
            expect(body.history.descending).toBe(true);
            return ok({
              data: {
                items: [
                  {
                    event_id: 142,
                    direction,
                    content: "fixture hello",
                    occurred_at: "2026-09-27T08:00:00Z",
                    origin: "qq",
                    sender_display_name: "fixture",
                  },
                ],
                next_cursor: null,
              },
              problem: null,
            });
          }
          if (String(url).endsWith("list_event_turns")) {
            expect(body.event_id).toBe(142);
            expect(body.direction).toBe(direction);
            expect(body.conversation_id).toBe("canonical-fixture");
          }
          return ok({
            data: { items: [], total: 0, next_cursor: null },
            problem: null,
          });
        });
      render(
        <Chat
          conversation="canonical-fixture"
          content
          refresh={0}
          notebook={<div>notebook</div>}
        />,
      );
      await screen.findByText("fixture hello");
      await userEvent.click(
        screen.getByRole("button", { name: "#142 · 查看事件与执行" }),
      );
      await waitFor(() =>
        expect(
          fetch.mock.calls.some(([url]) =>
            String(url).endsWith("list_event_turns"),
          ),
        ).toBe(true),
      );
    },
  );
});

it("keeps the selected default conversation and notebook section across refresh", async () => {
  const { default: App } = await import("./App");
  window.history.replaceState({}, "", "/#overview");
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    const method = String(url).split("/").pop()!;
    let data: unknown = { items: [], next_cursor: null };
    if (method === "session")
      data = {
        csrf: "fixture",
        content_access: { chat: true },
        surface: {
          protocol_version: "1",
          methods: [
            "read_system",
            "read_health",
            "list_conversations",
            "list_chat_events",
            "read_config_file",
          ].map((name) => ({ name, authorized: true, kind: "query" })),
        },
      };
    if (method === "list_conversations")
      data = {
        items: [{ conversation_id: "original-default", kind: "group" }],
        next_cursor: null,
      };
    if (method === "read_system") data = { version: "fixture" };
    if (method === "read_health") data = { components: [] };
    if (method === "read_conversation_execution")
      data = { state: "idle", active: [], recent: [] };
    if (method === "read_config_file")
      data = {
        fields: {
          file_id: "bot_persona",
          revision: "original",
          content: "fixture",
          valid: true,
        },
      };
    if (method === "read_persona")
      data = { fields: { system_prompt: "fixture persona" } };
    return new Response(JSON.stringify({ data }), { status: 200 });
  });
  render(<App />);
  await waitFor(() =>
    expect(screen.getByLabelText("会话")).toHaveValue("original-default"),
  );
  await userEvent.click(screen.getByRole("tab", { name: "人格" }));
  expect(
    await screen.findByText("当前加载的完整人格提示词"),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "刷新数据" }));
  await waitFor(() =>
    expect(screen.getByLabelText("会话")).toHaveValue("original-default"),
  );
  expect(screen.getByRole("tab", { name: "人格" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

it("browses recent conversations by numbered page and retains the chosen conversation", async () => {
  const { default: App } = await import("./App");
  window.history.replaceState({}, "", "/#overview");
  const visited: number[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    let data: unknown = { items: [], next_cursor: null, total: 0, number: 1 };
    if (method === "session")
      data = {
        csrf: "fixture",
        content_access: { chat: true },
        surface: {
          protocol_version: "1",
          methods: [
            "read_system",
            "read_health",
            "list_conversations",
            "list_chat_events",
          ].map((name) => ({ name, authorized: true, kind: "query" })),
        },
      };
    if (method === "list_conversations") {
      const number = Number(
        (
          JSON.parse(String(options?.body || "{}")) as {
            page: { number: number };
          }
        ).page.number,
      );
      visited.push(number);
      data = {
        items:
          number === 1
            ? [
                { conversation_id: "recent-chat", kind: "space" },
                ...Array.from({ length: 99 }, (_, index) => ({
                  conversation_id: `filler-${index}`,
                  kind: "space",
                })),
              ]
            : [{ conversation_id: "older-chat", kind: "space" }],
        total: 101,
        number,
        next_cursor: null,
      };
    }
    if (method === "read_system") data = { version: "fixture" };
    if (method === "read_health") data = { components: [] };
    if (method === "read_conversation_execution")
      data = { state: "idle", active: [], recent: [] };
    return new Response(JSON.stringify({ data }), { status: 200 });
  });
  render(<App />);
  const selected = await screen.findByRole("combobox", { name: "会话" });
  await waitFor(() => expect(selected).toHaveValue("recent-chat"));
  const pagination = screen.getByLabelText("会话分页");
  expect(
    within(pagination).getByText(/共 2 页 · 共 101 个会话/),
  ).toBeInTheDocument();
  await userEvent.click(
    within(pagination).getByRole("button", { name: "下一页" }),
  );
  await waitFor(() => expect(visited).toContain(2));
  await waitFor(() =>
    expect(
      [...(selected as HTMLSelectElement).options].some(
        (option) => option.value === "older-chat",
      ),
    ).toBe(true),
  );
  await userEvent.selectOptions(selected, "older-chat");
  expect(selected).toHaveValue("older-chat");
  await userEvent.click(
    within(pagination).getByRole("button", { name: "上一页" }),
  );
  await waitFor(() => expect(visited.at(-1)).toBe(1));
  expect(selected).toHaveValue("older-chat");
  const pageInput = within(pagination).getByRole("spinbutton", {
    name: "会话页码",
  });
  await userEvent.clear(pageInput);
  await userEvent.type(pageInput, "2");
  await userEvent.click(
    within(pagination).getByRole("button", { name: "跳转" }),
  );
  await waitFor(() => expect(visited.at(-1)).toBe(2));
});
