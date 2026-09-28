import { expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Memory } from "./memory";

const owner = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

function scene() {
  const requests: { method: string; body: Record<string, unknown> }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    const body = JSON.parse(String(options?.body || "{}"));
    requests.push({ method, body });
    const data =
      method === "read_memory_fact"
        ? {
            resource_id: "12",
            fields: {
              fact_id: 12,
              revision: 47,
              scope_type: "self",
              status: "active",
              review_state: "verified",
              content: "original private fact",
            },
          }
        : method === "read_display_names"
          ? { fields: { names: { [owner]: "夜聊群" } } }
          : method === "read_memory_health"
            ? {
                index: "ok",
                embedding: "not_configured",
                consistency: "ok",
                embedding_requested: true,
                embedding_configured: false,
                embedding_ready_count: 0,
                embedding_fact_count: 12,
                embedding_failed_jobs: 0,
                embedding_saved_enabled: true,
                embedding_config_version: null,
                embedding_pending_restart: false,
              }
            : {
                items:
                  method === "list_spaces"
                    ? [{ space_id: owner, name: "夜聊群" }]
                    : method === "list_persons"
                      ? [{ person_id: owner }]
                      : method === "list_memory_facts"
                        ? [
                            {
                              fact_id: 12,
                              scope_type: "self",
                              status: "active",
                              review_state: "verified",
                            },
                          ]
                        : method === "list_memory_evidence"
                          ? [
                              {
                                evidence_id: 4,
                                fact_id: 12,
                                event_id: 83,
                                relation: "agent_reflection",
                              },
                            ]
                          : [],
                next_cursor: null,
              };
    return new Response(JSON.stringify({ data, problem: null }), {
      status: 200,
    });
  });
  const act = vi.fn();
  render(<Memory allowed={() => true} act={act} refresh={0} conversation="" />);
  return { requests, act, user: userEvent.setup() };
}

it("shows missing embedding credentials and can disable the global service", async () => {
  const { act, user } = scene();
  expect(
    await screen.findByText(/缺少 Embedding 地址或 API Key/),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "关闭向量检索" }));
  expect(act).toHaveBeenCalledWith(
    expect.objectContaining({
      method: "set_config",
      revision: 0,
      payload: {
        key: "memory.embedding_enabled",
        scope_type: "global",
        scope_id: "",
        value: false,
      },
    }),
  );
});

it("applies canonical ownership on submit and clears unrelated SELF selectors", async () => {
  const { requests, user } = scene();
  await user.selectOptions(
    screen.getByRole("combobox", { name: "记忆归属" }),
    "self",
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "SELF 可见范围" }),
    "group",
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "可见群" }),
    owner,
  );
  expect(
    requests.some(
      (r) =>
        (r.body.scope as Record<string, unknown>)?.visibility_space_id ===
        owner,
    ),
  ).toBe(false);
  await user.click(screen.getByRole("button", { name: "应用全库筛选" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.method === "list_memory_facts" &&
          JSON.stringify(r.body.scope) ===
            JSON.stringify({
              scope_type: "self",
              visibility_type: "group",
              visibility_space_id: owner,
            }),
      ),
    ).toBe(true),
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "记忆归属" }),
    "person",
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "记忆人物" }),
    owner,
  );
  await user.click(screen.getByRole("button", { name: "应用全库筛选" }));
  await waitFor(() =>
    expect(
      requests.filter((r) => r.method === "list_memory_facts").at(-1)?.body
        .scope,
    ).toEqual({ scope_type: "person", person_id: owner }),
  );
  await user.type(
    screen.getByRole("spinbutton", { name: "来源事件 ID" }),
    "83",
  );
  await user.type(
    screen.getByRole("spinbutton", { name: "工具证据回执 ID" }),
    "9",
  );
  expect(screen.getByRole("spinbutton", { name: "来源事件 ID" })).toHaveValue(
    null,
  );
});

it("loads original fact evidence and reviews mutations with detail revision", async () => {
  const { requests, act, user } = scene();
  await user.click(
    await screen.findByRole("button", { name: "详情与证据 #12" }),
  );
  expect(await screen.findByText("original private fact")).toBeInTheDocument();
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.method === "list_memory_evidence" &&
          (r.body.scope as Record<string, unknown>)?.fact_id === 12,
      ),
    ).toBe(true),
  );
  const detail = screen
    .getByRole("heading", { name: "记忆 #12" })
    .closest("section")!;
  await user.click(within(detail).getByRole("button", { name: "隔离" }));
  expect(act).toHaveBeenCalledWith({
    method: "mutate_memory",
    label: "隔离记忆",
    revision: 47,
    payload: { action: "quarantine", resource_id: "12" },
  });
  await user.click(screen.getByRole("button", { name: "来源执行" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.method === "list_execution_trace" &&
          (r.body.scope as Record<string, unknown>)?.source_event_id === 83,
      ),
    ).toBe(true),
  );
  expect(requests.every((r) => !r.method.includes("commands"))).toBe(true);
});
