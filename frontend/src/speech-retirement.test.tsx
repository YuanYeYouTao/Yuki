import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Assets } from "./pages";
import { Chat } from "./chat";

it("keeps emoji management without fetching retired speech management", async () => {
  const methods: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    methods.push(String(url).split("/").pop()!);
    return new Response(
      JSON.stringify({ data: { items: [], next_cursor: null } }),
      {
        status: 200,
      },
    );
  });
  render(
    <Assets allowed={() => true} act={vi.fn()} refresh={0} conversation="" />,
  );
  expect(await screen.findByText("表情库")).toBeInTheDocument();
  expect(screen.queryByText("语音")).not.toBeInTheDocument();
  expect(methods).toContain("list_emoji_assets");
  expect(methods).not.toContain("list_speech_profiles");
});

it("continues to show incoming audio transcripts from saved chat history", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(
    async (url) =>
      new Response(
        JSON.stringify({
          data: {
            items: String(url).endsWith("list_chat_events")
              ? [
                  {
                    event_id: 41,
                    direction: "inbound",
                    content: "",
                    audio_transcript: "保存的接收语音转写",
                    occurred_at: "2026-10-07T00:00:00Z",
                  },
                ]
              : [],
            next_cursor: null,
          },
        }),
        { status: 200 },
      ),
  );
  render(
    <Chat conversation="fixture" content refresh={0} notebook={<div />} />,
  );
  expect(
    await screen.findByText("语音：保存的接收语音转写"),
  ).toBeInTheDocument();
});
