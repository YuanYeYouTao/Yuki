import { it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AutomationEditor } from "./automation-editor";

it("edits native schedule variants and registered step arguments before reviewing the original revision", async () => {
  const daily = {
    type: "object",
    properties: {
      type: { const: "daily" },
      hour: { type: "integer", minimum: 0, maximum: 23 },
      minute: { type: "integer", minimum: 0, maximum: 59 },
    },
    required: ["type", "hour", "minute"],
  };
  const interval = {
    type: "object",
    properties: {
      type: { const: "interval" },
      seconds: { type: "integer", minimum: 1, maximum: 31536000 },
    },
    required: ["type", "seconds"],
  };
  const script = {
    version: 1,
    name: "现有任务",
    schedule: { type: "daily", hour: 23, minute: 30 },
    steps: [
      {
        id: "agent",
        call: "yuki.agent",
        arguments: { instruction: "整理工作区", delivery_target: "none" },
      },
    ],
  };
  const catalog = {
    script: {
      type: "object",
      properties: {
        name: { type: "string" },
        schedule: {
          oneOf: [daily, interval],
          discriminator: { propertyName: "type" },
        },
      },
    },
    capabilities: [
      {
        name: "yuki.agent",
        description: "原主 Agent",
        permission: "self",
        risk: "generate",
        schema: {
          type: "object",
          properties: {
            instruction: { type: "string" },
            delivery_target: {
              type: "string",
              enum: ["none", "current_group"],
            },
          },
        },
      },
    ],
  };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ data: { fields: catalog }, problem: null }), {
      status: 200,
    }),
  );
  const act = vi.fn();
  render(
    <AutomationEditor
      props={{
        allowed: () => true,
        act,
        refresh: 0,
        conversation: "scene-uuid",
      }}
      initial={script}
      automationId={94}
      revision={771}
      close={() => {}}
    />,
  );
  await screen.findByRole("textbox", { name: "任务名称" });
  const user = userEvent.setup();
  await user.selectOptions(
    screen.getByRole("combobox", { name: "安排类型" }),
    "interval",
  );
  await user.clear(screen.getByRole("spinbutton", { name: "间隔秒数 *" }));
  await user.type(
    screen.getByRole("spinbutton", { name: "间隔秒数 *" }),
    "7200",
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "投递目标" }),
    "none",
  );
  await user.click(screen.getByRole("button", { name: "检查并提交脚本" }));
  await waitFor(() => expect(act).toHaveBeenCalledOnce());
  expect(act.mock.calls[0][0]).toMatchObject({
    revision: 771,
    payload: {
      action: "update",
      resource_id: "94",
      spec: {
        script: {
          schedule: { type: "interval", seconds: 7200 },
          steps: [
            { call: "yuki.agent", arguments: { delivery_target: "none" } },
          ],
        },
      },
    },
  });
  expect(act.mock.calls[0][0].edit).toBeUndefined();
});

it("uses registry permission projections for SELF without discarding an existing step", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        data: {
          fields: {
            script: { type: "object", properties: {} },
            capabilities: [
              {
                name: "workspace.list",
                permitted_levels: ["user", "superuser"],
                schema: { type: "object", properties: {} },
              },
              {
                name: "yuki.agent",
                permitted_levels: ["self", "user", "superuser"],
                schema: { type: "object", properties: {} },
              },
            ],
          },
        },
      }),
      { status: 200 },
    ),
  );
  render(
    <AutomationEditor
      props={{
        allowed: () => true,
        act: vi.fn(),
        refresh: 0,
        conversation: "original",
      }}
      creatorKind="self"
      automationId={94}
      revision={11}
      initial={{
        name: "original",
        steps: [{ id: "old", call: "workspace.list", arguments: {} }],
      }}
      close={() => {}}
    />,
  );
  expect(
    await screen.findByText(
      "原 Registry 不允许 SELF 委托此能力。请选择其他能力。",
    ),
  ).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "workspace.list" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "检查并提交脚本" })).toBeDisabled();
  await userEvent.selectOptions(screen.getByLabelText("能力 1"), "yuki.agent");
  expect(screen.getByRole("button", { name: "检查并提交脚本" })).toBeEnabled();
});
