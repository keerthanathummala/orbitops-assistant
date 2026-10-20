#!/usr/bin/env bash
# Every gcloud and kubectl command used to build, deploy and demo OrbitOps
# Assistant on Google Kubernetes Engine, with a comment on what each one does.
#
# Run one step at a time:   ./commands.sh <step>
#   setup      project, APIs and the image registry
#   build      build the images with Cloud Build (v1, plus v2 for the demo)
#   cluster    create the GKE cluster
#   deploy     apply every Kubernetes manifest
#   status     what is running and the app's public address
#   demo-*     the Week 13 demo, in order (see DEPLOY.md "4-minute demo")
#   cleanup    delete the cluster and its disks so billing stops
#
# Change these two before the first run.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-your-project-id}"
REGION="${REGION:-australia-southeast1}"
ZONE="$REGION-a"
REG="$REGION-docker.pkg.dev/$PROJECT_ID/orbitops"
NS=orbitops

cd "$(dirname "$0")"

# The manifests say PROJECT_ID where the registry path goes; fill it in while
# applying so the files in the repository stay unchanged.
apply() {
  for f in "$@"; do
    # Send the manifest, with the real project id, to the cluster.
    sed "s/PROJECT_ID/$PROJECT_ID/g" "$f" | kubectl apply -f -
  done
}

case "${1:-}" in

setup)
  # Make every later gcloud command use this project.
  gcloud config set project "$PROJECT_ID"
  # Turn on GKE, Artifact Registry (stores images) and Cloud Build (builds them).
  gcloud services enable container.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
  # Create the Docker registry the cluster pulls the images from.
  gcloud artifacts repositories create orbitops --repository-format=docker --location="$REGION"
  ;;

build)
  # Build the backend image in Google Cloud and push it as v1. The chat, auth
  # and ops services all run this one image with different start commands.
  gcloud builds submit backend --tag "$REG/backend:v1"
  # Build the frontend (React app served by nginx) and push it as v1.
  gcloud builds submit frontend --tag "$REG/frontend:v1"
  # Push the same backend code as v2 now, so the rolling-update demo doesn't
  # have to wait for a build. The app shows its version in the footer.
  gcloud artifacts docker tags add "$REG/backend:v1" "$REG/backend:v2"
  ;;

cluster)
  # Two e2-standard-2 nodes to start; the cluster autoscaler adds up to four
  # when pods don't fit (for example during the autoscaling demo).
  gcloud container clusters create orbitops \
    --zone "$ZONE" --machine-type e2-standard-2 \
    --disk-type pd-standard --disk-size 30 \
    --num-nodes 2 --enable-autoscaling --min-nodes 2 --max-nodes 4
  # Save the cluster's credentials so kubectl talks to it.
  gcloud container clusters get-credentials orbitops --zone "$ZONE"
  ;;

deploy)
  # The orbitops namespace that everything else lives in.
  kubectl apply -f k8s/00-namespace.yaml
  # Passwords and API keys. secrets.yaml is your filled-in copy of
  # 01-secrets.example.yaml and is git-ignored.
  kubectl apply -f k8s/secrets.yaml
  # Shared settings, then the three stateful pieces: MySQL (accounts, chats,
  # uploads), Chroma (vector store) and Redis (shared rate limits).
  apply k8s/02-configmap.yaml k8s/10-mysql.yaml k8s/11-chroma.yaml k8s/12-redis.yaml
  # Read-only cluster access, given only to the ops service.
  apply k8s/22-rbac.yaml
  # The four app services: chat (backend), frontend, auth and ops, each with
  # its own Service; then the autoscalers.
  apply k8s/20-backend.yaml k8s/21-frontend.yaml k8s/23-auth.yaml k8s/24-ops.yaml k8s/30-hpa.yaml
  # Wait until each Deployment has all its pods ready.
  for d in backend frontend auth ops; do
    kubectl -n $NS rollout status deploy/$d --timeout=5m
  done
  # Print the public address (EXTERNAL-IP); it can take a minute to appear.
  kubectl -n $NS get svc frontend
  ;;

status)
  # Every Deployment, StatefulSet, Service and pod, with the node each pod is on.
  kubectl -n $NS get deploy,sts,svc,pods -o wide
  # Current and target CPU and replica counts of the autoscalers.
  kubectl -n $NS get hpa
  # Disruption budgets: how many pods of each service may be taken down at once.
  kubectl -n $NS get pdb
  ;;

# ---------------------------------------------------------------------------
# Week 13 demo, about 4 minutes. Open the app in a browser first.
# ---------------------------------------------------------------------------

demo-services)
  # Microservices: frontend, chat, auth and ops Deployments plus MySQL,
  # Chroma and Redis, each behind its own Service, spread across the nodes.
  kubectl -n $NS get deploy,sts,svc,pods -o wide
  ;;

