# Deploying OrbitOps to Google Kubernetes Engine

## Architecture

```
Browser ──> GCP external load balancer (Service type LoadBalancer)
              └─> frontend pods (nginx + React build)          2-4 replicas, HPA
                    ├─ /api/auth, /api/admin ─> auth pods      2-4 replicas, HPA   sign-up, sign-in, admin page
                    ├─ /api/ops ─────────────> ops pod         1 replica           health check; only pods that can read the cluster
                    └─ /api/* ───────────────> backend pods    2-5 replicas, HPA   chat, history, uploads (the "chat service")
                                                 └─ cluster questions ─> ops
   auth, backend and ops all use:
     mysql   StatefulSet + 5Gi disk   accounts, sessions, sign-in log, chats, uploads
     chroma  StatefulSet + 2Gi disk   vector store (backend, ops)
     redis   Deployment               shared rate limits
   backend and ops call the Gemini API; backend also GitHub / HackerNews / Google.
```

Seven services in all: four of ours (frontend, backend, auth, ops) and
three stateful ones (mysql, chroma, redis). The app pods are stateless:
everything a request needs lives in MySQL, Chroma or Redis, so any pod can
answer any request, and pods can be added, removed, moved to another node
or replaced (rolling update) without losing accounts, chats or uploads.

Every Deployment spreads its replicas across nodes
(`topologySpreadConstraints`), and frontend, backend and auth each have a
PodDisruptionBudget (`minAvailable: 1`), so draining a node (a GKE upgrade,
or the node-failure demo) never takes all their pods down at once. Ops,
MySQL, Chroma and Redis have one pod each and no budget: if their node is
drained they restart on another node, and sign-in, chat or cluster questions
(depending on which one moved) pause for about a minute.

Every command below is also in [`commands.sh`](commands.sh), one step at a
time and with a comment on each command: `./commands.sh setup`, `build`,
`cluster`, `deploy`, then the `demo-*` steps.

## 0. Before you start

- The runbook corpus in `backend/app/data/docs/` is baked into the backend
  image as the base corpus; add or edit `.txt` files there before building.
- Get a free Gemini API key at https://aistudio.google.com/apikey.
- Install the Google Cloud CLI and `kubectl`
  (`gcloud components install kubectl gke-gcloud-auth-plugin`).

## 1. Try it locally first (saves credit)

```bash
cp backend/.env.example backend/.env      # put GEMINI_API_KEY in it
docker compose up --build
# open http://localhost:8080 ; the footer shows which backend container answered
```

## 2. Project, registry and images

```bash
export PROJECT_ID=your-project-id
export REGION=australia-southeast1
gcloud config set project $PROJECT_ID
gcloud services enable container.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

gcloud artifacts repositories create orbitops --repository-format=docker --location=$REGION

# Cloud Build builds the images in GCP (no local Docker needed). The backend
# image runs the chat, auth and ops services, each with its own start command.
export REG=$REGION-docker.pkg.dev/$PROJECT_ID/orbitops
gcloud builds submit backend  --tag $REG/backend:v1  --timeout=30m
gcloud builds submit frontend --tag $REG/frontend:v1 --timeout=15m
# The same backend image tagged v2, ready for the rolling-update demo
gcloud artifacts docker tags add $REG/backend:v1 $REG/backend:v2
```

## 3. Cluster

```bash
gcloud container clusters create orbitops \
  --zone $REGION-a --machine-type e2-standard-2 \
  --disk-type pd-standard --disk-size 30 \
  --num-nodes 2 --enable-autoscaling --min-nodes 2 --max-nodes 4
gcloud container clusters get-credentials orbitops --zone $REGION-a
```

## 4. Deploy

```bash
# point the manifests at your registry
sed -i "s/PROJECT_ID/$PROJECT_ID/g" k8s/2*.yaml   # macOS: sed -i ''  (commands.sh does this without editing the files)

kubectl apply -f k8s/00-namespace.yaml
cp k8s/01-secrets.example.yaml k8s/secrets.yaml   # fill in passwords, GEMINI_API_KEY and ADMIN_PASSWORD (git-ignored)
kubectl apply -f k8s/secrets.yaml
kubectl apply -f k8s/02-configmap.yaml -f k8s/10-mysql.yaml -f k8s/11-chroma.yaml -f k8s/12-redis.yaml
kubectl apply -f k8s/22-rbac.yaml       # read-only cluster access, for the ops service only
kubectl apply -f k8s/20-backend.yaml -f k8s/21-frontend.yaml -f k8s/23-auth.yaml -f k8s/24-ops.yaml -f k8s/30-hpa.yaml

kubectl -n orbitops get pods -w                 # wait until everything is Running/Ready
kubectl -n orbitops get svc frontend            # EXTERNAL-IP is the app's public address
```

## 5. The 4-minute demo (Week 13)

Before the demo: deploy, open the app in a browser, sign in, and check that
`./commands.sh status` shows everything Ready. The Gemini free tier
allows only a few requests a minute and a daily total, shared by every
user, so avoid testing with it on demo day before the demo. The v2 image was already pushed by `build`, so
nothing has to build during the demo.

| Time | Show | Run | What to point at |
|---|---|---|---|
| 0:00 | Microservices | `./commands.sh demo-services` | frontend, backend (chat), auth and ops Deployments plus MySQL, Chroma and Redis, each with its own Service, spread over both nodes |
| 0:30 | Load balancing | `./commands.sh demo-lb`, and refresh the app | `pod` in the replies and the footer changes between backend pods |
| 0:50 | Autoscaling | `./commands.sh demo-scale` (Ctrl+C after the jump), then `demo-scale-stop` | 3 load pods × 4 loops push CPU past 60%; replicas climb from 2 towards 5, and the cluster adds a node if they don't fit |
| 1:50 | Node failure / self-healing | `./commands.sh demo-node-failure` | a node is drained; its pods are recreated on the other node, and the disruption budgets keep one frontend, chat and auth pod running, so the app keeps answering (the step picks a node without MySQL or Chroma when it can) |
| 2:40 | Rolling update and rollback | `./commands.sh demo-update`, then `demo-rollback` | pods replaced one at a time with no downtime; footer shows `version v2`, then `v1` again |
| 3:20 | AIOps incident | `./commands.sh demo-incident`, click **Run health check**, then `demo-recover` | the ops service flags the stuck rollout (ImagePullBackOff), explains it with the runbook and suggests `rollout undo`; after the undo it reports Healthy |

If there is time left: ask "why did the backend pod restart?" while signed
in as the admin (an ops answer from live pods and logs; other accounts get
the pod state without logs), show the admin page (sign-in times and
IPs, no chats), or `kubectl -n orbitops delete pod mysql-0` to show chats
survive a database restart.

## 6. Stop paying when you're done

```bash
gcloud container clusters delete orbitops --zone $REGION-a
```
Persistent disks from the StatefulSets may remain: check
`gcloud compute disks list` and delete them too.
