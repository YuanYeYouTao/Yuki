import { expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

let terminalInput: (text: string) => void = () => {};
const terminalOutput = vi.fn();
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    loadAddon() {}
    open() {}
    dispose() {}
    clear() {}
    focus() {}
    write(value: string) {
      terminalOutput(value);
    }
    writeln(value: string) {
      terminalOutput(value);
    }
    onData(callback: (text: string) => void) {
      terminalInput = callback;
      return { dispose() {} };
    }
  },
}));
vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit() {}
  },
}));
import { InteractiveTerminal } from "./terminal-console";

const run = "4f375f13-d0f0-480a-baeb-f4e86906fce2";

it("starts one original Manager tty, sends direct input, and reads streamed output", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  }));
  const requests: { method: string; body: Record<string, unknown> }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").at(-1)!;
    const body = JSON.parse(String(options?.body || "{}"));
    requests.push({ method, body });
    const data =
      method === "read_environment"
        ? {
            fields: {
              status: "running",
              run_id: run,
              output: "yuki@workspace$ ",
              next_cursor: 16,
            },
          }
        : { success: true, resource_id: run, revision: 1 };
    return new Response(JSON.stringify({ data }), { status: 200 });
  });
  const user = userEvent.setup();
  render(
    <InteractiveTerminal
      props={{
        allowed: () => true,
        act: vi.fn(),
        refresh: 0,
        conversation: "",
      }}
    />,
  );
  await user.click(screen.getByRole("button", { name: "新建终端" }));
  await waitFor(() =>
    expect(requests.some(({ method }) => method === "read_environment")).toBe(
      true,
    ),
  );
  expect(
    requests.find(({ method }) => method === "mutate_environment_terminal")
      ?.body.payload,
  ).toEqual({
    resource_id: "environment",
    action: "exec",
    spec: { command: "bash", cwd: "/workspace", tty: true, timeout_seconds: 0 },
  });
  expect(terminalOutput).toHaveBeenCalledWith("yuki@workspace$ ");
  await act(async () => {
    terminalInput("pwd\r");
    await new Promise((resolve) => setTimeout(resolve, 100));
  });
  await waitFor(() =>
    expect(
      requests.filter(({ method }) => method === "mutate_environment_terminal"),
    ).toHaveLength(2),
  );
  const write = requests.findLast(
    ({ method }) => method === "mutate_environment_terminal",
  )!.body;
  expect(write.payload).toEqual({
    resource_id: "environment",
    action: "write",
    spec: { run_id: run, text: "pwd\r" },
  });
  expect(write.request_id).toBeTruthy();
});

it("holds later terminal input while the original write receipt is pending", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  }));
  const writes: Record<string, unknown>[] = [];
  const receipts: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").at(-1)!;
    const body = JSON.parse(String(options?.body || "{}"));
    let data: Record<string, unknown>;
    if (method === "read_environment")
      data = { fields: { status: "running", output: "", next_cursor: 0 } };
    else if (method === "read_operation") {
      receipts.push(body);
      data = { status: "succeeded" };
    } else if (body.payload?.action === "write") {
      writes.push(body);
      data = {
        success: writes.length > 1,
        resource_id: run,
        operation: { status: writes.length > 1 ? "succeeded" : "running" },
      };
    } else data = { success: true, resource_id: run, revision: 1 };
    return new Response(JSON.stringify({ data }), { status: 200 });
  });
  const user = userEvent.setup();
  render(
    <InteractiveTerminal
      props={{
        allowed: () => true,
        act: vi.fn(),
        refresh: 0,
        conversation: "",
      }}
    />,
  );
  await user.click(screen.getByRole("button", { name: "新建终端" }));
  await act(async () => {
    terminalInput("a".repeat(1030));
    await new Promise((resolve) => setTimeout(resolve, 100));
  });
  expect(writes).toHaveLength(1);
  expect((writes[0].payload as Record<string, unknown>).spec).toEqual({
    run_id: run,
    text: "a".repeat(1024),
  });
  expect(screen.getByRole("button", { name: "Ctrl+C" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "查询原回执" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(receipts).toEqual([{ request_id: writes[0].request_id }]);
  expect((writes[1].payload as Record<string, unknown>).spec).toEqual({
    run_id: run,
    text: "a".repeat(6),
  });
  expect(writes[1].request_id).not.toBe(writes[0].request_id);
});

it("holds terminal input while the original interrupt receipt is pending", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  }));
  const mutations: Record<string, unknown>[] = [];
  const receipts: Record<string, unknown>[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").at(-1)!;
    const body = JSON.parse(String(options?.body || "{}"));
    let data: Record<string, unknown>;
    if (method === "read_environment")
      data = { fields: { status: "running", output: "", next_cursor: 0 } };
    else if (method === "read_operation") {
      receipts.push(body);
      data = { status: "succeeded" };
    } else {
      mutations.push(body);
      data = {
        success: body.payload?.action !== "control",
        resource_id: run,
        operation: { status: "running" },
      };
    }
    return new Response(JSON.stringify({ data }), { status: 200 });
  });
  const user = userEvent.setup();
  render(
    <InteractiveTerminal
      props={{
        allowed: () => true,
        act: vi.fn(),
        refresh: 0,
        conversation: "",
      }}
    />,
  );
  await user.click(screen.getByRole("button", { name: "新建终端" }));
  await user.click(screen.getByRole("button", { name: "Ctrl+C" }));
  await act(async () => {
    terminalInput("pwd\r");
    await new Promise((resolve) => setTimeout(resolve, 100));
  });
  expect(mutations).toHaveLength(2);
  expect(screen.getByRole("button", { name: "Ctrl+C" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "查询原回执" }));
  expect(receipts).toEqual([{ request_id: mutations[1].request_id }]);
  expect(mutations).toHaveLength(2);
});
