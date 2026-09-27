import { expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AutomationHistory } from "./automation-history";

it("pages original runs and scopes steps to the selected execution", async () => {
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (url, init) => {
      const method = String(url).split("/").pop(),
        args = JSON.parse(String(init?.body));
      const data =
        method === "list_automation_runs"
          ? {
              items: [
                {
                  resource_id: args.page.cursor ? "41" : "42",
                  fields: {
                    id: args.page.cursor ? 41 : 42,
                    status: "succeeded",
                  },
                },
              ],
              next_cursor: args.page.cursor ? null : "run-cursor",
            }
          : {
              items: [
                {
                  resource_id: "701",
                  fields: {
                    id: 701,
                    run_id: args.run_id || 42,
                    step_id: "original-step",
                    status: "succeeded",
                  },
                },
              ],
              next_cursor: null,
            };
      return new Response(JSON.stringify({ data }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
  const user = userEvent.setup();
  render(
    <AutomationHistory automationId={17} allowed={() => true} refresh={0} />,
  );
  const runs = within(screen.getByRole("region", { name: "自动化执行历史" }));
  await user.click(await runs.findByRole("button", { name: "查看本次步骤" }));
  await screen.findByRole("heading", { name: "执行 #42 的步骤" });
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("list_automation_steps") &&
          JSON.parse(String(init?.body)).run_id === 42,
      ),
    ).toBe(true),
  );
  await user.click(runs.getByRole("button", { name: "下一页" }));
  await runs.findByText("41");
  await user.click(screen.getByRole("button", { name: "查看全部执行" }));
  await screen.findByRole("heading", { name: "全部执行步骤" });
  expect(
    fetch.mock.calls.every(
      ([, init]) => JSON.parse(String(init?.body)).automation_id === 17,
    ),
  ).toBe(true);
});

it("does not read ungranted history collections", () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  render(
    <AutomationHistory automationId={17} allowed={() => false} refresh={0} />,
  );
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.queryByRole("region")).not.toBeInTheDocument();
});
