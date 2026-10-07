import { render, screen, waitFor, within } from "@testing-library/react";
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

it("explains a provider total that has no complete input/output breakdown", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    const method = String(url).split("/").pop();
    return new Response(
      JSON.stringify({
        data: {
          fields:
            method === "read_model_usage_summary"
              ? {
                  window: "24h",
                  since: "2026-09-28T11:31:00Z",
                  until: "2026-09-29T11:31:00Z",
                  calls: 3,
                  input_tokens: 100,
                  output_tokens: 20,
                  total_tokens: 187,
                  models: [],
                  profiles: [],
                  tasks: [],
                  buckets: [],
                  model_buckets: [],
                }
              : { profiles: [], routes: [] },
        },
        problem: null,
      }),
    );
  });
  render(
    <Models allowed={() => true} act={() => {}} refresh={0} conversation="" />,
  );
  expect(
    await screen.findByText(/上游总 Token 比已报告输入、输出之和多 67 Token/),
  ).toBeInTheDocument();
  expect(screen.getByText(/缺失的分项不能按差额推算/)).toBeInTheDocument();
});

it("explains loaded task routes with their provider and model", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    const method = String(url).split("/").pop();
    return new Response(
      JSON.stringify({
        data:
          method === "read_model_catalog"
            ? {
                fields: {
                  profiles: [
                    {
                      id: "current",
                      provider: "gemini",
                      model: "gemini-3.8-flash",
                    },
                    {
                      id: "spare",
                      provider: "deepseek",
                      model: "deepseek-flash",
                    },
                  ],
                  routes: [{ task: "chat_agent", profile_id: "current" }],
                },
              }
            : { fields: {}, items: [] },
        problem: null,
      }),
    );
  });
  render(
    <Models allowed={() => true} act={() => {}} refresh={0} conversation="" />,
  );
  await userEvent.click(await screen.findByText("查看已加载的模型与任务路由"));
  const routeHeading = screen.getByRole("heading", { name: "任务路由" });
  const routeTable = routeHeading.nextElementSibling?.querySelector("table");
  expect(routeTable).not.toBeNull();
  await waitFor(() =>
    expect(
      within(routeTable!).getByRole("row", {
        name: "主对话 gemini gemini-3.8-flash current",
      }),
    ).toBeInTheDocument(),
  );
  expect(within(routeTable!).queryByText("deepseek-flash")).toBeNull();
});

it.each([
  [100, 260, 60, 1, "已确认缓存占已记录输入 60.0%"],
  [100, 60, 60, 0, "缓存命中率 60.0%"],
  [100, 0, 0, 0, "缓存命中率 0.0%"],
  [0, 200, 0, 1, "已确认缓存占已记录输入 —"],
])(
  "keeps overview and model cache coverage aligned (%s/%s/%s/%s)",
  async (input, raw, reported, missing, label) => {
    const row = {
      calls: 2,
      input_tokens: input,
      cached_input_tokens: raw,
      cache_reported_input_tokens: input,
      cache_reported_cached_tokens: reported,
      cache_unreported_calls: missing,
      missing_usage_calls: missing,
      output_tokens: 10,
      total_tokens: input + 10,
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (url) =>
        new Response(
          JSON.stringify({
            data: {
              fields: String(url).endsWith("read_model_usage_summary")
                ? {
                    ...row,
                    window: "24h",
                    models: [
                      {
                        ...row,
                        provider: "anthropic",
                        model: "coverage-fixture",
                      },
                    ],
                    buckets: [],
                    model_buckets: [],
                    profiles: [],
                    tasks: [],
                  }
                : { profiles: [], routes: [] },
              items: [],
            },
            problem: null,
          }),
        ),
    );
    render(
      <Models
        allowed={() => true}
        act={() => {}}
        refresh={0}
        conversation=""
      />,
    );
    expect(
      (await screen.findAllByText(new RegExp(label.replace(".", "\\."))))
        .length,
    ).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(/260\.0%/)).not.toBeInTheDocument();
    if (missing && input)
      expect(
        screen.getAllByText(/已报告子集命中率 60.0%/).length,
      ).toBeGreaterThanOrEqual(2);
  },
);
