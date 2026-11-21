# Relay Chat — LLM chatbot with key failover and response caching

A small, production-minded full-stack chat app: **React** UI, **FastAPI** backend, **SQLite** storage, and a **LiteLLM** gateway that rotates across a pool of API keys and caches responses. Every assistant reply shows how it was served: cache or live, which model, latency, and tokens for that request only.

```
 Browser (React, nginx)                FastAPI backend                         Provider
┌────────────────────┐   POST /chat   ┌──────────────────────────────────┐
│ chat + dispatch    │ ─────────────▶ │ validate → load history (SQLite) │
│ strip per reply    │                │ → LiteLLM gateway                │
│ failure simulator  │ ◀───────────── │    cache lookup ── hit ──▶ reply │
└────────────────────┘  reply + meta  │    miss → key_1 ─ fail ─▶ key_2 ─┼──▶ Gemini / any
                                      │ → save exchange (SQLite) → reply │     LiteLLM model
                                      └──────────────────────────────────┘
```

## 1. Live URLs

| | URL / status |
|---|---|
| Frontend | `https://lyftr-ai-assignment-frontend.vercel.app/` (deployed) |
| Backend | `https://lyftr-ai-assignment-g3ss.onrender.com` (deployed) |
| Health check | `https://lyftr-ai-assignment-g3ss.onrender.com/health` (verified `200`) |
| Demo video | PENDING — `<link>` |

Deployment health, CORS, real Gemini replies, cache hits, failover, and the all-keys-failed path were verified on the deployed backend. The demo video link is still pending. Local checks and their limits are listed in section 4.

### Deploying

**Backend (Render, Docker).** Create a *Web Service* from this repo with root directory `backend` (or use the included `render.yaml` Blueprint). Set these environment variables:

| Variable | Value |
|---|---|
| `LLM_API_KEYS` | your keys, comma-separated (at least 2) |
| `LLM_MODEL` | `gemini/gemini-3.5-flash-lite` (unprefixed `gemini-*` values are normalized automatically) |
| `ENABLE_FAILURE_SIMULATION` | `true` (required for the failover demo) |
| `ALLOWED_ORIGINS` | `http://localhost:3000,https://lyftr-ai-assignment-frontend.vercel.app` |
| `DATABASE_URL` | `sqlite:////data/app.db` (attach a disk at `/data` if your plan supports it) |

