"""
Thin dispatcher joining the two separate pipelines (RAG, Agent) — chosen over
one merged LangGraph so each half stays independently testable and the v1
ships without the added complexity of a unified graph.

Routing is keyword-first, sequential for "both":
  - 'ops'    -> questions about the live deployment itself (pods, restarts,
               rollouts, logs, cluster health) -> cluster_ops (AIOps). Checked
               first: "why did the backend pod restart" is about the cluster,
               not the docs, even though runbooks get pulled in as context.
  - 'docs'   -> questions about OrbitOps internal documentation/procedures
  - 'agent'  -> questions about the outside world (GitHub, HackerNews, Google)
  - 'both'   -> query clearly asks for both ("according to our docs AND what's
               trending on GitHub") -> RAG runs first, then Agent, responses
               are concatenated with their own source tags.
"""

from __future__ import annotations

import os
import re
import time
from typing import Literal, Optional

import httpx

from . import rag_core
from . import agent_core
from . import cluster_ops
from . import store

# On Kubernetes, cluster questions go to the separate ops service, the only
# pods allowed to read the cluster. Unset (local dev), cluster_ops runs here.
OPS_SERVICE_URL = os.environ.get("OPS_SERVICE_URL", "").rstrip("/")
_ops_available = (float("-inf"), False)  # (checked_at, available)


def ops_available() -> bool:
    global _ops_available
    if not OPS_SERVICE_URL:
        return cluster_ops.is_available()
    # Asked on every message, so cache the answer for 30s rather than make
    # an extra call each time. If the ops service is down, cluster
    # questions fall back to the docs instead of failing.
    checked_at, available = _ops_available
    if time.monotonic() - checked_at > 30:
        try:
            r = httpx.get(f"{OPS_SERVICE_URL}/internal/ops/available", timeout=3)
            available = bool(r.json().get("available"))
        except Exception:
            available = False
        _ops_available = (time.monotonic(), available)
    return available


def ops_answer(thread_id: str, query: str) -> Optional[dict]:
    """The ops service's answer, or None if it can't be reached right now
    (restarting, rescheduled after a node drain), so the caller can answer
    from the runbooks instead."""
    global _ops_available
    if not OPS_SERVICE_URL:
        return cluster_ops.ops_answer(query, rag_session=rag_core.get_or_create_session(thread_id))
    try:
        r = httpx.post(f"{OPS_SERVICE_URL}/internal/ops/answer",
                       json={"thread_id": thread_id, "query": query}, timeout=150)
    except httpx.TransportError:
        _ops_available = (time.monotonic(), False)
        return None
    if r.status_code == 429:
        r.raise_for_status()  # the model quota; main.py explains it
    if r.status_code >= 500:
        _ops_available = (time.monotonic(), False)
        return None
    r.raise_for_status()
    return r.json()

AGENT_SIGNALS = [
    r"\bgithub\b", r"\brepo(s|sitory|sitories)?\b", r"\bstars?\b",
    r"\bhacker ?news\b", r"\bhn\b",
    r"\bgoogle\b", r"\bsearch the web\b", r"\bonline\b", r"\binternet\b",
    r"\btrending\b", r"\bnews\b", r"\blatest\b",
    r"\breal[- ]world\b", r"\bcommunity\b", r"\bdevelopers? (are |is )?saying\b",
    r"\bopen[- ]source\b",
]

OPS_SIGNALS = [
    r"\b(k8s|kubernetes|kubectl|cluster)\b",
    r"\bpods?\b", r"\breplicas?\b", r"\bautoscal\w*\b", r"\bhpa\b",
    r"\brestart(s|ed|ing)?\b", r"\bcrash\w*\b", r"\boom\w*\b", r"\brollouts?\b",
    r"\b(health|healthy|status) of\b", r"\bis (anything|something) (failing|broken|down|wrong)\b",
    r"\b(backend|frontend|auth|ops|mysql|chroma|redis) (logs?|errors?|down|slow|failing|status)\b",
    r"\b(explain|show|check) (me )?(the )?(\w+ )?(logs?|errors?)\b",
]

BOTH_SIGNALS = [
    r"\b(and|as well as)\b.*\b(github|hackernews|hacker news|google|trending|news)\b",
    r"\bcompare\b.*\b(our docs|documentation|runbook)\b.*\b(github|hackernews|google)\b",
]

Route = Literal["rag", "agent", "both", "ops"]


def classify_query(query: str) -> Route:
    q = query.lower()
    # Only when the app can actually see a cluster -- running locally, a
    # question mentioning "pods" is better answered from the docs than with
    # "cluster access isn't configured".
    if ops_available():
        for pattern in OPS_SIGNALS:
            if re.search(pattern, q):
                return "ops"
    for pattern in BOTH_SIGNALS:
        if re.search(pattern, q):
            return "both"
    for pattern in AGENT_SIGNALS:
        if re.search(pattern, q):
            return "agent"
    return "rag"


def handle_message(thread_id: str, query: str) -> dict:
    """Runs the appropriate pipeline(s) and returns a display-ready result:
    {'route': 'rag'|'agent'|'both'|'ops', 'segments': [{'source', 'text', 'citations'}]}
    """
    route = classify_query(query)
    segments = []

    if route == "ops":
        ops_result = ops_answer(thread_id, query)
        if ops_result is None:
            route = "rag"  # ops service unreachable: answer from the runbooks
        else:
            segments.append({"source": "ops", "text": ops_result["text"], "citations": ops_result["citations"]})

    if route in ("rag", "both"):
        rag_session = rag_core.get_or_create_session(thread_id)
        rag_result = rag_session.answer(query, history=store.get_messages(thread_id))
        segments.append({
            "source": "rag",
            "text": rag_result["answer"],
            "citations": rag_result["citations"],
        })

    if route in ("agent", "both"):
        agent_result = agent_core.run_agent(query, thread_id=thread_id)
        segments.append({
            "source": "agent",
            "text": agent_result.get("final_response", ""),
            "citations": [],
        })

    return {"route": route, "segments": segments}


def docs_indexed_count(thread_id: str) -> int:
    # A new chat isn't saved until its first message or upload; don't build
    # an index just to count the base documents.
    if not store.existing_thread_ids([thread_id]):
        return rag_core.base_docs_count()
    session = rag_core.get_or_create_session(thread_id)
    return len(set(c.metadata["doc_id"] for c in session.chunks))
