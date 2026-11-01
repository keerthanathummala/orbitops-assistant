# Hosting OrbitOps Assistant for free from GitHub

This gives the app a public link that stays up without a GKE cluster (and
without spending cloud credit). Every push to `main` on GitHub redeploys it
automatically. The only outside service it calls is the Gemini API, and the
Gemini key stays secret: it lives in GitHub's encrypted secrets and the
host's secrets, never in the repo, the image or the browser.

## How it works

```
push to main ─> GitHub Actions (.github/workflows/deploy-hf-space.yml)
                  └─> Hugging Face Space (free Docker host, 2 vCPU / 16 GB)
                        builds the root Dockerfile and runs ONE container:
                          FastAPI: chat + auth + ops + the built React app
                          SQLite, on-disk Chroma, in-memory rate limits
                            └─> Gemini API (key from the Space's secrets)
```

The root `Dockerfile` runs the backend as a single process, the same way it
runs locally without Docker (see README "Running locally"), and that process
also serves the React build. So there's no nginx, MySQL, Redis or Chroma
server to host. The Kubernetes setup in `k8s/` and `docker-compose.yml` is
unchanged and still the deployment for the INFS3208 demo.

Why Hugging Face Spaces: it's free, builds straight from a Dockerfile, and
gives 16 GB of RAM, which the embedding model (torch + MiniLM) needs. Free
tiers such as Render's (512 MB) run out of memory on this app.

## One-time setup (about 10 minutes)

1. **Hugging Face account.** Sign up at https://huggingface.co/join (free).
2. **Access token.** https://huggingface.co/settings/tokens → *Create new
   token* → type **Write** → copy it.
3. **Gemini key.** https://aistudio.google.com/apikey (free), if you don't
   already have one.
4. **GitHub secrets.** In this repo on GitHub: *Settings → Secrets and
   variables → Actions → New repository secret*, add:
   | Name | Value |
   |---|---|
   | `HF_TOKEN` | the Hugging Face token |
   | `GEMINI_API_KEY` | the Gemini key |
   | `ADMIN_PASSWORD` | a password for the app's `admin` account |

   Optional, under the *Variables* tab: `HF_SPACE` (e.g.
   `myname/orbitops-assistant`, the default) and `ADMIN_USERNAME` (default
   `admin`).
5. **Deploy.** *Actions → Deploy to Hugging Face Space → Run workflow* (or
   just push to `main`). The job log ends with the app's URL:
   `https://<hf-username>-orbitops-assistant.hf.space`.
6. **Wait for the build.** The first build on Hugging Face takes about 5-10
   minutes (installing torch and the embedding model). Watch it on the
   Space page's *Logs* tab. Later deploys reuse cached layers and are faster.

Then open the URL, sign up or sign in as `admin`, and chat.

## What's different from the GKE deployment

- **One replica, no autoscaling or load balancing.** Fine for a demo link;
  the scaling, self-healing and rolling-update story is the GKE deployment.
- **Data isn't permanent.** Accounts, chats and uploads are kept in the
  container, so they reset when the Space restarts or redeploys. The admin
  account is recreated from the secrets on every start. (Hugging Face's paid
  persistent storage would fix this; set `SQLITE_PATH` to a file under
  `/data`.)
- **It sleeps when idle.** A free Space sleeps after 48 hours without
  visitors; the next visit wakes it in a minute or two.
- **No cluster.** Cluster questions are answered from the runbooks and the
  health check reports "Unavailable", as when running locally.
- **Public URL.** Anyone with the link can sign up and use your Gemini free
  quota. Chat is rate-limited per account; make the Space private in its
  settings if you only want it for yourself.

## Running the same container yourself

```bash
docker build -t orbitops-hosted .
docker run -p 7860:7860 -e GEMINI_API_KEY=... -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD=... orbitops-hosted
# open http://localhost:7860
```

The image also works on any other Docker host that sets `PORT` (Cloud Run,
Railway, Fly.io), as long as it has about 2 GB of memory.
