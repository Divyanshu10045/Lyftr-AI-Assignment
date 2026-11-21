import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  class ApiError extends Error {
    constructor(message: string, public status: number) {
      super(message);
    }
  }
  return {
    ApiError,
    fetchHealth: vi.fn(),
    sendChat: vi.fn(),
  };
});

vi.mock("./api", () => ({
  ApiError: mocks.ApiError,
  fetchHealth: mocks.fetchHealth,
  sendChat: mocks.sendChat,
}));

import App from "./App";
import { AssistantMessage } from "./components";
import type { ChatMetadata } from "./types";

const liveMeta: ChatMetadata = {
  resolution_source: "LIVE_INFERENCE",
  model: "gemini/gemini-3.5-flash-lite",
  latency_ms: 812,
  input_tokens: 42,
  output_tokens: 9,
  key_id: "key_1",
  fallback_attempts: 0,
};

const cacheMeta: ChatMetadata = {
  resolution_source: "LLM_CACHE",
  model: "gemini/gemini-3.5-flash-lite",
  latency_ms: 1,
  input_tokens: 42,
  output_tokens: 9,
  key_id: null,
  fallback_attempts: 0,
};

beforeEach(() => {
  mocks.fetchHealth.mockResolvedValue({
    status: "ok",
    keys_configured: 2,
    cache_enabled: true,
    failure_simulation: false,
  });
  mocks.sendChat.mockReset();
});

describe("Relay Chat", () => {
  it("sends a message, clears the input and renders live metadata", async () => {
    const user = userEvent.setup();
    mocks.sendChat.mockResolvedValue({
      session_id: "s-test",
      reply: "The capital is Paris.",
      metadata: liveMeta,
    });

    render(<App />);
    await user.type(screen.getByTestId("chat-input"), "What is the capital of France?");
    await user.click(screen.getByTestId("send-button"));

    await waitFor(() => expect(screen.getByTestId("assistant-message")).toBeInTheDocument());
    expect(mocks.sendChat).toHaveBeenCalledWith(
      expect.objectContaining({ message: "What is the capital of France?" }),
    );
    expect(screen.getByTestId("chat-input")).toHaveValue("");
    expect(screen.getByTestId("meta-source")).toHaveTextContent("LIVE_INFERENCE");
    expect(screen.getByTestId("meta-model")).toHaveTextContent("gemini/gemini-3.5-flash-lite");
    expect(screen.getByTestId("meta-latency")).toHaveTextContent("812 ms");
    expect(screen.getByTestId("meta-input-tokens")).toHaveTextContent("42");
    expect(screen.getByTestId("meta-output-tokens")).toHaveTextContent("9");
  });

  it("renders the metadata strip for a cache hit", () => {
    render(<AssistantMessage text="The capital is Paris." meta={cacheMeta} />);

    expect(screen.getByTestId("meta-source")).toHaveTextContent("LLM_CACHE");
    expect(screen.getByTestId("meta-model")).toHaveTextContent("gemini/gemini-3.5-flash-lite");
    expect(screen.getByTestId("meta-latency")).toHaveTextContent("1 ms");
    expect(screen.getByTestId("meta-input-tokens")).toHaveTextContent("42");
    expect(screen.getByTestId("meta-output-tokens")).toHaveTextContent("9");
  });

  it("shows a readable error banner and no assistant reply when chat fails", async () => {
    const user = userEvent.setup();
    mocks.sendChat.mockRejectedValue(new mocks.ApiError("All AI providers are unavailable right now.", 503));

    render(<App />);
    await user.type(screen.getByTestId("chat-input"), "Will this work?");
    await user.click(screen.getByTestId("send-button"));

    expect(await screen.findByTestId("error-message")).toHaveTextContent(
      "All AI providers are unavailable right now.",
    );
    expect(screen.queryByTestId("assistant-message")).not.toBeInTheDocument();
  });
});
