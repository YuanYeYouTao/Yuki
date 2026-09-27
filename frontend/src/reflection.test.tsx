import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Reflection } from "./reflection";

it("follows original cycle and batch IDs without executing reflection", async () => {
  const requests: { method: string; body: Record<string, unknown> }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    const body = JSON.parse(String(options?.body || "{}"));
    requests.push({ method, body });
    const data =
      method === "read_self_reflection_health"
        ? {
            resource_id: "reflection",
            fields: {
              calls_today: 1,
              daily_limit: 96,
              ingress_events_total: 12,
              processed_events_total: 7,
              ingress_events_per_hour: null,
              drain_events_per_hour: null,
            },
          }
        : {
            items:
              body.section === "cycles"
                ? [
                    {
                      resource_id: "sr_original",
                      fields: { id: "sr_original", status: "completed" },
                    },
                  ]
                : body.section === "runs"
                  ? [
                      {
                        resource_id: "8",
                        fields: {
                          id: 8,
                          cycle_id: "sr_original",
                          source_kind: "initiative_tools",
                          first_receipt_id: 35,
                          last_receipt_id: 42,
                        },
                      },
                    ]
                  : [],
            next_cursor: null,
          };
    return new Response(JSON.stringify({ data, problem: null }), {
      status: 200,
    });
  });
  const act = vi.fn(),
    user = userEvent.setup();
  render(
    <Reflection allowed={() => true} act={act} refresh={0} conversation="" />,
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "历史集合" }),
    "cycles",
  );
  await user.click(
    await screen.findByRole("button", { name: "查看本周期批次" }),
  );
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.body.section === "runs" &&
          (r.body.scope as Record<string, unknown>)?.cycle_id === "sr_original",
      ),
    ).toBe(true),
  );
  expect(await screen.findByText("initiative_tools")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "查看实际请求" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.body.section === "requests" &&
          (r.body.scope as Record<string, unknown>)?.run_id === 8,
      ),
    ).toBe(true),
  );
  expect(act).not.toHaveBeenCalled();
});
