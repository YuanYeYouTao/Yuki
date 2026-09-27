import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { command, login, query } from "./api";
import { TraceContent } from "./traces";
import { ActionSheet } from "./actions";
import { Chat } from "./chat";
import { displayTrace } from "./trace-display";

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
  it("starts with newest events and uses internal ids to open the execution trail", async () => {
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
                  direction: "inbound",
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
        expect(body.scope.source_event_id).toBe(142);
        expect(body.scope.conversation_id).toBe("canonical-fixture");
        return ok({ data: { items: [], next_cursor: null }, problem: null });
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
      screen.getByRole("button", { name: "#142 · 查看本轮" }),
    );
    await waitFor(() =>
      expect(
        fetch.mock.calls.some(([url]) =>
          String(url).endsWith("list_execution_trace"),
        ),
      ).toBe(true),
    );
  });
});
