"""
AIOps endpoints: the one-click health check (/api/ops/health-check) and the
internal calls the chat service makes for cluster questions.

On Kubernetes these run in their own ops service (ops_service.py), the only
pods given read access to the cluster (k8s/22-rbac.yaml). The chat pods,
which handle untrusted user input, get no cluster access at all. Run
locally, main.py mounts the same router and dispatcher.py calls
cluster_ops directly.

/internal/* is only reachable inside the cluster: the frontend's nginx
proxies /api/ paths only.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel, Field

from . import auth
from . import cluster_ops
from . import common
from . import guardrails
from . import llm
from . import rag_core
from . import store
from .rate_limit import check_rate_limit

HEALTH_CHECK_RATE_LIMIT = (6, 60)  # per account; each check reads the cluster + calls the LLM

router = APIRouter()


@router.post("/api/ops/health-check")
def ops_health_check(thread_id: str = Form(...), user: dict = Depends(auth.current_user)):
    """Scans the app's Kubernetes namespace, flags problems with rule-based
    checks and has the LLM write an incident summary. The result is also
    saved into the chat so it stays in the conversation history."""
    common.require_own_thread(thread_id, user)
    if not check_rate_limit(f"health:{user['id']}", *HEALTH_CHECK_RATE_LIMIT):
        raise HTTPException(429, "Too many health checks — try again shortly.")

    store.ensure_thread(thread_id, title_hint="Cluster health check", user_id=user["id"])
    # The thread's session so runbooks the user uploaded are searched too.
    session = rag_core.get_or_create_session(thread_id)
    try:
        result = cluster_ops.health_check(rag_session=session, include_logs=user["is_admin"])
    except Exception as e:
        if llm.is_quota_error(e):
            raise HTTPException(429, llm.QUOTA_MESSAGE)
        raise

    label = {"healthy": "Healthy", "warning": "Warning", "critical": "Critical",
             "unavailable": "Unavailable"}[result["status"]]
    lines = [f"Health check: {label}", ""]
    for issue in result["issues"]:
        lines.append(f"[{issue['severity'].upper()}] {issue['object']}: {issue['problem']}")
    if result["issues"]:
        lines.append("")
    lines.append(result["summary"])
    text = "\n".join(lines)

    store.add_message(thread_id, "user", "Run a cluster health check")
    store.add_message(thread_id, "assistant", text, source="ops",
                      citations=json.dumps(result["citations"]) if result["citations"] else None)
    store.set_title_from_first_message(thread_id, "Cluster health check")

    return {"thread_id": thread_id, "status": result["status"], "issues": result["issues"],
            "text": text, "citations": result["citations"]}


# ---------------------------------------------------------------------------
# Called by the chat service (dispatcher.py) when a message is about the
# cluster. The chat service has already checked the user and the thread.
# ---------------------------------------------------------------------------

class OpsQuestion(BaseModel):
    thread_id: str
    query: str = Field(..., max_length=guardrails.MAX_QUERY_LENGTH + 50)
    include_logs: bool = False  # the chat service sets it for the admin only


@router.get("/internal/ops/available")
def ops_available():
    return {"available": cluster_ops.is_available()}


@router.post("/internal/ops/answer")
def ops_internal_answer(req: OpsQuestion):
    common.require_valid_thread_id(req.thread_id)
    try:
        session = rag_core.get_or_create_session(req.thread_id)
    except LookupError:
        raise HTTPException(404, "Chat not found.")
    try:
        return cluster_ops.ops_answer(req.query, rag_session=session, include_logs=req.include_logs)
    except Exception as e:
        if llm.is_quota_error(e):
            raise HTTPException(429, llm.QUOTA_MESSAGE)
        raise
