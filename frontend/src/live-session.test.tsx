import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LiveSession } from "./live-session";
import { EventTurns } from "./event-turns";
import { Chat } from "./chat";

function answer(data: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify({ data, problem: null }), { status: 200 }),
  );
}

it("shows current conversation activity and opens the original turn", async () => {
  const calls: { method: string; body: Record<string, unknown> }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    const body = JSON.parse(String(options?.body || "{}"));
    calls.push({ method, body });
    if (method === "read_conversation_execution")
      return answer({
        fields: {
          conversation_id: "conversation-a",
          observed_at: "2026-09-28T08:00:00Z",
          state: "active",
          active: [
            {
              turn_id: "turn-one",
              original_conversation_id: "conversation-a",
              origin: "user_message",
              started_at: "2026-09-28T07:59:59Z",
              last_step_at: "2026-09-28T08:00:00Z",
              latest_kind: "tool_batch_start",
              status: "active",
              steps: [
                {
                  id: 12,
                  kind: "tool_batch_start",
                  created_at: "2026-09-28T08:00:00Z",
                  payload_status: "recorded",
                },
              ],
            },
          ],
          recent: [],
        },
      });
    return answer({ items: [], next_cursor: null });
  });
  render(<LiveSession conversation="conversation-a" refresh={0} />);
  expect(await screen.findByText("正在执行 1 个轮次")).toBeInTheDocument();
  expect(screen.getAllByText("开始工具批次").length).toBeGreaterThan(0);
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "查看原始轨迹表" }));
  await waitFor(() =>
    expect(calls.some((call) => call.method === "list_execution_trace")).toBe(
      true,
    ),
  );
  expect(
    calls.find((call) => call.method === "list_execution_trace")?.body.scope,
  ).toMatchObject({
    turn_id: "turn-one",
    conversation_id: "conversation-a",
  });
  expect(
    screen.queryByRole("textbox", { name: "轮次编号" }),
  ).not.toBeInTheDocument();
});

it("shows linked received and sent messages with content only when granted", async () => {
  const calls: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => {
    const body = JSON.parse(String(options?.body));
    calls.push(body);
    return answer({
      fields: {
        conversation_id: "conversation-a",
        observed_at: "2026-09-28T08:00:00Z",
        state: "active",
        recent: [],
        active: [
          {
            turn_id: "turn-a",
            original_conversation_id: "conversation-a",
            origin: "user_message",
            started_at: "2026-09-28T07:59:00Z",
            latest_kind: "provider_start",
            status: "active",
            steps: [],
            messages: [
              {
                event_id: 1,
                direction: "received",
                sender_display_name: "阿远",
                conversation_id: "conversation-a",
                occurred_at: "2026-09-28T07:59:00Z",
                content: "请查一下",
              },
              {
                event_id: 2,
                direction: "sent",
                sender_display_name: "Yuki",
                delivery_status: "confirmed",
                conversation_id: "conversation-b",
                occurred_at: "2026-09-28T08:00:00Z",
                content: "我去查一下",
              },
            ],
          },
        ],
      },
    });
  });
  render(<LiveSession conversation="conversation-a" refresh={0} content />);
  expect(await screen.findByText("请查一下")).toBeInTheDocument();
  expect(screen.getByText("我去查一下")).toBeInTheDocument();
  expect(screen.getByText(/收到 · 阿远/)).toBeInTheDocument();
  expect(screen.getByText(/发出 · Yuki/)).toBeInTheDocument();
  expect(screen.getByText(/已由投递回执确认/)).toBeInTheDocument();
  expect(screen.getByText(/发送到其他会话/)).toBeInTheDocument();
  expect(calls[0].include_content).toBe(true);
});

