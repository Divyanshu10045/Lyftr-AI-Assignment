import ReactMarkdown from "react-markdown";
import type { ChatMetadata } from "./types";

/** How this reply was routed: cache, or which key answered and how many failed first. */
export function Route({ meta }: { meta: ChatMetadata }) {
  if (meta.resolution_source === "LLM_CACHE") {
    return (
      <div className="route" aria-label="Served from cache, no provider call">
        <span className="hop hop-cache">cache</span>
        <span className="route-note">no provider call</span>
      </div>
    );
  }
  const failed = meta.fallback_attempts;
  return (
    <div
      className="route"
      aria-label={failed ? `${failed} key${failed > 1 ? "s" : ""} failed, answered by ${meta.key_id}` : `Answered by ${meta.key_id}`}
    >
      {failed > 0 && (
        <>
          <span className="hop hop-failed">
            {failed} key{failed > 1 ? "s" : ""} failed
          </span>
          <span className="route-arrow" aria-hidden="true" />
        </>
      )}
      <span className="hop hop-ok">{meta.key_id}</span>
      {failed > 0 && <span className="route-note">failover</span>}
    </div>
  );
}

function Stat({ label, testId, value }: { label: string; testId: string; value: string | number }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd data-testid={testId}>{value}</dd>
    </div>
  );
}

export function AssistantMessage({ text, meta }: { text: string; meta: ChatMetadata }) {
  const cached = meta.resolution_source === "LLM_CACHE";
  return (
    <article className="turn turn-assistant" data-testid="assistant-message">
      <div className="avatar" aria-hidden="true">R</div>
      <div className="turn-body">
        <div className="bubble prose">
          <ReactMarkdown>{text}</ReactMarkdown>
        </div>
        <div className="dispatch">
          <div className="dispatch-top">
            <span className={`source ${cached ? "source-cache" : "source-live"}`}>
              <i aria-hidden="true" />
              <span data-testid="meta-source">{meta.resolution_source}</span>
            </span>
            <Route meta={meta} />
          </div>
          <dl className="stats">
            <Stat label="model" testId="meta-model" value={meta.model} />
            <Stat label="latency" testId="meta-latency" value={`${meta.latency_ms} ms`} />
            <Stat label="input tokens" testId="meta-input-tokens" value={meta.input_tokens} />
            <Stat label="output tokens" testId="meta-output-tokens" value={meta.output_tokens} />
          </dl>
        </div>
      </div>
    </article>
  );
}
