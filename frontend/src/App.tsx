import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, fetchHealth, sendChat } from "./api";
import { AssistantMessage } from "./components";
import type { Health, NewTurn, Turn } from "./types";

const newSessionId = () => "s-" + Math.random().toString(16).slice(2, 10);

const SUGGESTIONS = [
  "What is the capital of France?",
  "Explain what a cache hit is in two sentences.",
  "Give me three tips for writing a clear commit message.",
];

const FAILURES = [
  { label: "None", value: "" },
  { label: "key_1 rate-limited (429)", value: "key_1=429" },
  { label: "key_1 server error (500)", value: "key_1=500" },
  { label: "key_1 429 + key_2 500", value: "key_1=429,key_2=500" },
  { label: "Every key rate-limited", value: "*=429" },
];

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId); // a new session on every page load
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false); // small screens only
  const nextId = useRef(1);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchHealth(ctrl.signal)
      .then((h) => (setHealth(h), setHealthError(false)))
      .catch(() => !ctrl.signal.aborted && setHealthError(true));
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, sending]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [input]);

  const push = useCallback((t: NewTurn) => {
    setTurns((prev) => [...prev, { ...t, id: nextId.current++ }]);
  }, []);

  const send = useCallback(
    async (raw?: string) => {
      const message = (raw ?? input).trim();
      if (!message || sending) return;
      setInput("");
      setSending(true);
      push({ kind: "user", text: message });
      try {
        const res = await sendChat({
          sessionId,
          message,
          systemPrompt,
          simulateFailure: health?.failure_simulation ? simulate : "",
        });
        push({ kind: "assistant", text: res.reply, meta: res.metadata });
      } catch (err) {
        push({ kind: "error", text: err instanceof ApiError ? err.message : "Something went wrong. Try again." });
      } finally {
        setSending(false);
        inputRef.current?.focus();
      }
    },
    [input, sending, sessionId, systemPrompt, simulate, health, push],
  );

  const newChat = () => {
    setSessionId(newSessionId());
    setTurns([]);
    setInput("");
    inputRef.current?.focus();
  };

  const status = useMemo(() => {
    if (healthError) return { tone: "bad", text: "Backend unreachable" };
    if (!health) return { tone: "idle", text: "Connecting…" };
    if (health.status !== "ok") return { tone: "bad", text: "Backend degraded" };
    return {
      tone: "good",
      text: `${health.keys_configured} keys · cache ${health.cache_enabled ? "on" : "off"}`,
    };
  }, [health, healthError]);

  return (
    <div className="shell">
      <aside className={`rail${settingsOpen ? " rail-open" : ""}`} aria-label="Session settings">
        <div className="brand">
          <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="8" fill="var(--ink)" />
            <path d="M8 22V10h6a4 4 0 010 8h-3m5 4l-3-4" fill="none" stroke="#7fb0ff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <div>
            <h1>Relay Chat</h1>
            <p>Every reply shows how it was served.</p>
          </div>
          <button
            type="button"
            className="btn-quiet settings-toggle"
            aria-expanded={settingsOpen}
            onClick={() => setSettingsOpen((v) => !v)}
          >
            Settings
          </button>
        </div>

        <div className={`status status-${status.tone}`} role="status">
          <i aria-hidden="true" />
          {status.text}
        </div>

        <section className="panel">
          <h2>Session</h2>
          <div className="session-row">
            <code title="Session id">{sessionId}</code>
            <button className="btn-quiet" onClick={newChat} type="button">
              New chat
            </button>
          </div>
          <p className="hint">History is stored on the server. Reloading the page starts a new session.</p>
        </section>

        <section className="panel panel-extra">
          <label htmlFor="sys" className="panel-title">System prompt</label>
          <textarea
            id="sys"
            data-testid="system-prompt-input"
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            rows={4}
            placeholder="Optional. Overrides the default for this session, e.g. “Answer like a pirate.”"
          />
        </section>

        {health?.failure_simulation && (
          <section className="panel panel-lab panel-extra">
            <label htmlFor="sim" className="panel-title">Simulate a provider failure</label>
            <select id="sim" value={simulate} onChange={(e) => setSimulate(e.target.value)}>
              {FAILURES.map((f) => (
                <option key={f.value} value={f.value}>{f.label}</option>
              ))}
            </select>
            <p className="hint">
              Sent as <code>X-Simulate-Failure</code> with each message while selected. It affects only that request.
            </p>
          </section>
        )}
      </aside>

      <main className="chat">
        <div className="scroll" role="log" aria-live="polite" aria-label="Conversation">
          <div className="column">
            {turns.length === 0 && (
              <div className="empty">
                <h2>Ask anything, then ask it again.</h2>
                <p>
                  Send a question, open a new chat and send the same question: the second answer comes from the cache
                  and the strip under it says so.
                </p>
                <div className="chips">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} type="button" className="chip" onClick={() => send(s)} disabled={sending}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {turns.map((t) =>
              t.kind === "user" ? (
                <article key={t.id} className="turn turn-user">
                  <div className="bubble">{t.text}</div>
                </article>
              ) : t.kind === "assistant" ? (
                <AssistantMessage key={t.id} text={t.text} meta={t.meta} />
              ) : (
                <div key={t.id} className="turn-error" role="alert" data-testid="error-message">
                  <svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true">
                    <path d="M10 2l8.5 15h-17L10 2zm0 5.5v4.5m0 2v.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  <span>{t.text}</span>
                </div>
              ),
            )}

            {sending && (
              <div className="turn turn-assistant" aria-label="Waiting for a reply">
                <div className="avatar" aria-hidden="true">R</div>
                <div className="typing"><span /><span /><span /></div>
              </div>
            )}
            <div ref={endRef} />
          </div>
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <div className="composer-inner">
            <textarea
              ref={inputRef}
              data-testid="chat-input"
              value={input}
              rows={1}
              maxLength={4000}
              placeholder="Message Relay Chat"
              aria-label="Message"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void send();
                }
              }}
              autoFocus
            />
            <button
              type="submit"
              className="send"
              data-testid="send-button"
              disabled={sending || !input.trim()}
              aria-label="Send message"
            >
              Send
            </button>
          </div>
          <p className="composer-hint">Enter to send · Shift+Enter for a new line</p>
        </form>
      </main>
    </div>
  );
}
