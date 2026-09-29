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
              input_tokens: 120,
              output_tokens: 20,
              total_tokens: 140,
              cached_input_tokens: 60,
              cache_write_input_tokens: 30,
              cache_write_5m_input_tokens: 15,
              cache_write_1h_input_tokens: 5,
              cache_write_5m_reported_calls: 2,
              cache_write_1h_reported_calls: 2,
              cache_write_ttl_unreported_calls: 1,
              cache_write_classified_input_tokens: 20,
              cache_write_unreported_calls: 1,
              cache_reported_input_tokens: 100,
              cache_reported_cached_tokens: 60,
              cache_unreported_calls: 1,
              missing_usage_calls: 1,
              models: [
                {
                  provider: "anthropic",
                  model: "m1",
                  calls: 3,
                  total_tokens: 140,
                  input_tokens: 120,
                  cached_input_tokens: 60,
                  cache_write_input_tokens: 30,
                  cache_write_5m_input_tokens: 15,
                  cache_write_1h_input_tokens: 5,
                  cache_write_5m_reported_calls: 2,
                  cache_write_1h_reported_calls: 2,
                  cache_write_ttl_unreported_calls: 1,
                  cache_write_classified_input_tokens: 20,
                  cache_write_unreported_calls: 1,
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
                  provider: "anthropic",
                  model: "m1",
                  calls: 3,
                  total_tokens: 140,
                  input_tokens: 120,
                  cached_input_tokens: 60,
                  cache_write_input_tokens: 30,
                  cache_reported_input_tokens: 100,
                  cache_reported_cached_tokens: 60,
                  cache_unreported_calls: 1,
                },
              ],
              tasks: [
                {
                  task: "chat_agent",
                  calls: 3,
                  total_tokens: 140,
                  input_tokens: 120,
                  cached_input_tokens: 60,
                  cache_write_input_tokens: 30,
                  cache_reported_input_tokens: 100,
                  cache_reported_cached_tokens: 60,
                  cache_unreported_calls: 1,
                },
              ],
              buckets: [
                {
                  at: "2026-09-28T08:00:00Z",
                  calls: 3,
                  input_tokens: 120,
                  output_tokens: 20,
                  total_tokens: 140,
                  cached_input_tokens: 60,
                  cache_write_input_tokens: 30,
                  cache_write_classified_input_tokens: 20,
                  cache_reported_input_tokens: 100,
                  cache_reported_cached_tokens: 60,
                  cache_unreported_calls: 1,
                },
              ],
              model_buckets: [
                {
                  provider: "anthropic",
                  model: "m1",
                  at: "2026-09-28T08:00:00Z",
                  calls: 3,
                  input_tokens: 120,
                  output_tokens: 20,
                  total_tokens: 140,
                  cached_input_tokens: 60,
                  cache_write_input_tokens: 30,
                  cache_write_classified_input_tokens: 20,
                  cache_reported_input_tokens: 100,
                  cache_reported_cached_tokens: 60,
                  cache_unreported_calls: 1,
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
  expect(
    (await screen.findAllByText(/已确认缓存占已记录输入 50.0%/)).length,
  ).toBeGreaterThanOrEqual(2);
  expect(screen.getAllByText(/已报告子集命中率 60.0%/).length).toBeGreaterThan(
    0,
  );
  expect(screen.getAllByRole("list", { name: "模型调用次数" })).toHaveLength(2);
  expect(screen.getAllByRole("list", { name: "Token 用量" })).toHaveLength(2);
  expect(
    screen.getAllByRole("listitem", {
      name: /已确认缓存占已记录输入 50.0%；已报告子集命中率 60.0%/,
    }),
  ).toHaveLength(4);
  expect(screen.getByText(/1 次调用的上游未报告总 Token/)).toBeInTheDocument();
  expect(screen.getByText(/不是供应商账单/)).toBeInTheDocument();
  expect(
    screen.getByText(/Claude 缓存写入已报告 30 Token/),
  ).toBeInTheDocument();
  expect(screen.getByText(/5 分钟写入已报告 15 Token/)).toBeInTheDocument();
  expect(screen.getByText(/1 小时写入已报告 5 Token/)).toBeInTheDocument();
  expect(screen.getByText(/1 次写入时长明细缺失或不一致/)).toBeInTheDocument();
  expect(
    screen.getAllByRole("listitem", { name: /Claude 缓存写入 30/ }),
  ).toHaveLength(2);
  await userEvent.click(screen.getByRole("button", { name: "最近 7 天" }));
  expect(calls).toEqual(["24h", "7d"]);
});
