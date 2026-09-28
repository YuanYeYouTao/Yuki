import { expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Files } from "./workspace";
import { PluginApproval } from "./plugin-approval";
import { Identity } from "./identity";
import { MemoryMaintenance } from "./memory-maintenance";
import { SchemaFields } from "./schema-fields";
import { useState } from "react";
import type { Row } from "./api";

const props = {
  allowed: () => true,
  act: vi.fn(),
  refresh: 0,
  conversation: "",
};
function responses(values: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    const method = String(url).split("/").pop()!;
    return new Response(
      JSON.stringify({
        data: values[method] || { items: [], next_cursor: null },
      }),
      { status: 200 },
    );
  });
}

it("keeps published snapshots distinct from live workspace files", async () => {
  responses({
    list_workspace: {
      items: [
        {
          resource_id: "original",
          fields: { name: "large.txt", revision: 17 },
        },
      ],
      next_cursor: null,
    },
    read_workspace: {
      fields: {
        name: "large.txt",
        revision: 17,
        text: "partial",
        truncated: true,
      },
    },
  });
  const act = vi.fn();
  render(<Files {...props} act={act} />);
  await userEvent.click(
    screen.getByText("已发布的文件快照（独立于当前工作区）"),
  );
  await userEvent.click(await screen.findByRole("button", { name: "查看" }));
  expect(await screen.findByText("快照预览已截断。")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "保存到工作区" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "删除共享文件快照" }),
  ).not.toBeInTheDocument();
  expect(act).not.toHaveBeenCalled();
});

it("reviews original plugin hash and selected declared permissions without raw JSON", async () => {
  responses({
    read_plugin_approval: {
      fields: {
        revision: 771,
        manifest_hash: "a".repeat(64),
        manifest_available: true,
        manifest_hash_matches: true,
        requested_permissions: ["http.read", "plugin.config.read"],
        approved_permissions: [],
      },
    },
  });
  const act = vi.fn();
  render(<PluginApproval pluginId="fixture" props={{ ...props, act }} />);
  await userEvent.click(
    await screen.findByRole("checkbox", { name: "plugin.config.read" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "检查并批准" }));
  expect(act).toHaveBeenCalledWith(
    expect.objectContaining({
      revision: 771,
      payload: {
        resource_id: "fixture",
        action: "approve",
        spec: { permissions: ["plugin.config.read"] },
      },
    }),
  );
  expect(document.querySelector("textarea")).toBeNull();
});

it("edits and pauses the original route using its canonical IDs and revision", async () => {
  const row = {
    kind: "person_active",
    person_id: "person-uuid",
    identity_binding_id: "binding-uuid",
    presence_id: "presence-uuid",
    paused: false,
    revision: 912,
    route_generation: 4,
  };
  responses({ list_person_active_routes: { items: [row], next_cursor: null } });
  const act = vi.fn();
  render(<Identity {...props} act={act} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑原路由" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "检查并保存路由" }));
  expect(act).toHaveBeenCalledWith(
    expect.objectContaining({
      method: "set_route",
      revision: 912,
      target: { kind: "person", id: "person-uuid" },
      payload: {
        kind: "person_active",
        paused: false,
        presence_id: "presence-uuid",
        identity_binding_id: "binding-uuid",
      },
    }),
  );
  await userEvent.click(screen.getByRole("button", { name: "暂停" }));
  expect(act).toHaveBeenLastCalledWith(
    expect.objectContaining({
      method: "pause_route",
      revision: 912,
      target: { kind: "person", id: "person-uuid" },
    }),
  );
});

it("reviews an original rebuild proposal and prevents commit with pending candidates", async () => {
  responses({
    list_operations: {
      items: [{ operation_id: "rebuild:original", status: "waiting" }],
      next_cursor: null,
    },
    read_memory_maintenance_run: {
      fields: {
        kind: "rebuild",
        public_id: "original",
        status: "review",
        revision: 811,
        review_counts: { pending: 1 },
      },
    },
    list_memory_rebuild_proposals: {
      items: [
        {
          resource_id: "94",
          fields: {
            id: 94,
            event_id: 6912,
            review_status: "pending",
            content_visible: true,
            content: "原候选",
            evidence_quote: "原证据",
          },
        },
      ],
      next_cursor: null,
    },
  });
  const act = vi.fn();
  render(<MemoryMaintenance props={{ ...props, act }} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "范围与执行详情" }),
  );
  await userEvent.click(await screen.findByRole("button", { name: "批准" }));
  expect(act).toHaveBeenCalledWith(
    expect.objectContaining({
      method: "rebuild_memory",
      revision: 811,
      payload: {
        action: "approve",
        resource_id: "original",
        spec: { proposal_ids: [94] },
      },
    }),
  );
  expect(screen.getByRole("button", { name: "提交已审核候选" })).toBeDisabled();
  expect(screen.getByRole("link", { name: "事件 6912" })).toHaveAttribute(
    "href",
    "#audit?event=6912",
  );
});

function TemplateFixture() {
  const [values, setValues] = useState<Row>({ seconds: 60 });
  const schema = {
    type: "object",
    properties: { seconds: { type: "integer", minimum: 0 } },
  };
  return (
    <>
      <SchemaFields
        schema={schema}
        root={schema}
        values={values}
        change={setValues}
        prefix="fixture"
        templates
      />
      <output>{JSON.stringify(values)}</output>
    </>
  );
}
it("supports typed previous-step templates rather than converting them to numbers", async () => {
  render(<TemplateFixture />);
  await userEvent.selectOptions(screen.getByLabelText("参数来源"), "template");
  await userEvent.type(
    screen.getByLabelText("原模板表达式"),
    "${{prior.seconds}",
  );
  expect(screen.getByRole("status")).toHaveTextContent(
    '"seconds":"${prior.seconds}"',
  );
  await userEvent.selectOptions(screen.getByLabelText("参数来源"), "literal");
  expect(within(document.body).getByRole("spinbutton")).toHaveValue(0);
});