it("loads earlier turn steps and messages by the internal turn and step IDs", async () => {
  const calls: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => {
    const body = JSON.parse(String(options?.body));
    calls.push(body);
    if (!body.before_step_id)
      return answer({
        fields: {
          conversation_id: "conversation-a",
          observed_at: "2026-09-28T08:00:00Z",
          state: "idle",
          active: [],
          recent: [
            {
              turn_id: "turn-a",
              original_conversation_id: "conversation-a",
              started_at: "2026-09-28T07:59:00Z",
              latest_kind: "turn_end",
              status: "completed",
              steps: [
                {
                  id: 42,
                  kind: "turn_end",
                  created_at: "2026-09-28T08:00:00Z",
                  payload_status: "recorded",
                },
              ],
              steps_truncated: true,
              messages_truncated: true,
              messages: [],
            },
          ],
        },
      });
    return answer({
      fields: {
        turn_id: "turn-a",
        original_conversation_id: "conversation-a",
        steps_truncated: false,
        steps: [
          {
            id: 10,
            kind: "tool_start",
            created_at: "2026-09-28T07:59:01Z",
            payload_status: "recorded",
          },
        ],
        messages: [
          {
            event_id: 7,
            direction: "received",
            conversation_id: "conversation-a",
            occurred_at: "2026-09-28T07:59:00Z",
            content: null,
          },
        ],
      },
    });
  });
  render(<LiveSession conversation="conversation-a" refresh={0} />);
  await userEvent
    .setup()
    .click(await screen.findByRole("button", { name: "查看最近一次轮次" }));
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "加载更早的状态和收发消息" }));
  expect(await screen.findByText("内部事件 #7")).toBeInTheDocument();
  expect(screen.getByText("消息正文未授权读取")).toBeInTheDocument();
  expect(calls[1]).toMatchObject({
    conversation_id: "conversation-a",
    turn_id: "turn-a",
    before_step_id: 42,
    include_content: false,
  });
  expect(
    screen.queryByRole("button", { name: "加载更早的状态和收发消息" }),
  ).not.toBeInTheDocument();
});

it("opens a recorded tool call with its actual arguments and paired result", async () => {
  const calls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    calls.push(method);
    if (method === "read_conversation_execution")
      return answer({
        fields: {
          conversation_id: "conversation-a",
          observed_at: "2026-09-28T08:00:00Z",
          state: "idle",
          active: [],
          recent: [
            {
              turn_id: "turn-one",
              original_conversation_id: "conversation-a",
              started_at: "2026-09-28T07:59:00Z",
              latest_kind: "turn_end",
              status: "completed",
              steps: [
                {
                  id: 12,
                  operation_id: "tool-op",
                  kind: "tool_start",
                  created_at: "2026-09-28T07:59:10Z",
                  payload_status: "recorded",
                },
                {
                  id: 13,
                  operation_id: "tool-op",
                  kind: "tool_end",
                  created_at: "2026-09-28T07:59:11Z",
                  payload_status: "recorded",
                },
              ],
            },
          ],
        },
      });
    const id = JSON.parse(String(options?.body)).entry_id;
    return answer(
      id === 12
        ? {
            kind: "tool_start",
            payload_status: "recorded",
            payload: {
              data: {
                call: {
                  function: {
                    name: "search_web",
                    arguments: '{"query":"weather"}',
                  },
                },
              },
            },
          }
        : {
            kind: "tool_end",
            payload_status: "recorded",
            payload: { data: { result: '{"ok":true,"count":2}' } },
          },
    );
  });
  render(
    <LiveSession conversation="conversation-a" refresh={0} traceContent />,
  );
  await userEvent
    .setup()
    .click(await screen.findByRole("button", { name: "查看最近一次轮次" }));
  expect(screen.getAllByText(/工具返回结果 · 1 秒/).length).toBeGreaterThan(0);
  expect(calls).not.toContain("read_execution_trace");
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "查看记录 #12 的具体操作" }));
  expect(await screen.findByText("search_web")).toBeInTheDocument();
  expect(screen.getByText("实际参数")).toBeInTheDocument();
  expect(screen.getAllByText("工具返回结果").length).toBeGreaterThan(0);
  expect(screen.getByText(/"weather"/)).toBeInTheDocument();
  expect(screen.getByText(/"count": 2/)).toBeInTheDocument();
  expect(
    calls.filter((method) => method === "read_execution_trace"),
  ).toHaveLength(2);
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "查看本轮具体操作（2 条）" }));
  expect(
    await screen.findByRole("group", { name: "本轮具体操作" }),
  ).toHaveTextContent("search_web");
  expect(screen.getByRole("group", { name: "本轮具体操作" })).toHaveTextContent(
    '"count": 2',
  );
});

