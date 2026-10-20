"""
AIOps: lets the assistant observe the Kubernetes deployment it runs in.

Three capabilities, all read-only:
  - snapshot():       live state of the app's namespace -- pods (ready,
                      restarts, why the last container died), deployments
                      (rollout progress), autoscalers and recent warning
                      events.
  - detect_issues():  rule-based checks over a snapshot (crash loops,
                      OOM kills, image pull failures, pods not ready,
                      stalled rollouts, autoscaler at its ceiling, ...).
                      Rules, not the LLM, decide what counts as a problem,
                      so the health check can't hallucinate an incident.
  - recent_logs():    the last lines of each pod's logs for one app, with
                      error-looking lines picked out.

ops_answer() and health_check() then hand that data, plus the OrbitOps
runbooks retrieved by the RAG pipeline, to the LLM to explain what's going
on and what to do about it.

Access comes from the pod's own service account (k8s/22-rbac.yaml grants
get/list/watch on these resources in the app's namespace only, nothing that
can change the cluster). Outside a cluster it falls back to the local
kubeconfig, and if neither exists the ops features report themselves as
unavailable instead of failing.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import llm

NAMESPACE = os.environ.get("POD_NAMESPACE", "orbitops")
APPS = ["frontend", "backend", "auth", "ops", "mysql", "chroma", "redis"]
EVENT_WINDOW_MINUTES = 30
RESTART_WARN_THRESHOLD = 3
LOG_LINES = 200

# No leading \b on the words, so "KeyError" / "ConnectionRefusedError" match too.
ERROR_PATTERN = re.compile(r"(error|exception|traceback|critical|fatal|failed|refused|timed? ?out)|\b5\d\d\b", re.I)

_apis = None
_unavailable_reason: Optional[str] = None


def _get_apis():
    """(CoreV1Api, AppsV1Api, AutoscalingV2Api), or None if no cluster is reachable."""
    global _apis, _unavailable_reason
    if _apis is not None or _unavailable_reason is not None:
        return _apis
    try:
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        _apis = (client.CoreV1Api(), client.AppsV1Api(), client.AutoscalingV2Api())
    except Exception as e:  # no cluster, no kubeconfig, or client not installed
        _unavailable_reason = f"Cluster access isn't configured here ({type(e).__name__})."
    return _apis


def is_available() -> bool:
    return _get_apis() is not None


def unavailable_message() -> str:
    _get_apis()
    return (_unavailable_reason or "Cluster access isn't configured here.") + (
        " The cluster tools work once the app is deployed to Kubernetes with k8s/22-rbac.yaml applied."
    )


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _age_minutes(ts) -> Optional[float]:
    if ts is None:
        return None
    return round((datetime.now(timezone.utc) - ts).total_seconds() / 60, 1)


def _pod_info(pod) -> dict:
    statuses = pod.status.container_statuses or []
    containers = []
    for cs in statuses:
        state, reason = "unknown", None
        if cs.state.running:
            state = "running"
        elif cs.state.waiting:
            state, reason = "waiting", cs.state.waiting.reason
        elif cs.state.terminated:
            state, reason = "terminated", cs.state.terminated.reason
        last = cs.last_state.terminated if cs.last_state else None
        containers.append({
            "name": cs.name,
            "ready": bool(cs.ready),
            "restarts": cs.restart_count or 0,
            "state": state,
            "reason": reason,
            "last_exit_reason": last.reason if last else None,
            "last_exit_code": last.exit_code if last else None,
            "last_exit_minutes_ago": _age_minutes(last.finished_at) if last else None,
            "image": cs.image,
        })
    return {
        "name": pod.metadata.name,
        "app": (pod.metadata.labels or {}).get("app", ""),
        "phase": pod.status.phase,
        "node": pod.spec.node_name,
        "age_minutes": _age_minutes(pod.metadata.creation_timestamp),
        "containers": containers,
    }


def _deployment_info(d) -> dict:
    conditions = {c.type: {"status": c.status, "reason": c.reason, "message": c.message}
                  for c in (d.status.conditions or [])}
    return {
        "name": d.metadata.name,
        "desired": d.spec.replicas or 0,
        "ready": d.status.ready_replicas or 0,
        "updated": d.status.updated_replicas or 0,
        "available": d.status.available_replicas or 0,
        "image": d.spec.template.spec.containers[0].image,
        "revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
        "rollout_in_progress": (d.status.observed_generation or 0) < (d.metadata.generation or 0)
                               or (d.status.updated_replicas or 0) < (d.spec.replicas or 0),
        "conditions": conditions,
    }


def _hpa_info(h) -> dict:
    cpu = None
    for m in (h.status.current_metrics or []):
        if m.resource and m.resource.name == "cpu" and m.resource.current:
            cpu = m.resource.current.average_utilization
    target = None
    for m in (h.spec.metrics or []):
        if m.resource and m.resource.name == "cpu" and m.resource.target:
            target = m.resource.target.average_utilization
    return {
        "name": h.metadata.name,
        "min": h.spec.min_replicas,
        "max": h.spec.max_replicas,
        "current": h.status.current_replicas,
        "desired": h.status.desired_replicas,
        "cpu_percent": cpu,
        "cpu_target_percent": target,
    }


def _event_time(e):
    return e.last_timestamp or e.event_time or e.metadata.creation_timestamp


def snapshot() -> dict:
    core, apps, autoscaling = _get_apis()
    pods = [_pod_info(p) for p in core.list_namespaced_pod(NAMESPACE).items]
    deployments = [_deployment_info(d) for d in apps.list_namespaced_deployment(NAMESPACE).items]
    statefulsets = [{
        "name": s.metadata.name,
        "desired": s.spec.replicas or 0,
        "ready": s.status.ready_replicas or 0,
    } for s in apps.list_namespaced_stateful_set(NAMESPACE).items]
    hpas = [_hpa_info(h) for h in autoscaling.list_namespaced_horizontal_pod_autoscaler(NAMESPACE).items]

    events = []
    for e in core.list_namespaced_event(NAMESPACE).items:
        age = _age_minutes(_event_time(e))
        if e.type == "Warning" and age is not None and age <= EVENT_WINDOW_MINUTES:
            events.append({
                "object": f"{(e.involved_object.kind or '').lower()}/{e.involved_object.name}",
                "reason": e.reason,
                "message": (e.message or "")[:300],
                "count": e.count or 1,
                "minutes_ago": age,
            })
    events.sort(key=lambda ev: ev["minutes_ago"])

    return {
        "namespace": NAMESPACE,
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pods": pods,
        "deployments": deployments,
        "statefulsets": statefulsets,
        "autoscalers": hpas,
        "warning_events": events[:25],
    }


# ---------------------------------------------------------------------------
# Rule-based issue detection
# ---------------------------------------------------------------------------

CRITICAL_WAIT_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                         "CreateContainerConfigError", "InvalidImageName"}


def detect_issues(snap: dict) -> List[dict]:
    """Each issue: {severity: 'critical'|'warning', object, problem, detail}."""
    issues: List[dict] = []

    def add(severity, obj, problem, detail=""):
        issues.append({"severity": severity, "object": obj, "problem": problem, "detail": detail})

    for pod in snap["pods"]:
        obj = f"pod/{pod['name']}"
        if pod["phase"] == "Pending" and (pod["age_minutes"] or 0) > 2:
            add("warning", obj, "Pod stuck in Pending",
                "Usually not enough CPU/memory on the nodes, or a volume that can't be attached.")
        if pod["phase"] == "Failed":
            add("critical", obj, "Pod failed")
        for c in pod["containers"]:
            if c["state"] == "waiting" and c["reason"] in CRITICAL_WAIT_REASONS:
                add("critical", obj, f"Container '{c['name']}' is in {c['reason']}",
                    f"Image {c['image']}, {c['restarts']} restarts.")
            if c["last_exit_reason"] == "OOMKilled":
                add("critical", obj, f"Container '{c['name']}' was OOMKilled",
                    "It used more memory than its limit; raise the memory limit or reduce usage.")
            if c["restarts"] >= RESTART_WARN_THRESHOLD and c["reason"] != "CrashLoopBackOff":
                add("warning", obj, f"Container '{c['name']}' has restarted {c['restarts']} times",
                    f"Last exit: {c['last_exit_reason']} (code {c['last_exit_code']}), "
                    f"{c['last_exit_minutes_ago']} min ago.")
            if (pod["phase"] == "Running" and not c["ready"] and c["state"] == "running"
                    and (pod["age_minutes"] or 0) > 3):
                add("warning", obj, f"Container '{c['name']}' is running but not ready",
                    "Its readiness probe is failing, so the Service sends it no traffic.")

    for d in snap["deployments"]:
        obj = f"deployment/{d['name']}"
        progressing = d["conditions"].get("Progressing", {})
        if progressing.get("reason") == "ProgressDeadlineExceeded":
            add("critical", obj, "Rollout is stuck (progress deadline exceeded)",
                f"Image {d['image']}. Consider `kubectl rollout undo deployment/{d['name']}`.")
        elif d["rollout_in_progress"]:
            add("warning", obj, "Rollout in progress",
                f"{d['updated']}/{d['desired']} pods updated to {d['image']}.")
        if d["available"] == 0 and d["desired"] > 0:
            add("critical", obj, "No pods available", "The service is down.")
        elif d["ready"] < d["desired"]:
            add("warning", obj, f"Only {d['ready']}/{d['desired']} pods ready")

    for s in snap["statefulsets"]:
        if s["ready"] < s["desired"]:
            add("critical", f"statefulset/{s['name']}", f"Only {s['ready']}/{s['desired']} pods ready",
                "Data services (MySQL/Chroma) being down breaks chat history and retrieval.")

    for h in snap["autoscalers"]:
        if h["current"] is not None and h["max"] and h["current"] >= h["max"]:
            add("warning", f"hpa/{h['name']}", f"Autoscaler at its maximum ({h['max']} pods)",
                f"CPU {h['cpu_percent']}% vs target {h['cpu_target_percent']}%. "
                "Load may exceed capacity; consider raising maxReplicas or node count.")

    # Warning events not already explained by a rule above (e.g. FailedScheduling,
    # FailedMount, Unhealthy) -- surface the most frequent few.
    seen = {i["object"] for i in issues}
    for ev in sorted(snap["warning_events"], key=lambda e: -e["count"])[:5]:
        if ev["object"] not in seen:
            add("warning", ev["object"], f"Warning event: {ev['reason']}",
                f"{ev['message']} (x{ev['count']}, {ev['minutes_ago']} min ago)")

    issues.sort(key=lambda i: 0 if i["severity"] == "critical" else 1)
    return issues


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def recent_logs(app: str = "backend", lines: int = LOG_LINES) -> List[dict]:
    """Recent logs of every pod of one app: [{pod, error_lines, tail}]."""
    core, _, _ = _get_apis()
    out = []
    for pod in core.list_namespaced_pod(NAMESPACE, label_selector=f"app={app}").items:
        try:
            text = core.read_namespaced_pod_log(pod.metadata.name, NAMESPACE, tail_lines=lines)
        except Exception as e:
            text = f"(could not read logs: {type(e).__name__})"
        log_lines = text.splitlines()
        out.append({
            "pod": pod.metadata.name,
            "error_lines": [ln for ln in log_lines if ERROR_PATTERN.search(ln)][-30:],
            "tail": log_lines[-15:],
        })
    return out


def wanted_log_app(query: str) -> Optional[str]:
    """Which app's logs a question is asking about, if it's asking about logs/errors at all."""
    q = query.lower()
    if not re.search(r"\b(logs?|errors?|exceptions?|stack ?traces?|5\d\d|restart\w*|crash\w*|fail\w*)\b", q):
        return None
    for app in APPS:
        if re.search(rf"\b{app}\b", q):
            return app
    return "backend"


# ---------------------------------------------------------------------------
# LLM explanations
# ---------------------------------------------------------------------------

def _runbook_context(rag_session, query: str) -> tuple[str, List[dict]]:
    if rag_session is None:
        return "", []
    chunks = rag_session.retrieve(query, top_k=4)
    if not chunks:
        return "", []
    from .rag_core import build_context
    citations = [{"doc_id": c.metadata.get("doc_id"), "title": c.metadata.get("title")} for c in chunks[:3]]
    return build_context(chunks, max_chunks=4), citations


def _compact(snap: dict) -> str:
    return json.dumps(snap, default=str, separators=(",", ":"))[:12000]


def ops_answer(query: str, rag_session=None, include_logs: bool = False) -> dict:
    """Answers a question about the live deployment. Pod logs (which hold
    other users' IP addresses and chat ids) are only read for the admin;
    everyone else gets the cluster state and events."""
    if not is_available():
        return {"text": unavailable_message(), "citations": []}

    snap = snapshot()
    issues = detect_issues(snap)
    log_app = wanted_log_app(query) if include_logs else None
    logs = recent_logs(log_app) if log_app else None
    runbooks, citations = _runbook_context(rag_session, query + " " + " ".join(i["problem"] for i in issues[:3]))

    prompt = f"""You are the AIOps assistant for the OrbitOps platform, which runs on Kubernetes
(namespace {snap['namespace']}). Answer the operator's question using ONLY the live data below.

QUESTION: {query}

LIVE CLUSTER STATE (taken {snap['taken_at']}):
{_compact(snap)}

ISSUES FOUND BY AUTOMATED CHECKS:
{json.dumps(issues, default=str) if issues else "none"}
"""
    if logs is not None:
        prompt += f"\nRECENT {log_app.upper()} LOGS (error-looking lines, then the tail of each pod):\n{json.dumps(logs)[:8000]}\n"
    if runbooks:
        prompt += f"\nRELEVANT ORBITOPS RUNBOOKS:\n{runbooks}\n"
    prompt += """
INSTRUCTIONS:
1. Lead with the direct answer, then the evidence (pod/deployment names, numbers, log lines).
2. If logs were included, group similar errors and give the most likely cause.
3. If something needs fixing, give concrete next steps; cite runbooks as [doc_id] when you use them.
4. Never invent pods, numbers or errors that are not in the data. If the data can't answer it, say so.
"""
    if not include_logs and wanted_log_app(query):
        prompt += "5. Pod logs are only shown to the admin account; if the question needs them, say so.\n"
    return {"text": llm.generate(prompt), "citations": citations if runbooks else []}


def health_check(rag_session=None, include_logs: bool = False) -> dict:
    """One-click scan: rule-based issues + an LLM incident summary. Logs of
    the affected pods are only read for the admin (see ops_answer)."""
    if not is_available():
        return {"status": "unavailable", "issues": [], "summary": unavailable_message(), "citations": []}

    snap = snapshot()
    issues = detect_issues(snap)
    status = "critical" if any(i["severity"] == "critical" for i in issues) else (
        "warning" if issues else "healthy")

    pods_ready = sum(all(c["ready"] for c in p["containers"]) and p["phase"] == "Running" for p in snap["pods"])
    if not issues:
        summary = (f"All clear: {pods_ready}/{len(snap['pods'])} pods ready, no crash loops, "
                   f"stuck rollouts or warning events in the last {EVENT_WINDOW_MINUTES} minutes.")
        return {"status": status, "issues": [], "summary": summary, "citations": [], "taken_at": snap["taken_at"]}

    affected_apps = {p["app"] for p in snap["pods"] if any(p["name"] in i["object"] for i in issues)}
    logs = ({app: recent_logs(app, lines=100) for app in sorted(affected_apps) if app}
            if include_logs else "not included (only the admin account sees pod logs)")
    runbooks, citations = _runbook_context(rag_session, "incident " + " ".join(i["problem"] for i in issues[:5]))

    prompt = f"""You are the AIOps assistant for the OrbitOps platform on Kubernetes. Automated
checks just found the problems below. Write a short incident summary for the on-call engineer.

ISSUES (from rule-based checks, these are facts):
{json.dumps(issues, default=str)}

CLUSTER STATE:
{_compact(snap)}

LOGS FROM AFFECTED PODS:
{json.dumps(logs)[:8000]}
"""
    if runbooks:
        prompt += f"\nRELEVANT ORBITOPS RUNBOOKS:\n{runbooks}\n"
    prompt += """
Format:
Status: one line (what is broken and the user impact).
Likely cause: one or two lines, based on the issues and logs.
Next steps: 2-4 numbered, concrete actions (kubectl commands where useful); cite runbooks as [doc_id] when used.
Do not invent anything that isn't in the data.
"""
    return {"status": status, "issues": issues, "summary": llm.generate(prompt),
            "citations": citations if runbooks else [], "taken_at": snap["taken_at"]}
