# OrbitOps Assistant — Compass

A single live chat app combining two COMP2701 assignments into one portfolio project:

- **RAG pipeline** (Assessment 1): BM25 + dense embeddings + ChromaDB, fused with
  Reciprocal Rank Fusion, answering questions grounded in your OrbitOps docs —
  plus up to 10 uploaded documents per chat, indexed on top of the base corpus.
- **Domain Intelligence Agent** (Assessment 2): a LangGraph agent that calls the
  real GitHub and HackerNews APIs (plus optional Google Custom Search), with a
  regex guardrail against prompt injection and an LLM-as-judge eval suite.

Both run as their own pipeline, joined by a thin keyword-first dispatcher — not
one merged graph — so each half stays independently testable. Everything is
free/open-source: the LLM is `qwen2.5:7b` running locally via Ollama, no paid
API required.

## Fixes made while combining them

- **Agent empty-results bug** (flagged in the original eval report — ADV3, E1,
  E2 all returned a 0-word response): `fetch_node` was setting
  `current_node='end'` on *any* empty result, which made `route_after_fetch`'s
  very first check intercept it before the retry/explain logic ever ran. Fixed
  by using a neutral marker so retries (up to 3 attempts) and a proper
  "no results found" message actually get a chance to run.
- **Multi-turn memory gap**: `run_agent()` always built a brand-new initial
  state and never read back the LangGraph checkpoint, so despite `MemorySaver`
  being wired up, no node ever saw a previous turn. Fixed by loading prior
  state via `get_state()` and carrying forward `chat_history` across calls on
  the same `thread_id`.
- **Chroma metadata bug** (found while adding document uploads): Chroma
  rejects `None` metadata values, which an uploaded doc with no header fields
  would produce. Fixed by coercing `None` → `""` before indexing.

## Safety and security

Added once the plan became "multiple strangers will reach this over the
internet," not just "my own dev machine":

- **Prompt-injection guardrail on both pipelines, covering the broader known
  attack categories** — not just one phrase. The Agent originally only
  caught "ignore previous instructions"; the RAG path had no guardrail at
  all. One shared guardrail (`guardrails.py`) now runs on both and detects:
  persona switching ("you are now a...", "pretend to be..."), prompt
  template extraction ("print your instructions"), ignoring the template,
  conversation-history extraction, template augmentation/reset, fake
  chat-turn injection (prefill/fake-completion attacks impersonating a
  prior Assistant turn), output-format bypass attempts ("respond in
  base64"), and obfuscated input (leetspeak like "1gn0re", and base64-
  encoded instructions — both decoded/normalised before matching). Not
  caught, because no regex reliably can: synonym-rephrased attacks ("ignore"
  → "pay attention to" with no other signal) and a politely-worded request
  exploiting the model's tendency to trust friendly phrasing. Those need
  semantic judgment, not pattern matching — the actual backstop for them is
  that both pipelines' prompts constrain the model to answer only from
  supplied context/data, which limits what even a successful injection can
  make it do.
- **Anti-hallucination check on both pipelines' answers.** A lightweight
  faithfulness heuristic (the same one from the A1 notebook's eval section)
  checks whether the model's answer is actually grounded in the
  retrieved/sourced material. If the overlap is too low, a visible notice is
  appended rather than presenting the answer as fully verified. This is a
  heuristic, not a guarantee — it catches obvious drift, not subtle errors.
- **`thread_id` validation.** The frontend's `thread_id` has no login behind
  it and goes straight into a Chroma collection name and SQLite rows. Every
  endpoint now requires it to actually be a UUID this app would have
  generated, not an arbitrary client-supplied string.
- **Rate limiting.** 20 chat messages / 60s and 10 uploads / 60s per thread
  (`rate_limit.py`, in-memory). This exists because the backend runs one
  shared Ollama worker and a global Google quota — without this, one user (or
  one retry-happy script) can stall the whole app for everyone else.
- **Upload size cap** (5MB) and file-type allowlist (`.txt`/`.md`/`.docx`/
  `.pdf` only), enforced server-side regardless of what the frontend sends.
- **CORS restricted** to configured origins (`FRONTEND_ORIGINS` env var,
  defaults to localhost) instead of `*`.

What this does *not* cover: this is still no-login, so anyone who learns a
`thread_id` can read/continue that chat — an accepted trade-off discussed
earlier, not a bug. It also doesn't defend against a determined attacker
with many IPs (the rate limiter is per-thread, in-memory, single-process);
that's out of scope for a free-tier portfolio deployment.

## Architecture decisions made for this project

- Document uploads are **added on top of** the base OrbitOps corpus, not a
  replacement, capped at 10 per chat session.
- Google Search quota (100/day, free tier) is **global**, not per-user — it's
  tied to one API key. When exhausted, only the Google tool is disabled for
  the rest of the day; RAG + GitHub/HackerNews keep working.
- No login: each browser gets a random `thread_id` stored in `localStorage`,
  used to return to previous chats. Chat history is persisted server-side in
  SQLite (not just the in-memory LangGraph checkpointer) so it survives a
  backend restart.

## Running locally

### 1. Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen2.5:7b
```

### 2. Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Copy your Assessment 1 `data/docs/*.txt` files into `backend/app/data/docs/` —
that's the base OrbitOps corpus the RAG pipeline indexes at startup.

Optional — enable Google Search by copying `.env.example` to `.env` and
filling in a free Google Custom Search API key + Search Engine ID. Leave it
blank to run with GitHub + HackerNews only.

```bash
uvicorn app.main:app --reload --port 8000
```

If your frontend runs anywhere other than `localhost:5173`/`127.0.0.1:5173`
(e.g. once deployed), set `FRONTEND_ORIGINS` to its real origin before
starting uvicorn, comma-separated if there's more than one:

```bash
export FRONTEND_ORIGINS="https://your-deployed-frontend.example.com"
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open the printed `localhost` URL. Set `VITE_API_URL` in a `.env` file in
`frontend/` if the backend isn't on `localhost:8000`.

## Deploying for free

Not yet decided — options discussed: Oracle Cloud's Always Free ARM VM
(24GB RAM, enough to run Ollama + the backend + frontend together,
always-on with a real public URL) or your own PC exposed via a free
Cloudflare Tunnel (free, but only live while your PC is on).