it("shows actual model and Provider request steps in the turn operation summary", async () => {
  const detailReads: number[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    if (method === "read_conversation_execution")
      return answer({
        fields: {
          conversation_id: "conversation-a",
          observed_at: "2026-09-28T08:00:00Z",
          state: "active",
          active: [
            {
              turn_id: "turn-one",
              original_conversation_id: "conversation-a",
              started_at: "2026-09-28T07:59:59Z",
              latest_kind: "provider_response",
              status: "active",
              steps: [
                { id: 10, kind: "model_start", payload_status: "recorded" },
                { id: 11, kind: "provider_start", payload_status: "recorded" },
                {
                  id: 12,
                  kind: "provider_response",
                  payload_status: "recorded",
                },
              ],
            },
          ],
          recent: [],
        },
      });
    const id = Number(JSON.parse(String(options?.body)).entry_id);
    detailReads.push(id);
    const payload =
      id === 10
        ? {
            data: {
              task: "chat_agent",
              request: { messages: [{ role: "user" }], tools: [] },
            },
          }
        : id === 11
          ? {
              data: {
                protocol: "gemini",
                dispatch: "sent",
                body: { model: "gemini-flash" },
              },
            }
          : {
              data: {
                http_status: 200,
                body: { model: "gemini-flash", usage: {} },
              },
            };
    return answer({
      kind:
        id === 10
          ? "model_start"
          : id === 11
            ? "provider_start"
            : "provider_response",
      payload_status: "recorded",
      payload,
    });
  });
  render(
    <LiveSession conversation="conversation-a" refresh={0} traceContent />,
  );
  await userEvent
    .setup()
    .click(
      await screen.findByRole("button", { name: "查看本轮具体操作（3 条）" }),
    );
  await waitFor(() => expect(detailReads).toEqual([10, 11, 12]));
  expect(screen.getByRole("group", { name: "本轮具体操作" })).toHaveTextContent(
    "查看实际模型输入与请求参数",
  );
  expect(screen.getByRole("group", { name: "本轮具体操作" })).toHaveTextContent(
    "查看实际发给 Provider 的请求",
  );
  expect(screen.getByRole("group", { name: "本轮具体操作" })).toHaveTextContent(
    "HTTP 200",
  );
});

it("refreshes an expanded active turn when a new step arrives", async () => {
  let step = 12;
  let traceReads = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    const method = String(url).split("/").pop()!;
    if (method === "read_conversation_execution")
      return answer({
        fields: {
          conversation_id: "conversation-a",
          observed_at: "2026-09-28T08:00:00Z",
          state: "active",
          active: [
            {
              turn_id: "turn-one",
              original_conversation_id: "conversation-a",
              origin: "user_message",
              started_at: "2026-09-28T07:59:59Z",
              last_step_at: "2026-09-28T08:00:00Z",
              latest_kind: "tool_batch_start",
              status: "active",
              steps: [
                {
                  id: step,
                  kind: "tool_batch_start",
                  created_at: "2026-09-28T08:00:00Z",
                  payload_status: "recorded",
                },
              ],
            },
          ],
          recent: [],
        },
      });
    if (method === "list_execution_trace") traceReads++;
    return answer({ items: [], total: 0, page: { number: 1, limit: 30 } });
  });
  const view = render(
    <LiveSession conversation="conversation-a" refresh={0} />,
  );
  await screen.findByText("正在执行 1 个轮次");
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "查看原始轨迹表" }));
  await waitFor(() => expect(traceReads).toBe(1));
  step = 13;
  view.rerender(<LiveSession conversation="conversation-a" refresh={1} />);
  await waitFor(() => expect(traceReads).toBe(2));
});

it("keeps the latest completed turn collapsed while idle", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(() =>
    answer({
      fields: {
        conversation_id: "conversation-a",
        observed_at: "2026-09-28T08:00:00Z",
        state: "idle",
        active: [],
        recent: [
          {
            turn_id: "turn-last",
            original_conversation_id: "conversation-a",
            origin: "user_message",
            started_at: "2026-09-28T07:00:00Z",
            last_step_at: "2026-09-28T07:01:00Z",
            latest_kind: "turn_end",
            status: "completed",
            steps: [],
          },
        ],
      },
    }),
  );
  render(<LiveSession conversation="conversation-a" refresh={0} />);
  expect(await screen.findByText("当前没有执行")).toBeInTheDocument();
  expect(screen.queryByText("已完成")).not.toBeInTheDocument();
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "查看最近一次轮次" }));
  expect(screen.getByText("已完成")).toBeInTheDocument();
});

