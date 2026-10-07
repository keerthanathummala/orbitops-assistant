"""
Thin dispatcher joining the two separate pipelines (RAG, Agent) — chosen over
one merged LangGraph so each half stays independently testable and the v1
ships without the added complexity of a unified graph.

Routing is keyword-first, sequential for "both":
  - 'docs'   -> questions about OrbitOps internal documentation/procedures
  - 'agent'  -> questions about the outside world (GitHub, HackerNews, Google)
  - 'both'   -> query clearly asks for both ("according to our docs AND what's
               trending on GitHub") -> RAG runs first, then Agent, responses
               are concatenated with their own source tags.
"""

from __future__ import annotations

import re
from typing import Literal

from . import rag_core
from . import agent_core

AGENT_SIGNALS = [
    r"\bgithub\b", r"\brepo(s|sitory|sitories)?\b", r"\bstars?\b",
    r"\bhacker ?news\b", r"\bhn\b",
    r"\bgoogle\b", r"\bsearch the web\b", r"\bonline\b", r"\binternet\b",
    r"\btrending\b", r"\bnews\b", r"\blatest\b",
    r"\breal[- ]world\b", r"\bcommunity\b", r"\bdevelopers? (are |is )?saying\b",
    r"\bopen[- ]source\b",
]

BOTH_SIGNALS = [
    r"\b(and|as well as)\b.*\b(github|hackernews|hacker news|google|trending|news)\b",
    r"\bcompare\b.*\b(our docs|documentation|runbook)\b.*\b(github|hackernews|google)\b",
]

Route = Literal["rag", "agent", "both"]


def classify_query(query: str) -> Route:
    q = query.lower()
    for pattern in BOTH_SIGNALS:
        if re.search(pattern, q):
            return "both"
    for pattern in AGENT_SIGNALS:
        if re.search(pattern, q):
            return "agent"
    return "rag"


def handle_message(thread_id: str, query: str) -> dict:
    """Runs the appropriate pipeline(s) and returns a display-ready result:
    {'route': 'rag'|'agent'|'both', 'segments': [{'source', 'text', 'citations'}]}
    """
    route = classify_query(query)
    segments = []

    if route in ("rag", "both"):
        rag_session = rag_core.get_or_create_session(thread_id)
        rag_result = rag_session.answer(query)
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
    session = rag_core.get_or_create_session(thread_id)
    return len(set(c.metadata["doc_id"] for c in session.chunks))
