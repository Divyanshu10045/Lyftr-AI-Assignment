export type ResolutionSource = "LIVE_INFERENCE" | "LLM_CACHE";

export interface ChatMetadata {
  resolution_source: ResolutionSource;
  model: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  key_id: string | null;
  fallback_attempts: number;
}

export interface ChatResponse {
  session_id: string;
  reply: string;
  metadata: ChatMetadata;
}

export interface Health {
  status: string;
  keys_configured: number;
  cache_enabled: boolean;
  model?: string;
  failure_simulation?: boolean;
}

export type Turn =
  | { id: number; kind: "user"; text: string }
  | { id: number; kind: "assistant"; text: string; meta: ChatMetadata }
  | { id: number; kind: "error"; text: string };

/** `Omit` that keeps each member of the union intact. */
export type NewTurn =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; meta: ChatMetadata }
  | { kind: "error"; text: string };
