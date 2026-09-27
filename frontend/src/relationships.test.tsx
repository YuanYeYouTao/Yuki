import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Relationships } from "./relationships";

it("reads selected canonical relationship and reviews original bounded score actions", async () => {
  const person = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const requests: { method: string; body: Record<string, unknown> }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").pop()!;
    const body = JSON.parse(String(options?.body || "{}"));
    requests.push({ method, body });
    const row = {
      resource_id: person,
      fields: {
        person_id: person,
        revision: 38,
        affection_score: 77,
        trust_score: 64,
        stage: "close",
      },
    };
    return new Response(
      JSON.stringify({
        data:
          method === "read_relationship"
            ? row
            : {
                items: method === "list_relationships" ? [row] : [],
                next_cursor: null,
              },
        problem: null,
      }),
      { status: 200 },
    );
  });
  const act = vi.fn(),
    user = userEvent.setup();
  render(
    <Relationships
      allowed={() => true}
      act={act}
      refresh={0}
      conversation=""
    />,
  );
  await user.click(await screen.findByRole("button", { name: "详情与历史" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) => r.method === "read_relationship" && r.body.person_id === person,
      ),
    ).toBe(true),
  );
  await user.type(
    await screen.findByRole("spinbutton", { name: "设置好感" }),
    "88",
  );
  await user.click(screen.getAllByRole("button", { name: "检查并提交" })[0]);
  expect(act).toHaveBeenCalledWith({
    method: "mutate_relationship",
    label: "设置好感",
    revision: 38,
    payload: {
      action: "set_affection",
      resource_id: person,
      spec: { value: 88 },
    },
  });
  await user.selectOptions(
    screen.getByRole("combobox", { name: "历史类别" }),
    "jobs",
  );
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.method === "list_relationship_history" &&
          r.body.person_id === person &&
          r.body.section === "jobs",
      ),
    ).toBe(true),
  );
  expect(requests.every((r) => !r.method.includes("commands"))).toBe(true);
});
