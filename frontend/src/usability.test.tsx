import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryList, Table } from "./components";
import { MediaPreview } from "./preview";
import { Chat } from "./chat";

it("shows true numbered count and jumps directly to a later page", async () => {
  const seen: number[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => {
    const body = JSON.parse(String(options?.body));
    seen.push(body.page.number);
    return new Response(
      JSON.stringify({
        data: {
          items: [{ id: body.page.number, title: `row-${body.page.number}` }],
          total: 65,
          number: body.page.number,
          next_cursor: null,
        },
        problem: null,
      }),
      { status: 200 },
    );
  });
  const user = userEvent.setup();
  render(<QueryList method="list_plugins" columns={[["title", "名称"]]} />);
  expect(await screen.findByText("row-1")).toBeInTheDocument();
  expect(screen.getByText(/第 1 页 \/ 共 3 页 · 共 65 条/)).toBeInTheDocument();
  await user.clear(screen.getByRole("spinbutton", { name: "页码" }));
  await user.type(screen.getByRole("spinbutton", { name: "页码" }), "3");
  await user.click(screen.getByRole("button", { name: "跳转" }));
  expect(await screen.findByText("row-3")).toBeInTheDocument();
  expect(seen).toEqual([1, 3]);
});

it("truncates and restores long cells without losing their content", async () => {
  const user = userEvent.setup();
  const full = "回忆".repeat(130);
  render(
    <Table rows={[{ id: 1, content: full }]} columns={[["content", "内容"]]} />,
  );
  expect(screen.getByText(/…$/).textContent!.length).toBeLessThan(full.length);
  await user.click(screen.getByRole("button", { name: "展开全文" }));
  expect(screen.getByText(full)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "收起" }));
  expect(screen.queryByText(full)).not.toBeInTheDocument();
});

it("renders a fetched image thumbnail and desktop preview", async () => {
  // Node's Response reads the same bytes directly; jsdom Blob lacks stream().
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("image", {
          status: 200,
          headers: { "Content-Type": "image/png" },
        }),
    ),
  );
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: () => "blob:preview",
    revokeObjectURL: () => {},
  });
  const user = userEvent.setup();
  render(
    <MediaPreview url="/api/control/files/workspace/original" title="图片" />,
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "预览图片" }),
    ).toBeInTheDocument(),
  );
  await user.click(screen.getByRole("button", { name: "预览图片" }));
  expect(screen.getByRole("dialog", { name: "图片" })).toBeInTheDocument();
});

it("loads image bytes only when its preview approaches the viewport", async () => {
  const fetch = vi.fn(
    async () =>
      new Response("image", {
        status: 200,
        headers: { "Content-Type": "image/png" },
      }),
  );
  let reveal: ((entries: { isIntersecting: boolean }[]) => void) | undefined;
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(callback: typeof reveal) {
        reveal = callback;
      }
      observe() {}
      disconnect() {}
    },
  );
  render(
    <MediaPreview url="/api/control/files/workspace/later" title="稍后加载" />,
  );
  expect(fetch).not.toHaveBeenCalled();
  reveal?.([{ isIntersecting: true }]);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  vi.unstubAllGlobals();
});

it("uses a bounded chat cursor, shows older messages above newer ones, and locates an event", async () => {
  const seen: Array<{
    cursor?: string;
    number?: number;
    history: Record<string, unknown>;
  }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => {
    const body = JSON.parse(String(options?.body));
    if (!String(_url).endsWith("list_chat_events"))
      return new Response(JSON.stringify({ data: { items: [] } }), {
        status: 200,
      });
    seen.push({ ...body.page, history: body.history });
    const ids = body.history.through_event_id
      ? [2, 1]
      : body.page.cursor
        ? [1]
        : [3, 2];
    return new Response(
      JSON.stringify({
        data: {
          items: ids.map((id) => ({
            event_id: id,
            direction: "inbound",
            sender_display_name: "远野",
            content: `消息${id}`,
            occurred_at: "2026-09-28T00:00:00Z",
          })),
          next_cursor:
            !body.page.cursor && !body.history.through_event_id
              ? "older"
              : null,
        },
        problem: null,
      }),
      { status: 200 },
    );
  });
  const user = userEvent.setup();
  render(
    <Chat
      conversation="canonical-fixture"
      content
      refresh={0}
      notebook={<div />}
    />,
  );
  expect(await screen.findByText("消息3")).toBeInTheDocument();
  expect(
    [...document.querySelectorAll(".chat-timeline article")].map(
      (row) => row.textContent,
    ),
  ).toEqual(
    expect.arrayContaining([
      expect.stringContaining("消息2"),
      expect.stringContaining("消息3"),
    ]),
  );
  expect(screen.queryByRole("spinbutton", { name: "聊天页码" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "加载更早消息" }));
  expect(await screen.findByText("消息1")).toBeInTheDocument();
  expect(
    [...document.querySelectorAll(".chat-timeline article")].map((row) =>
      Number(row.getAttribute("data-event-id")),
    ),
  ).toEqual([1, 2, 3]);
  expect(seen.slice(0, 2).map((request) => request.cursor)).toEqual([
    undefined,
    "older",
  ]);
  expect(seen.every((request) => request.number === undefined)).toBe(true);
  await user.clear(screen.getByRole("spinbutton", { name: "事件" }));
  await user.type(screen.getByRole("spinbutton", { name: "事件" }), "2");
  await user.click(screen.getByRole("button", { name: "查找" }));
  await waitFor(() => expect(seen.at(-1)?.history.through_event_id).toBe(2));
  expect(await screen.findByText("消息2")).toBeInTheDocument();
});
