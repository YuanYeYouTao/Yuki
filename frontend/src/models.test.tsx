import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { Models } from "./models";

it("shows window totals, keeps cached input inside input, and marks missing usage", async () => {
  const calls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    if (method === "read_model_usage_summary") {
      const window = JSON.parse(String(options?.body)).window;
      calls.push(window);
      return new Response(
        JSON.stringify({
          data: {
            fields: {
              window,
              since: "2026-09-28T00:00:00Z",
              until: "2026-09-29T00:00:00Z",
              calls: 3,
              input_tokens: 100,
              output_tokens: 20,
              total_tokens: 120,
              cached_input_tokens: 60,
              missing_usage_calls: 1,
              models: [
                {
                  provider: "fixture",
                  model: "m1",
                  calls: 3,
                  total_tokens: 120,
                  input_tokens: 100,
                  cached_input_tokens: 60,
                  output_tokens: 20,
                  missing_usage_calls: 1,
                },
              ],
            },
          },
          problem: null,
        }),
      );
    }
    return new Response(
      JSON.stringify({
        data: { fields: { profiles: [], routes: [] }, items: [] },
        problem: null,
      }),
    );
  });
  render(
    <Models allowed={() => true} act={() => {}} refresh={0} conversation="" />,
  );
  expect(await screen.findByText(/60 Token 命中缓存/)).toBeInTheDocument();
  expect(screen.getByText(/1 次调用的上游未报告总 Token/)).toBeInTheDocument();
  expect(screen.getByText(/不是供应商账单/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "最近 7 天" }));
  expect(calls).toEqual(["24h", "7d"]);
});