it("requires an exact event to turn link and lets the reader choose among turns", async () => {
  const calls: { method: string; body: Record<string, unknown> }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    const body = JSON.parse(String(options?.body || "{}"));
    calls.push({ method, body });
    if (method === "list_event_turns")
      return answer({
        items: [
          {
            resource_id: "turn-a",
            fields: {
              turn_id: "turn-a",
              origin: "user_message",
              created_at: "2026-09-28T07:00:00Z",
              original_conversation_id: "conversation-a",
              trace_status: "recorded",
            },
          },
          {
            resource_id: "turn-b",
            fields: {
              turn_id: "turn-b",
              origin: "automation",
              created_at: "2026-09-28T08:00:00Z",
              original_conversation_id: "conversation-b",
              trace_status: "recorded",
            },
          },
        ],
        total: 2,
        next_cursor: null,
      });
    return answer({ items: [], next_cursor: null });
  });
  render(
    <EventTurns
      conversation="conversation-a"
      eventId={42}
      direction="outbound"
      refresh={0}
    />,
  );
  expect(await screen.findByText(/多个轮次/)).toBeInTheDocument();
  expect(screen.queryByText(/本轮执行/)).not.toBeInTheDocument();
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: /automation/ }));
  await waitFor(() =>
    expect(calls.some((call) => call.method === "list_execution_trace")).toBe(
      true,
    ),
  );
  expect(
    calls.find((call) => call.method === "list_execution_trace")?.body.scope,
  ).toMatchObject({
    turn_id: "turn-b",
    conversation_id: "conversation-b",
  });
  expect(calls[0].body).toMatchObject({
    conversation_id: "conversation-a",
    event_id: 42,
    direction: "outbound",
  });
});

it("does not claim a turn when an event has no retained link", async () => {
  const methods: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
    methods.push(String(url).split("/").pop()!);
    return answer({ items: [], next_cursor: null });
  });
  render(
    <EventTurns
      conversation="conversation-a"
      eventId={43}
      direction="inbound"
      refresh={0}
    />,
  );
  expect(await screen.findByText(/没有可查看的执行轮次/)).toBeInTheDocument();
  expect(methods).toEqual(["list_event_turns"]);
});

it("forgets prior event turn choices when the same event is refreshed", async () => {
  let refreshed = false;
  vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
    if (!String(url).endsWith("list_event_turns"))
      return answer({ items: [], total: 0, next_cursor: null });
    const turns = refreshed ? ["turn-new"] : ["turn-old", "turn-other"];
    return answer({
      items: turns.map((turn_id) => ({
        resource_id: turn_id,
        fields: {
          turn_id,
          origin: "user_message",
          created_at: "2026-09-28T08:00:00Z",
          original_conversation_id: "conversation-a",
          trace_status: "completed",
        },
      })),
      total: turns.length,
      next_cursor: null,
    });
  });
  const view = render(
    <EventTurns
      conversation="conversation-a"
      eventId={44}
      direction="inbound"
      refresh={0}
    />,
  );
  await screen.findAllByRole("button", { name: /聊天消息/ });
  await userEvent
    .setup()
    .click(screen.getAllByRole("button", { name: /聊天消息/ })[0]);
  expect(
    screen.getAllByText("turn-old", { exact: false }).length,
  ).toBeGreaterThan(0);
  refreshed = true;
  view.rerender(
    <EventTurns
      conversation="conversation-a"
      eventId={44}
      direction="inbound"
      refresh={1}
    />,
  );
  expect(
    (await screen.findAllByText("turn-new", { exact: false })).length,
  ).toBeGreaterThan(0);
  expect(screen.queryAllByText("turn-old", { exact: false })).toHaveLength(0);
});

it("refreshes current execution immediately when a hidden page becomes visible", async () => {
  const originalHidden = Object.getOwnPropertyDescriptor(document, "hidden");
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: true,
  });
  let calls = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
    if (String(url).endsWith("read_conversation_execution")) calls++;
    return answer({
      fields: {
        conversation_id: "conversation-a",
        observed_at: "2026-09-28T08:00:00Z",
        state: "idle",
        active: [],
        recent: [],
      },
    });
  });
  try {
    const view = render(
      <LiveSession conversation="conversation-a" refresh={0} />,
    );
    await waitFor(() => expect(calls).toBe(1));
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(calls).toBe(2));
    view.unmount();
  } finally {
    if (originalHidden)
      Object.defineProperty(document, "hidden", originalHidden);
    else Reflect.deleteProperty(document, "hidden");
  }
});

it("opens event execution beside the chat without losing the timeline", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
    const method = String(url).split("/").pop();
    if (method === "list_chat_events")
      return answer({
        items: [
          {
            event_id: 42,
            direction: "inbound",
            sender_display_name: "远野",
            content: "在做什么",
            occurred_at: "2026-09-28T08:00:00Z",
          },
        ],
        next_cursor: null,
      });
    return answer({ items: [], total: 0, next_cursor: null });
  });
  render(
    <Chat
      conversation="conversation-a"
      content
      refresh={0}
      notebook={<div />}
    />,
  );
  await userEvent
    .setup()
    .click(await screen.findByRole("button", { name: "#42 · 查看事件与执行" }));
  expect(
    screen.getByRole("dialog", { name: "事件 #42 的执行过程" }),
  ).toBeInTheDocument();
  expect(screen.getByText("在做什么")).toBeInTheDocument();
});
