# Deploying OrbitOps to Google Kubernetes Engine

## Architecture

```
Browser ──> GCP external load balancer (Service type LoadBalancer)
              └─> frontend pods (nginx + React build)   2-4 replicas, HPA
                    └─ /api ─> backend Service
                                 └─> backend pods (FastAPI)   2-5 replicas, HPA
                                       ├─> mysql   StatefulSet + 5Gi disk   chat history, threads, uploads
                                       ├─> chroma  StatefulSet + 2Gi disk   vector store
                                       ├─> redis   Deployment               shared rate limits
                                       ├─> Gemini API (hosted LLM), GitHub / HackerNews / Google APIs
                                       └─> Kubernetes API (read-only, own namespace) for the AIOps features
```

The backend pods are stateless: everything a request needs lives in MySQL,
Chroma or Redis, so any pod can answer any request, and pods can be added,
removed or replaced (rolling update) without losing chats or uploads.

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

# Cloud Build builds the images in GCP (no local Docker needed)
export REG=$REGION-docker.pkg.dev/$PROJECT_ID/orbitops
gcloud builds submit backend  --tag $REG/backend:v1
gcloud builds submit frontend --tag $REG/frontend:v1
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
sed -i "s/PROJECT_ID/$PROJECT_ID/g" k8s/20-backend.yaml k8s/21-frontend.yaml   # macOS: sed -i ''

kubectl apply -f k8s/00-namespace.yaml
cp k8s/01-secrets.example.yaml k8s/secrets.yaml   # fill in passwords + GEMINI_API_KEY (git-ignored)
kubectl apply -f k8s/secrets.yaml
kubectl apply -f k8s/02-configmap.yaml -f k8s/10-mysql.yaml -f k8s/11-chroma.yaml -f k8s/12-redis.yaml
kubectl apply -f k8s/22-rbac.yaml       # read-only cluster access for the AIOps features
kubectl apply -f k8s/20-backend.yaml -f k8s/21-frontend.yaml -f k8s/30-hpa.yaml

kubectl -n orbitops get pods -w                 # wait until everything is Running/Ready
kubectl -n orbitops get svc frontend            # EXTERNAL-IP is the app's public address
```

## 5. Demo script (Week 13)

| Show | Command | What to point at |
|---|---|---|
| Microservices | `kubectl -n orbitops get deploy,sts,svc,pods -o wide` | 6 services across nodes |
| Load balancing | refresh the app / `curl http://EXTERNAL-IP/api/health` a few times | footer and `pod` change between backend pods |
| Manual scaling | `kubectl -n orbitops scale deploy/backend --replicas=4` | new pods appear, history still there |
| Autoscaling | `kubectl -n orbitops run load --rm -it --image=busybox -- sh -c "while true; do wget -q -O- http://backend:8000/api/health; done"` then `kubectl -n orbitops get hpa -w` | replicas climb with CPU |
| Self-healing | `kubectl -n orbitops delete pod <a backend pod>` | replaced automatically, app stays up |
| Rolling update | build `:v2` (`gcloud builds submit backend --tag $REG/backend:v2`), change image tag and `APP_VERSION` to `v2` in `k8s/20-backend.yaml`, `kubectl apply -f k8s/20-backend.yaml`, `kubectl -n orbitops rollout status deploy/backend` | pods replaced one at a time; footer shows `version v2`; no downtime |
| Rollback | `kubectl -n orbitops rollout history deploy/backend` then `kubectl -n orbitops rollout undo deploy/backend` | footer back to `version v1` |
| Persistence | `kubectl -n orbitops delete pod mysql-0` | chats survive (persistent disk) |
| AIOps: healthy | click **Run health check** in the app | "Healthy", all pods ready |
| AIOps: incident | deploy a broken version: `kubectl -n orbitops set image deploy/backend backend=$REG/backend:does-not-exist`, then click **Run health check** | flags ImagePullBackOff / stuck rollout, explains it, suggests `rollout undo` with the runbook |
| AIOps: questions | ask "is anything failing?", "why did the backend pod restart?", "explain the backend errors" | answers from live pods, events and logs (OPS badge) |
| AIOps: recover | `kubectl -n orbitops rollout undo deploy/backend`, then health check again | back to Healthy |

## 6. Stop paying when you're done

```bash
gcloud container clusters delete orbitops --zone $REGION-a
```
Persistent disks from the StatefulSets may remain: check
`gcloud compute disks list` and delete them too.
