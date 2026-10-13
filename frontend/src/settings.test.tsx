import { expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsPage } from "./pages";

it.each([false, true])(
  "uses the selected scope revision for group edit and unset: local=%s",
  async (local) => {
    const act = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const method = String(url).split("/").pop();
      const data = method === "list_effective_configs"
        ? {
            items: [{
              key: "conversation.auto_reply_enabled",
              value: false,
              saved_value: false,
              apply_mode: "hot",
              scope_type: local ? "group" : "global",
              version: local ? 3 : 5,
            }],
            next_cursor: null,
          }
        : { items: [], next_cursor: null };
      return new Response(JSON.stringify({ ok: true, data }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    render(<SettingsPage allowed={() => true} act={act} refresh={0} conversation="" />);
    await userEvent.selectOptions(screen.getByLabelText("配置范围"), "group");
    await userEvent.type(screen.getByLabelText("范围内部 ID"), "space-id");
    await userEvent.click(screen.getByRole("button", { name: "查看" }));
    const row = await screen.findByText("conversation.auto_reply_enabled");
    const tableRow = row.closest("tr")!;
    await userEvent.click(within(tableRow).getByRole("button", { name: "编辑" }));
    await waitFor(() => expect(act).toHaveBeenCalled());
    expect(act.mock.calls[0][0]).toMatchObject({
      revision: local ? 3 : 0,
      payload: { scope_type: "group", scope_id: "space-id", value: false },
    });
    const unset = within(tableRow).getByRole("button", { name: "取消覆盖" });
    if (local) {
      expect(unset).toBeEnabled();
      await userEvent.click(unset);
      expect(act.mock.calls[1][0].revision).toBe(3);
    } else {
      expect(unset).toBeDisabled();
    }
  },
);
