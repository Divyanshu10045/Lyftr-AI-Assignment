import type { ChatResponse, Health } from "./types";

declare global {
  interface Window {
    __CONFIG__?: { API_URL?: string };
  }
}

const raw =
  window.__CONFIG__?.API_URL || (import.meta.env.VITE_API_URL as string | undefined) || "http://localhost:8000";
export const API_URL = raw.replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

/** Turn FastAPI's `{detail: ...}` (string or validation list) into a sentence a person can act on. */
function readableDetail(status: number, detail: unknown): string {
  if (status === 503) {
    return "All AI providers are unavailable right now. Your message was not saved, so you can send it again in a moment.";
  }
  if (status === 422) {
    if (Array.isArray(detail) && detail.length) {
      const msg = String((detail[0] as { msg?: string }).msg ?? "").replace(/^Value error, /, "");
      if (msg) return `That message can't be sent: ${msg}.`;
    }
    return "That message can't be sent. Check its length and try again.";
  }
  if (typeof detail === "string") return detail;
  return `The server returned an error (${status}).`;
}

export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const res = await fetch(`${API_URL}/health`, { signal });
  const body = await res.json().catch(() => ({}));
  if (!res.ok && !body.status) throw new ApiError("health check failed", res.status);
  return body as Health;
}

export async function sendChat(
  args: { sessionId: string; message: string; systemPrompt?: string; simulateFailure?: string },
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (args.simulateFailure) headers["X-Simulate-Failure"] = args.simulateFailure;

  let res: Response;
  try {
    res = await fetch(`${API_URL}/chat`, {
      method: "POST",
      headers,
      signal,
      body: JSON.stringify({
        session_id: args.sessionId,
        message: args.message,
        ...(args.systemPrompt?.trim() ? { system_prompt: args.systemPrompt.trim() } : {}),
      }),
    });
  } catch {
    throw new ApiError("Can't reach the server. Check your connection and try again.", 0);
  }

  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(readableDetail(res.status, body?.detail), res.status);
  return body as ChatResponse;
}
