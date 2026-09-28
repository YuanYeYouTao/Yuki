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
              cache_reported_input_tokens: 100,
              cache_reported_cached_tokens: 60,
              cache_unreported_calls: 1,
              missing_usage_calls: 1,
              models: [
                {
                  provider: "fixture",
                  model: "m1",
                  calls: 3,
                  total_tokens: 120,
                  input_tokens: 100,
                  cached_input_tokens: 60,
                  cache_reported_input_tokens: 100,
                  cache_reported_cached_tokens: 60,
                  cache_unreported_calls: 1,
                  output_tokens: 20,
                  missing_usage_calls: 1,
                },
              ],
              profiles: [
                {
                  profile_id: "main",
                  provider: "fixture",
                  model: "m1",
                  calls: 3,
                  total_tokens: 120,
                  cached_input_tokens: 60,
                  cache_reported_input_tokens: 100,
                  cache_reported_cached_tokens: 60,
                  cache_unreported_calls: 1,
                },
              ],
              tasks: [
                {
                  task: "chat_agent",
                  calls: 3,
                  total_tokens: 120,
                  cached_input_tokens: 60,
                  cache_reported_input_tokens: 100,
                  cache_reported_cached_tokens: 60,
                  cache_unreported_calls: 1,
                },
              ],
              buckets: [
                { at: "2026-09-28T08:00:00Z", calls: 3, total_tokens: 120 },
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
  expect(await screen.findAllByText(/缓存命中率 60.0%/)).toHaveLength(2);
  expect(
    screen.getByRole("list", { name: "API 调用次数" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Token 用量" })).toBeInTheDocument();
  expect(screen.getByText(/1 次调用的上游未报告总 Token/)).toBeInTheDocument();
  expect(screen.getByText(/不是供应商账单/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "最近 7 天" }));
  expect(calls).toEqual(["24h", "7d"]);
});
