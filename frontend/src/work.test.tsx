import { expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Work } from "./work";

const identity = "513313e2-8cbd-4db9-9ce0-0474a2c0c510";
function backend() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    const method = String(url).split("/").pop();
    const args = JSON.parse(String(init?.body));
    const data =
      method === "read_work"
        ? {
            resource_id: identity,
            fields: {
              state: "suspended",
              revision: 7,
              root_id: identity,
              generation: 1,
              conversation_id: "canonical",
              shared_budget: { models: 17 },
              goal: "继续原任务",
            },
          }
        : method === "list_work"
          ? {
              items: [
                {
                  resource_id: identity,
                  fields: { state: "suspended", revision: 7 },
                },
              ],
              next_cursor: null,
            }
          : method === "list_work_history" && args.section === "inputs"
            ? {
                items: [
                  {
                    resource_id: args.page.cursor ? "6911" : "6912",
                    fields: {
                      id: args.page.cursor ? 6911 : 6912,
                      event_id: args.page.cursor ? 12 : 13,
                    },
                  },
                ],
                next_cursor: args.page.cursor ? null : "original-cursor",
              }
            : { items: [], next_cursor: null };
    return new Response(JSON.stringify({ data }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

it("manages the selected Work with its read revision and pages original history", async () => {
  const fetch = backend(),
    act = vi.fn(),
    user = userEvent.setup();
  render(<Work allowed={() => true} act={act} refresh={0} conversation="" />);
  await user.click(await screen.findByRole("button", { name: "工作详情" }));
  await user.click(await screen.findByRole("button", { name: "续跑原工作" }));
  expect(act).toHaveBeenLastCalledWith(
    expect.objectContaining({
      method: "mutate_work",
      revision: 7,
      payload: { resource_id: identity, action: "resume" },
    }),
  );
  await user.click(screen.getByRole("button", { name: "取消工作树" }));
  expect(act).toHaveBeenLastCalledWith(
    expect.objectContaining({
      method: "mutate_work",
      revision: 7,
      payload: { resource_id: identity, action: "cancel" },
    }),
  );
  const history = within(screen.getByRole("region", { name: "接纳输入" }));
  await waitFor(() => expect(history.getByText("6912")).toBeInTheDocument());
  await user.click(history.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(history.getByText("6911")).toBeInTheDocument());
  expect(
    fetch.mock.calls.some(([url]) => String(url).includes("commands/")),
  ).toBe(false);
});

it("metadata access does not grant Work mutation or wait content", async () => {
  const fetch = backend(),
    user = userEvent.setup();
  render(
    <Work
      allowed={(name) =>
        name !== "mutate_work" && name !== "read_execution_trace"
      }
      act={vi.fn()}
      refresh={0}
      conversation=""
    />,
  );
  await user.click(await screen.findByRole("button", { name: "工作详情" }));
  await screen.findByText("共享累计预算");
  expect(
    screen.queryByRole("button", { name: "取消工作树" }),
  ).not.toBeInTheDocument();
  const calls = fetch.mock.calls.filter(([url]) =>
    String(url).endsWith("list_work_history"),
  );
  expect(calls.length).toBeGreaterThan(0);
  expect(
    calls.every(
      ([, init]) => JSON.parse(String(init?.body)).include_content === false,
    ),
  ).toBe(true);
});
