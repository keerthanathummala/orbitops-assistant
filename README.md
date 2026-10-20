# OrbitOps Assistant

INFS3208 Cloud Computing individual project (Type I: scalable web
application), by Keerthana Thummala.

OrbitOps Assistant is a chat assistant for a platform operations team,
deployed as microservices on Google Kubernetes Engine. It answers questions
from the team's runbooks, researches GitHub and HackerNews, and watches the
cluster it runs on: it can explain a failing pod from live events and logs,
or run a one-click health check that writes an incident summary with next
steps. Users sign in with their own accounts, and an admin page shows who
used the app, when and from where.

## Prior work and what is new for INFS3208

The chat app this project starts from is my own earlier work, built for
COMP2701 (a document Q&A pipeline and a research agent, joined in one
local chat app). It is reused here as the workload the cloud system runs,
and is disclosed so the marking is clear about what was done for this
course. The git history shows the split: everything after commit `a58e69b`
was written for INFS3208.

| Reused from COMP2701 (the starting point) | Built for INFS3208 |
|---|---|
| RAG pipeline: BM25 + MiniLM embeddings + Chroma, fused with Reciprocal Rank Fusion (`rag_core.py`) | Containerised services (`backend/Dockerfile`, `frontend/Dockerfile`) and `docker-compose.yml` |
| LangGraph research agent with GitHub, HackerNews and Google tools (`agent_core.py`) | Kubernetes manifests for GKE (`k8s/`): Deployments, StatefulSets with persistent disks, Services, a public load balancer, HPAs, disruption budgets, RBAC |
| Keyword dispatcher, guardrails, document upload, React chat UI | Split into four services: frontend, chat, auth and ops, routed by nginx |
| Single process, SQLite and on-disk Chroma, local Ollama model | Stateless replicas: shared MySQL, Chroma over HTTP and Redis rate limits, so any pod can serve any request |
| | Hosted Gemini API behind a provider switch (`llm.py`), with a clear message when the shared quota runs out |
| | AIOps: cluster questions, log explainer and health check (`cluster_ops.py`), reading the cluster through a read-only service account |
| | User accounts, sessions and the admin page (`auth.py`, `auth_api.py`) |
| | A runbook corpus for the platform (OO-001 to OO-012), uploads that can be removed, Markdown answers, light and dark theme |
| | Health and readiness probes, startup warm-up, rolling updates and rollback, node-failure handling, `commands.sh` |

## Architecture

```
Browser ─> GCP load balancer ─> frontend (nginx + React)      2-4 pods
                                  ├ /api/auth, /api/admin ─> auth     2-4 pods   accounts, admin page
                                  ├ /api/ops ──────────────> ops      1 pod      health check, reads the cluster
                                  └ /api/* ────────────────> backend  2-5 pods   chat, history, uploads
                                                                 └ cluster questions ─> ops
            MySQL (accounts, chats, uploads) · Chroma (vectors) · Redis (rate limits)
            Gemini API · GitHub / HackerNews APIs · Kubernetes API (ops only, read-only)
```

- **Microservices.** Four app services and three stateful ones, each with
  its own Kubernetes Service. The chat, auth and ops services share one
  image and one database but run as separate Deployments: a load spike on
  chat can't stop people signing in, and only the ops pods can read the
  cluster, so the pods that handle user prompts have no Kubernetes access.
- **Load balancing.** A GCP external load balancer in front of the nginx
  pods, and Kubernetes Services spreading requests over each service's
  replicas. The footer shows which pod and version answered.
- **Scaling.** HPAs on frontend, chat and auth (CPU target 60-70%), and the
  GKE cluster autoscaler adds nodes (2 to 4) when pods don't fit.
- **Self-healing and node failure.** Liveness, readiness and startup
  probes; replicas spread over nodes; PodDisruptionBudgets keep at least one
  pod of each service running when a node is drained.
- **Rolling updates and rollback.** `maxUnavailable: 0`, so a new version
  replaces pods one at a time with no downtime, and `rollout undo` goes back.
- **Persistence.** MySQL and Chroma run as StatefulSets with persistent
  disks, so accounts, chats and uploads survive pod restarts.

[DEPLOY.md](DEPLOY.md) has the full deployment guide and the 4-minute demo,
and [`commands.sh`](commands.sh) has every gcloud and kubectl command with a
comment on each.

## Security

- **Accounts.** Passwords are stored as salted scrypt hashes. A session is a
  random bearer token of which only the sha256 is stored, valid for 7 days
  and shared by every replica through the database. Everything except the
  health probes needs a session, a chat can only be read by the account that
  created it, and sign-in attempts are rate-limited per IP.
- **Admin page.** The account set by `ADMIN_USERNAME` / `ADMIN_PASSWORD`
  sees every account, sign-in times, IP addresses and browsers, and can
  delete accounts. No endpoint lets the admin read anyone's chats.
- **Least privilege on the cluster.** Only the ops service account can read
  the namespace (`k8s/22-rbac.yaml`), and only read. The other pods don't
  mount a service account token at all. The services' `/internal/` calls to
  each other aren't reachable from outside: nginx only proxies `/api/`.
- **Secrets.** API keys and passwords live in a Kubernetes Secret created
  from a git-ignored file (`k8s/secrets.yaml`), never in the image or repo.
- **Prompt-injection guardrail** on every chat message (persona switching,
  prompt extraction, fake chat turns, obfuscated and base64 input), and a
  faithfulness check that flags answers not grounded in their sources.
- **Input limits.** `thread_id` must be a UUID, uploads are capped at 5MB
  and to `.txt`/`.md`/`.docx`/`.pdf`, chat and uploads are rate-limited per
  thread through Redis, and CORS is restricted to configured origins.

Not covered: email verification and password reset, and the IP shown to
the admin is where the request came from, not a verified location.

## Running locally

The quickest full run is Docker Compose, which starts all seven services:

```bash
cp backend/.env.example backend/.env   # set GEMINI_API_KEY and ADMIN_PASSWORD
docker compose up --build
# open http://localhost:8080
```

Without Docker, one backend process serves all three backend services
(chat, auth and ops), with SQLite, an on-disk Chroma store and an in-memory
rate limiter:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                    # set GEMINI_API_KEY and ADMIN_PASSWORD
uvicorn app.main:app --reload --port 8000 --env-file .env

cd ../frontend
npm install
npm run dev                             # open the printed localhost URL
```

A free Gemini key comes from https://aistudio.google.com/apikey. Set
`LLM_PROVIDER=ollama` to use a local Ollama model instead. Sign in with
`ADMIN_USERNAME` / `ADMIN_PASSWORD` and click **Admin** under your name to
open the admin page. Outside a cluster the AIOps features report that
cluster access isn't configured, and cluster questions are answered from
the runbooks.

## Repository layout

```
backend/app/
  main.py           chat service: chat, history, uploads
  auth_service.py   auth service (routes in auth_api.py, logic in auth.py)
  ops_service.py    ops service (routes in ops_api.py, logic in cluster_ops.py)
  common.py         probes, CORS and ownership checks shared by the services
  dispatcher.py     sends each message to docs, research, both, or ops
  rag_core.py       document Q&A (reused, extended)
  agent_core.py     research agent (reused, extended)
  store.py          MySQL / SQLite: accounts, sessions, chats, uploads
  llm.py            Gemini or Ollama
  data/docs/        the runbook corpus, baked into the image
frontend/           React app and nginx config
k8s/                Kubernetes manifests, applied in file-name order
commands.sh         every gcloud / kubectl command, commented
DEPLOY.md           deployment guide and demo script
```