demo-lb)
  # Load balancing: the public load balancer's address.
  IP=$(kubectl -n $NS get svc frontend -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
  # Ask the chat service six times; "pod" changes as requests are spread
  # over the replicas.
  for i in 1 2 3 4 5 6; do curl -s "http://$IP/api/health"; echo; done
  ;;

demo-scale)
  # Autoscaling: start 3 load pods with 4 request loops each against the chat
  # service, enough to push its CPU well past the HPA's 60% target.
  kubectl -n $NS create deployment load --image=busybox:1.36 --replicas=3 -- \
    sh -c 'for i in 1 2 3 4; do (while true; do wget -q -O /dev/null http://backend:8000/api/health; done) & done; wait'
  # Watch replicas climb from 2 towards 5 (Ctrl+C to stop watching).
  kubectl -n $NS get hpa backend -w || true
  ;;

demo-scale-stop)
  # Remove the load pods; the HPA scales back down to 2 after about a minute
  # (see 30-hpa.yaml), before the node-failure step.
  kubectl -n $NS delete deployment load
  ;;

demo-node-failure)
  # Self-healing / node failure. Find the nodes running MySQL and Chroma (one
  # replica each, so draining their node restarts them elsewhere) ...
  MYSQL_NODE=$(kubectl -n $NS get pod mysql-0 -o jsonpath='{.spec.nodeName}')
  CHROMA_NODE=$(kubectl -n $NS get pod chroma-0 -o jsonpath='{.spec.nodeName}')
  # ... and pick a node that runs a chat pod but neither of those. When none
  # does (likely on two nodes), grep finds nothing; "|| true" stops that
  # ending the script under "set -e -o pipefail".
  NODE=$(kubectl -n $NS get pods -l app=backend -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' \
    | sort -u | grep -vxF -e "$MYSQL_NODE" -e "$CHROMA_NODE" | head -1 || true)
  if [ -z "$NODE" ]; then
    NODE=$(kubectl -n $NS get pods -l app=backend -o jsonpath='{.items[0].spec.nodeName}')
    echo "Every chat pod shares a node with MySQL or Chroma, so this drain also restarts"
    echo "them: sign-in and chat pause for about a minute while they move."
  fi
  echo "Taking node $NODE out of service"
  # Mark the node unschedulable and evict its pods, as a node failure or GKE
  # upgrade would. The disruption budgets keep at least one pod of each
  # service running, so the app stays up while pods move to the other node.
  # Drain gives up after 2.5 minutes rather than holding up the demo if a
  # replacement pod can't find room; the node is uncordoned either way.
  kubectl drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=150s \
    || echo "Drain didn't finish in time: some pods are still waiting for room on another node."
  # Every pod is now on the remaining node(s); the app still answers.
  kubectl -n $NS get pods -o wide
  # Bring the node back into service.
  kubectl uncordon "$NODE"
  ;;

demo-update)
  # Rolling update: deploy v2 of the chat service. Pods are replaced one at a
  # time (maxUnavailable 0), so there is no downtime; the footer shows v2.
  # A patch changes only the image and version, in one rollout; re-applying
  # the manifest would also reset the replica count the autoscaler has set.
  kubectl -n $NS patch deploy/backend -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"backend\",\"image\":\"$REG/backend:v2\",\"env\":[{\"name\":\"APP_VERSION\",\"value\":\"v2\"}]}]}}}}"
  # Follow the rollout until every pod runs v2.
  kubectl -n $NS rollout status deploy/backend
  ;;

demo-rollback)
  # List the versions Kubernetes has kept for the chat service.
  kubectl -n $NS rollout history deploy/backend
  # Go back to the previous version (v1), again one pod at a time.
  kubectl -n $NS rollout undo deploy/backend
  # Follow the rollback until it finishes.
  kubectl -n $NS rollout status deploy/backend
  ;;

demo-incident)
  # AIOps: ship a broken release (an image tag that doesn't exist). The old
  # pods keep serving, but the rollout is stuck. Then click "Run health
  # check" in the app: the ops service finds the ImagePullBackOff, reads the
  # events and runbooks, and suggests the fix.
  kubectl -n $NS set image deploy/backend backend="$REG/backend:does-not-exist"
  ;;

demo-recover)
  # Apply the fix the assistant suggested: undo the broken release.
  kubectl -n $NS rollout undo deploy/backend
  # Wait for it to finish, then run the health check again: Healthy.
  kubectl -n $NS rollout status deploy/backend
  ;;

cleanup)
  # Delete the cluster (nodes, load balancer) so it stops costing credit.
  gcloud container clusters delete orbitops --zone "$ZONE" --quiet
  # MySQL's and Chroma's disks can outlive the cluster; list any left over
  # and delete them with: gcloud compute disks delete <name> --zone <zone>
  gcloud compute disks list
  ;;

*)
  # No or unknown step: show the list at the top of this file.
  sed -n '2,14p' "$0"
  ;;
esac