The container reads `$PORT`, so it also runs on Railway, Fly.io, or a Hugging Face Docker Space (set the Space's `app_port` to the port you expose).

**Frontend (Vercel / Netlify / Cloudflare Pages).** Root directory `frontend`, build command `npm run build`, output directory `dist`, and one environment variable: `VITE_API_URL=https://lyftr-ai-assignment-g3ss.onrender.com`. Then add the frontend URL to the backend's `ALLOWED_ORIGINS`.

> Hosting note: free web tiers usually have an ephemeral filesystem and sleep when idle. Without a persistent disk, SQLite history resets when the container is recreated. Locally, the Docker named volume keeps it. The first request after a free-tier sleep is slow, so open `/health` before recording the demo.

## 2. Run locally: with Docker and without

### With Docker

```bash
cp .env.example .env      # put your real keys in LLM_API_KEYS
make up                   # backend :8000, frontend :3000
make down                 # stop
```

`make up` creates `.env` from `.env.example` if it is missing. Other targets: `make logs`, `make test`, `make clean` (also deletes the SQLite volume).

### Without Docker

```bash
# backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export LLM_API_KEYS=key-one,key-two ALLOWED_ORIGINS=http://localhost:3000 ENABLE_FAILURE_SIMULATION=true
uvicorn app.main:get_app --factory --reload --port 8000

# frontend (second terminal)
cd frontend
npm install
VITE_API_URL=http://localhost:8000 npm run dev      # http://localhost:3000
```

### Configuration (all environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LLM_API_KEYS` | — | Comma-separated key pool. Ids are `key_1`, `key_2`, … in this order |
| `LLM_MODEL` | `gemini/gemini-3.5-flash-lite` | Any LiteLLM model string. Availability with real keys was not verified here; confirm before submission |
| `PORT` | `8000` | Backend listen port (Render/Railway/Fly inject their own) |
| `LLM_API_BASE` | empty | Optional OpenAI-compatible endpoint (use an `openai/<model>` string) |
| `DATABASE_URL` | `sqlite:///./data/app.db` (`sqlite:////data/app.db` in Docker) | SQLite file |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | CORS origins, comma-separated |
| `ENABLE_FAILURE_SIMULATION` | `false` | Turns on the `X-Simulate-Failure` header |
| `CACHE_ENABLED` | `true` | LiteLLM in-memory response cache |
| `KEY_COOLDOWN_SECONDS` | `60` | How long a really-failed key is skipped first |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Per-key provider timeout |
| `LLM_TEMPERATURE` | `0` | Sampling temperature (also part of the cache key); omitted for models that reject it, including Gemini 3 |
| `LLM_MAX_OUTPUT_TOKENS` | empty | Optional provider cap (also part of the cache key) |
| `DEFAULT_SYSTEM_PROMPT` | built-in prompt | Default used when a session has no override; a current-date instruction is prepended at runtime |
| `MAX_HISTORY_MESSAGES` | `40` | Latest messages sent to the model (full history stays in SQLite) |
| `API_URL` / `VITE_API_URL` | `http://localhost:8000` | Backend URL for the frontend (Docker runtime / static-host build time) |

### Tests

```bash
make test          # or: cd backend && pytest -q
```

45 tests cover the API contract, validation, history, sessions, persistence, CORS, failover (simulated and real HTTP 404 / 429 / quota / 5xx), cache hits and misses, per-request token accounting, provider-error logging, key redaction, Gemini model normalization, unsupported-parameter handling, and date-aware prompts. They run against a local fake OpenAI-compatible provider, so they need no network and no API keys, yet they still go through the real LiteLLM code path. Real Gemini behavior was checked separately on the deployed backend; Docker was not verified here.

## 3. Design decisions: key fallback, caching, SQLite session storage

### Key fallback

- **Ordered pool with deterministic priority.** Keys are tried in the order given. The answering key is reported as `key_id` (never the key itself), and `fallback_attempts` counts the keys that failed first.
- **What counts as a failure.** Any provider error: HTTP 429, quota or resource-exhausted, 5xx, timeouts, connection errors, and also auth errors. Real Gemini status mappings were not verified in this environment; the gateway therefore fails over on every LiteLLM exception, so a bad key cannot break the app even if its provider reports a 4xx. If every key fails the API returns `503 {"detail": "all providers unavailable"}` and **nothing is written to history**.
- **Cooldown.** A key that really failed is parked for `KEY_COOLDOWN_SECONDS`, so following requests skip it instead of paying for a doomed round trip. Parked keys are still tried last, so the app never gives up while a key might work. Simulated failures never park a key: they are per-request only.
- **Why a loop around `litellm.acompletion` instead of `litellm.Router`.** LiteLLM supplies the provider abstraction and maps every provider error to a uniform exception class. The explicit loop adds what the contract requires: deterministic priority, per-request attribution (`key_id`, `fallback_attempts`) and an in-request failure hook. The trade-off is that I own the retry logic; the cooldown above is a small version of what a Router would do.
- **Failure simulation.** `X-Simulate-Failure: key_1=429,key_2=500` (or `*=429`) raises the same LiteLLM exception class a real provider response would, inside the normal failover path, and only when `ENABLE_FAILURE_SIMULATION=true`. A simulated request **skips the cache lookup**, so the failover demo works even for a question that was answered before. Without this, `*=429` on a cached question would return a cache hit instead of the expected 503.

### Caching

- LiteLLM's built-in cache, in memory. The cache key is the model plus the **full message list** sent to the model (system prompt + history + new message) plus sampling parameters. The API key is not part of the key, so a reply generated with `key_1` is a hit for any later request.
- Consequences that match the contract: the same first message in a new session is a hit; a different message, different history or different system prompt is a miss. A hit returns the identical reply text and the usage stored with it, with `key_id: null` and `fallback_attempts: 0`.
- Two details found while testing LiteLLM 1.110.2:
  1. LiteLLM writes cache entries in a background task, so an immediate repeat request could miss. The gateway also writes the entry explicitly after a live call.
  2. The first cache hit in a process loads several modules lazily (~150 ms, slower than a live call). A start-up warm-up serves one throw-away hit so real hits are fast from the first request.
- Limits: the cache is per process and resets on restart (history in SQLite does not). Run a single worker; to share a cache across workers use a Redis cache, which the assignment excludes. The disk cache type is not available in the pinned LiteLLM version, so I used the in-memory one the assignment allows.

### SQLite session storage

- `messages` follows the required schema (plus an index on `(session_id, id)`); a small `sessions` table stores the per-session system prompt. Schema is created on startup. WAL mode is on.
- The **client sends only the new message**. The backend loads the session history, prepends the system prompt, calls the gateway, and then writes the user message and the assistant message **in one transaction**. A failed request (422 or 503) therefore never leaves a half exchange behind.
- A per-session lock keeps two quick messages in the same chat in order.
- The model sees the last `MAX_HISTORY_MESSAGES` messages, always starting on a user turn. The history endpoint returns the full stored history, oldest first, without the system prompt.
- `latency_ms` is measured server-side from request start to the model result and rounded **up** to whole milliseconds, so a sub-millisecond cache hit reads `1`, not `0`.

### Errors and security

Every error is JSON with `detail`: 422 for validation or malformed JSON, 404 for an unknown session, 503 when every key fails, and a generic `500 {"detail": "internal server error"}` for anything unexpected, with no stack trace or HTML page. Keys are only read from the environment, are never returned or logged, and `.env` is git-ignored. Docker images are multi-stage, the backend runs as a non-root user, and CORS is limited to `ALLOWED_ORIGINS`.

## 4. Verification: how to see failover, cache hits and per-message token usage

### What was verified here

- Local: 45 backend pytest tests against the fake OpenAI-compatible provider; frontend `npm run build` and Vitest/RTL component tests.
- Local browser checks reported by the owner: live reply, cache hit, failover to `key_2`, and the all-keys-failed banner at desktop and mobile widths.
- Verified deployment checks: `/health` returned `200` with two keys configured, the database reachable, failure simulation enabled, the normalized `gemini/gemini-3.5-flash-lite` model, and CORS allowed the deployed frontend origin.
- Verified live Gemini checks: a live reply used `key_1`; the same first prompt in a new session returned `LLM_CACHE` with a lower latency; simulated `key_1=429` answered from `key_2`; simulated `*=429` returned `503` and stored no session history.
- Not verified here: Docker build/restart persistence or the final recorded demo.

Open the frontend; the sidebar has a **Simulate a provider failure** menu (shown when the backend has `ENABLE_FAILURE_SIMULATION=true`). Or use curl:

```bash
API=https://lyftr-ai-assignment-g3ss.onrender.com

# health
curl -s $API/health
# {"status":"ok","keys_configured":2,"cache_enabled":true, ...}

# 1. live answer: LIVE_INFERENCE, key_1, fallback_attempts 0
curl -s $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"s-demo-1","message":"What is the capital of France?"}'

# 2. cache hit: same first message, NEW session -> LLM_CACHE, key_id null, faster
curl -s $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"s-demo-2","message":"What is the capital of France?"}'

# 3. failover: key_1 "returns 429" -> answered by key_2, fallback_attempts 1
curl -s $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"s-demo-3","message":"Name a prime number."}'

# 4. every key fails -> 503 {"detail":"all providers unavailable"}, nothing stored
curl -si $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"s-demo-4","message":"Will this work?"}'
curl -s $API/sessions/s-demo-4/messages        # {"detail":"session not found"}

# 5. history, oldest first, no system prompt
curl -s $API/sessions/s-demo-1/messages

# 6. validation: 422
curl -si $API/chat -H 'Content-Type: application/json' -d '{"session_id":"s-x","message":"   "}'
```

**Token usage.** Each reply in the UI shows the `input_tokens` and `output_tokens` of *that request only*. Send two follow-ups in one chat: input tokens grow because the prompt includes history, while output tokens reflect only the new reply. Two equal-length first messages in two new sessions report the same input tokens.

### Demo script (2–3 minutes)

0:00–0:20 — Open the live frontend and `<backend>/health`; say the status, key count and cache flag out loud.
0:20–0:50 — Ask “What is the capital of France?”; point to `LIVE_INFERENCE`, `key_1`, latency and per-request tokens.
0:50–1:15 — New chat, ask the same question; point to `LLM_CACHE`, `key_id: null` and the lower latency.
1:15–1:50 — Follow up with “What country is that in?” or “What did I just ask?”; explain that only the new message was sent and input tokens grew because history was included.
1:50–2:15 — Select `key_1 rate-limited (429)` and send a new question; show the answer from `key_2` with `fallback_attempts: 1`.
2:15–2:40 — Select `Every key rate-limited`; show the readable error banner, then curl the history endpoint for that session to show `404 {"detail":"session not found"}`.
2:40–3:00 — curl a 422 with an empty message and close with the repository/README links.

## 5. Setup Used: your editor and AI tools

- Editor: opencode CLI in the terminal; Divyanshu's desktop editor name was not specified and should be filled in before submission.
- AI tools: Claude (Anthropic) and opencode were used as coding assistants for scaffolding, code changes, tests and this README. Divyanshu reviewed the work and ran the local backend/frontend/browser checks. Docker was not verified here; deployment health, CORS, and real Gemini behavior were checked on the deployed services.
- Stack: Python 3.12, FastAPI, LiteLLM 1.110.2, SQLite, React 18 + Vite + TypeScript, nginx, Docker Compose.

## 6. Bonus features (if any): what you added beyond the requirements and why

- **Dispatch strip on every reply.** Shows the routing at a glance: `cache`, or `1 key failed → key_2`. It makes failover and caching visible without opening the network tab.
- **In-UI failure simulator** for the demo, sending the `X-Simulate-Failure` header.
- **Key cooldown** so a dead key stops costing a round trip per request.
- **Per-session system prompt**, persisted in SQLite.
- **Runtime frontend config** (`API_URL` read at container start), so one image works in any environment.
- **Readable errors** in the chat for 422, 503 and network failures; Markdown rendering; responsive layout with dark mode.
- **Degraded `/health`** (503) when the database is unreachable or no keys are loaded.
- **Hermetic test suite** against a fake provider, so CI needs no keys.
