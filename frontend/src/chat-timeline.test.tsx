import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Chat } from "./chat";

function response(ids: number[], nextCursor: string | null) {
  return new Response(
    JSON.stringify({
      data: {
        items: ids.map((eventId) => ({
          event_id: eventId,
          direction: "inbound",
          content: `消息${eventId}`,
          occurred_at: "2026-09-28T00:00:00Z",
        })),
        next_cursor: nextCursor,
      },
    }),
    { status: 200 },
  );
}

afterEach(() => vi.unstubAllGlobals());

it("loads older chat on upward scroll, keeps the visible anchor through insertion and resizing", async () => {
  let resize: (() => void) | undefined;
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        resize = callback;
      }
      observe() {}
      disconnect() {}
    },
  );
  let olderLoaded = false;
  let anchorTop = 10;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      const top =
        this.getAttribute("data-event-id") === "2"
          ? olderLoaded
            ? anchorTop -
              ((document.querySelector<HTMLElement>(".chat-messages")
                ?.scrollTop || 50) -
                50)
            : 10
          : this.getAttribute("data-event-id") === "3"
            ? 40
            : 0;
      return {
        top,
        bottom: top + 20,
        left: 0,
        right: 100,
        width: 100,
        height: 20,
        x: 0,
        y: top,
        toJSON() {},
      };
    },
  );
  const requests: Array<Record<string, unknown>> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    if (!String(url).endsWith("list_chat_events")) return response([], null);
    const body = JSON.parse(String(options?.body));
    requests.push(body);
    if (body.page.cursor) {
      olderLoaded = true;
      anchorTop = 40;
      return response([1], null);
    }
    return response([3, 2], "older");
  });
  render(
    <Chat conversation="conversation" content refresh={0} notebook={<div />} />,
  );
  expect(await screen.findByText("消息3")).toBeInTheDocument();
  const viewport = document.querySelector<HTMLElement>(".chat-messages")!;
  Object.defineProperty(viewport, "scrollHeight", {
    configurable: true,
    value: 1000,
  });
  Object.defineProperty(viewport, "clientHeight", {
    configurable: true,
    value: 100,
  });
  viewport.scrollTop = 100;
  fireEvent.scroll(viewport);
  viewport.scrollTop = 50;
  fireEvent.scroll(viewport);
  await waitFor(() => expect(requests.length).toBe(2));
  await waitFor(() => expect(viewport.scrollTop).toBe(80));
  expect(
    [...document.querySelectorAll(".chat-timeline article")].map((row) =>
      row.getAttribute("data-event-id"),
    ),
  ).toEqual(["1", "2", "3"]);
  anchorTop = 60;
  await act(async () => resize?.());
  expect(viewport.scrollTop).toBe(100);
});

it("keeps at most 160 messages mounted and offers a clear return to latest", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    if (!String(url).endsWith("list_chat_events")) return response([], null);
    const body = JSON.parse(String(options?.body));
    const page = body.page.cursor
      ? Number(String(body.page.cursor).slice(1))
      : 0;
    const highest = 240 - page * 40;
    return response(
      Array.from({ length: 40 }, (_, index) => highest - index),
      page < 5 ? `c${page + 1}` : null,
    );
  });
  render(
    <Chat conversation="conversation" content refresh={0} notebook={<div />} />,
  );
  expect(await screen.findByText("消息240")).toBeInTheDocument();
  for (let page = 1; page <= 5; page++) {
    await userEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
    expect(
      await screen.findByText(`消息${240 - page * 40}`),
    ).toBeInTheDocument();
    expect(
      document.querySelectorAll(".chat-timeline article").length,
    ).toBeLessThanOrEqual(160);
  }
  expect(
    screen.getByRole("button", { name: "较新消息已暂时收起，回到最新" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("消息240")).toBeNull();
  await userEvent.click(
    screen.getByRole("button", { name: "较新消息已暂时收起，回到最新" }),
  );
  expect(await screen.findByText("消息240")).toBeInTheDocument();
});

it("does not move the timeline while reading history when new messages arrive", async () => {
  let newest = 3;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    if (!String(url).endsWith("list_chat_events")) return response([], null);
    return response([newest, newest - 1], null);
  });
  const view = render(
    <Chat conversation="conversation" content refresh={0} notebook={<div />} />,
  );
  expect(await screen.findByText("消息3")).toBeInTheDocument();
  const viewport = document.querySelector<HTMLElement>(".chat-messages")!;
  Object.defineProperty(viewport, "scrollHeight", {
    configurable: true,
    value: 1000,
  });
  Object.defineProperty(viewport, "clientHeight", {
    configurable: true,
    value: 100,
  });
  viewport.scrollTop = 300;
  fireEvent.scroll(viewport);
  newest = 4;
  view.rerender(
    <Chat conversation="conversation" content refresh={1} notebook={<div />} />,
  );
  expect(
    await screen.findByRole("button", { name: "有新消息，回到底部" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("消息4")).toBeNull();
  expect(viewport.scrollTop).toBe(300);
  await userEvent.click(
    screen.getByRole("button", { name: "有新消息，回到底部" }),
  );
  expect(await screen.findByText("消息4")).toBeInTheDocument();
});

it("does not let an old history request block a new timeline scope", async () => {
  const cursors: string[] = [];
  let latestReads = 0;
  let oldRequestAborted = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    if (!String(url).endsWith("list_chat_events")) return response([], null);
    const body = JSON.parse(String(options?.body));
    const cursor = body.page.cursor as string | undefined;
    if (!cursor) {
      latestReads++;
      return latestReads === 1
        ? response([3, 2], "older-a")
        : response([8, 7], "older-b");
    }
    cursors.push(cursor);
    if (cursor === "older-a")
      return new Promise<Response>((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => {
          oldRequestAborted = true;
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    return response([6], null);
  });
  render(
    <Chat conversation="conversation" content refresh={0} notebook={<div />} />,
  );
  expect(await screen.findByText("消息3")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
  await waitFor(() => expect(cursors).toEqual(["older-a"]));
  await userEvent.click(screen.getByRole("button", { name: "最新" }));
  expect(await screen.findByText("消息8")).toBeInTheDocument();
  expect(oldRequestAborted).toBe(true);
  await userEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
  expect(await screen.findByText("消息6")).toBeInTheDocument();
  expect(cursors).toEqual(["older-a", "older-b"]);
});
