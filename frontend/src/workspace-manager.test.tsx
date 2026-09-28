import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FileManager } from "./workspace-manager";

const file = {
  path: "/workspace/notes.txt",
  name: "notes.txt",
  kind: "file",
  size: 6,
  modified_at: 1790553600,
};
const directory = {
  path: "/workspace/drafts",
  name: "drafts",
  kind: "directory",
  size: 0,
  modified_at: 1790553400,
};
const calls: { method: string; body: Record<string, unknown> }[] = [];

function backend() {
  calls.length = 0;
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (url, options) => {
      const method = String(url).split("/").at(-1)!;
      const body = JSON.parse(String(options?.body || "{}"));
      calls.push({ method, body });
      let data: unknown = {};
      if (method === "read_environment") {
        const args = body.arguments as Record<string, unknown>;
        data =
          body.section === "files"
            ? {
                fields: {
                  items: args.path === "/workspace" ? [file, directory] : [],
                  total: args.path === "/workspace" ? 2 : 0,
                  number: args.number,
                },
              }
            : {
                fields: {
                  path: file.path,
                  text: "原始内容",
                  version: "original-sha",
                  size: 6,
                  truncated: false,
                },
              };
      } else if (method === "mutate_environment_file")
        data = { success: true, resource_id: "environment", revision: 1 };
      else if (method === "read_operation")
        data = { success: true, resource_id: "environment" };
      return new Response(JSON.stringify({ data }), { status: 200 });
    });
}

const props = {
  allowed: () => true,
  act: vi.fn(),
  refresh: 0,
  conversation: "",
};

it("navigates the real workspace and deletes a file by its original SHA, never an artifact", async () => {
  backend();
  const user = userEvent.setup();
  render(<FileManager props={props} />);
  expect(await screen.findByText("notes.txt")).toBeInTheDocument();
  expect(screen.getByText(/共 2 项 · 第 1 页/)).toBeInTheDocument();
  await user.click(screen.getAllByRole("button", { name: "删除" })[0]);
  await waitFor(() =>
    expect(
      calls.some(({ method }) => method === "mutate_environment_file"),
    ).toBe(true),
  );
  const mutation = calls.find(
    ({ method }) => method === "mutate_environment_file",
  )!.body;
  expect(mutation.payload).toEqual({
    resource_id: "environment",
    action: "delete",
    spec: { path: file.path, expected_version: "original-sha" },
  });
  expect(calls.some(({ method }) => method === "mutate_workspace")).toBe(false);
  await user.click(screen.getByRole("button", { name: /drafts/ }));
  expect(await screen.findByText("这个目录目前没有文件。")).toBeInTheDocument();
  expect(
    calls.findLast(
      ({ method, body }) =>
        method === "read_environment" && body.section === "files",
    )?.body.arguments,
  ).toEqual({ path: directory.path, number: 1, limit: 30 });
});

it("opens a desktop file editor and saves with the current file SHA", async () => {
  backend();
  const user = userEvent.setup();
  render(<FileManager props={props} />);
  await user.click(await screen.findByRole("button", { name: /notes.txt/ }));
  const editor = await screen.findByRole("textbox", {
    name: "文件内容（UTF-8）",
  });
  await user.clear(editor);
  await user.type(editor, "新的内容");
  await user.click(screen.getByRole("button", { name: "保存到工作区" }));
  await waitFor(() =>
    expect(
      calls.some(({ method }) => method === "mutate_environment_file"),
    ).toBe(true),
  );
  expect(
    calls.find(({ method }) => method === "mutate_environment_file")?.body
      .payload,
  ).toEqual({
    resource_id: "environment",
    action: "write",
    spec: {
      path: file.path,
      text: "新的内容",
      expected_version: "original-sha",
    },
  });
});

it("uploads binary bytes directly into the selected real directory", async () => {
  backend();
  const user = userEvent.setup();
  render(<FileManager props={props} />);
  await user.click(await screen.findByRole("button", { name: /drafts/ }));
  const bytes = Uint8Array.from([0, 255, 137, 80, 78, 71]);
  const file = new File([bytes], "photo.png", { type: "image/png" });
  Object.defineProperty(file, "arrayBuffer", {
    value: async () => bytes.buffer,
  });
  await user.upload(
    screen.getByLabelText("上传本地文件到当前目录（最大 4 MiB）"),
    file,
  );
  await waitFor(() =>
    expect(
      calls.some(({ method }) => method === "mutate_environment_file"),
    ).toBe(true),
  );
  expect(
    calls.find(({ method }) => method === "mutate_environment_file")?.body
      .payload,
  ).toEqual({
    resource_id: "environment",
    action: "upload",
    spec: {
      path: "/workspace/drafts/photo.png",
      base64: btoa(String.fromCharCode(...bytes)),
      expected_version: "missing",
    },
  });
  expect(calls.some(({ method }) => method === "mutate_workspace")).toBe(false);
});

it("shows a file version read failure before deletion without inventing a command receipt", async () => {
  const methods: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const method = String(url).split("/").at(-1)!;
    methods.push(method);
    const body = JSON.parse(String(options?.body || "{}"));
    if (method === "read_environment" && body.section === "files")
      return new Response(
        JSON.stringify({
          data: { fields: { items: [file], total: 1, number: 1 } },
        }),
        { status: 200 },
      );
    return new Response(JSON.stringify({ problem: { code: "not_found" } }), {
      status: 404,
    });
  });
  const user = userEvent.setup();
  render(<FileManager props={props} />);
  await user.click((await screen.findAllByRole("button", { name: "删除" }))[0]);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "记录不存在、已过期，或不属于指定范围。",
  );
  expect(methods).not.toContain("mutate_environment_file");
  expect(screen.queryByText("请求编号与恢复")).not.toBeInTheDocument();
});
