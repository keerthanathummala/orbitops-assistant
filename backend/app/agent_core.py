"""
Domain Intelligence Agent — ported from COMP2701 Assessment 2 (LangGraph:
fetch_node -> analyse_node, GitHub + HackerNews tools, regex guardrail,
LLM-as-judge eval suite already proven at 86% in the notebook).

Fixes/changes relative to the graded submission:

1. route_after_fetch(): when results stay empty after 3 fetch attempts, the
   original code returned 'end' directly from the conditional edge, which
   produced a final_response of "" (seen in the eval report on cases ADV3,
   E1, E2). Fixed by routing to 'respond_empty', a tiny edge that produces
   a graceful "no results found" message instead of silence.

2. run_agent(): always built a brand-new initial state and never loaded the
   prior checkpoint, so despite MemorySaver being wired up, multi-turn
   memory was never actually used by any node. Fixed by reading back the
   graph's own checkpoint via get_state() and carrying forward a running
   chat_history, which fetch_node/analyse_node can use for follow-up
   context in the same thread_id.

3. Performance: the original pipeline made 3 sequential Ollama calls per
   turn (keyword extraction -> synthesis -> response formatting), each
   20-60s+ on CPU. fetch_node's keyword extraction is now a stopword
   heuristic (extract_keywords_heuristic) instead of an LLM call, and
   analyse_node does synthesis + final formatting in one combined call
   instead of two separate nodes. Happy path: 1 LLM call per turn instead
   of 3.

A third tool (Google Custom Search) is added alongside GitHub + HackerNews.
It is only called when it is currently available (daily quota not
exhausted) — see google_search.py.
"""

from __future__ import annotations

import json
import operator
import re
from typing import TypedDict, Annotated, List, Optional

import httpx
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from . import google_search
from . import guardrails

MODEL = "qwen2.5:7b"
_llm: Optional[ChatOllama] = None
_llm_creative: Optional[ChatOllama] = None


def get_llm() -> ChatOllama:
    global _llm
    if _llm is None:
        # See rag_core.get_llm for why num_predict is set explicitly --
        # without it Ollama's own default (~128 tokens) silently truncates
        # longer answers (e.g. a requested list of points) mid-sentence.
        _llm = ChatOllama(model=MODEL, temperature=0, num_predict=4096)
    return _llm


def get_llm_creative() -> ChatOllama:
    global _llm_creative
    if _llm_creative is None:
        _llm_creative = ChatOllama(model=MODEL, temperature=0.3, num_predict=4096)
    return _llm_creative


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class GithubInput(BaseModel):
    query: str = Field(description='Search query for repositories, e.g. "LLM agent python"')
    language: str = Field(default="", description='Filter by programming language. Empty = any.')
    max_results: int = Field(default=5, ge=1, le=10)


@tool("search_github", args_schema=GithubInput)
def search_github(query: str, language: str = "", max_results: int = 5) -> dict:
    """Search GitHub for public repositories matching a query."""
    q = query + (f" language:{language}" if language else "")
    try:
        r = httpx.get(
            "https://api.github.com/search/repositories",
            params={"q": q, "sort": "stars", "order": "desc", "per_page": max_results},
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "portfolio-app"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        repos = [
            {
                "name": i["full_name"],
                "description": (i["description"] or "")[:200],
                "stars": i["stargazers_count"],
                "language": i["language"] or "",
                "url": i["html_url"],
                "updated": i["updated_at"][:10],
            }
            for i in data.get("items", [])
        ]
        return {"repos": repos, "total_count": data.get("total_count", 0), "query": query}
    except httpx.HTTPStatusError as e:
        return {"error": f"GitHub API error {e.response.status_code}",
                "retry_after": e.response.headers.get("Retry-After", "60"),
                "repos": [], "query": query}
    except Exception as e:
        return {"error": str(e), "repos": [], "query": query}


class HNInput(BaseModel):
    query: str = Field(description="Search query for HackerNews stories and discussions")
    max_results: int = Field(default=5, ge=1, le=10)


@tool("search_hackernews", args_schema=HNInput)
def search_hackernews(query: str, max_results: int = 5) -> dict:
    """Search HackerNews for stories and community discussions. An empty
    query returns the current front page instead of doing a keyword search."""
    try:
        if query.strip():
            params = {"query": query, "tags": "story", "hitsPerPage": max_results}
        else:
            # "What's trending on HN today" isn't a search query at all --
            # Algolia's relevance search over story titles returns nothing
            # for a generic phrase like that (no title literally contains
            # "trending ... today"). An empty topic means the caller wants
            # the actual current front page, which is its own endpoint.
            params = {"tags": "front_page", "hitsPerPage": max_results}
        r = httpx.get(
            "https://hn.algolia.com/api/v1/search",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        stories = [
            {
                "title": h.get("title", ""),
                "url": h.get("url", ""),
                "points": h.get("points", 0),
                "comments": h.get("num_comments", 0),
                "date": h.get("created_at", "")[:10],
            }
            for h in data.get("hits", [])
        ]
        return {"stories": stories, "count": len(stories), "query": query}
    except Exception as e:
        return {"error": str(e), "stories": [], "query": query}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    query: str
    final_response: str
    errors: Annotated[List[str], operator.add]
    iteration: int
    current_node: str
    github_results: List[dict]
    hn_stories: List[dict]
    google_results: List[dict]
    analysis: str
    chat_history: List[dict]   # [{'role': 'user'|'assistant', 'content': str}, ...]


# ---------------------------------------------------------------------------
# Guardrail -- now shared with the RAG path via guardrails.py (previously
# this module had its own private copy and the RAG path had none at all).
# ---------------------------------------------------------------------------

from .guardrails import sanitise_query  # noqa: E402


# Filler words to strip for GitHub search keyword extraction -- replaces an
# LLM call that previously did the same job (see fetch_node).
_FILLER_WORDS = {
    "what", "are", "is", "the", "a", "an", "to", "for", "on", "with", "of",
    "in", "and", "or", "that", "this", "it", "as", "at", "from", "by",
    "find", "popular", "best", "most", "open", "source", "frameworks",
    "framework", "please", "can", "you", "me", "about", "some", "any",
    "do", "does", "how", "would", "should", "could", "there", "we",
    # Time/recency words that survived as "keywords" and got sent straight
    # to GitHub's literal text search -- "python github right now" matched
    # any repo whose README happened to contain the words "right" or "now",
    # returning noise instead of actually popular repos.
    "right", "now", "today", "currently", "latest", "trending", "recently",
    "new", "lately", "nowadays", "these", "days",
    # Meta/platform words that name *where* to search, not *what* for --
    # redundant (and noisy) when sent to that same platform's own search API.
    "github", "repo", "repos", "repository", "repositories",
    "hackernews", "hacker", "news", "google", "search", "online",
    "internet", "web", "trend", "trends",
    # Generic request verbs/nouns that add no topical signal.
    "list", "show", "give", "tell", "want", "need", "using", "use",
    "project", "projects", "library", "libraries",
    "developer", "developers", "saying", "discussion", "discussing",
    "around", "community",
    # Generic connector/reference words -- meaningless as search topics on
    # their own, but common in "both docs-and-agent" combined questions
    # ("according to our docs, ... and what's trending on GitHub about X"),
    # where they used to crowd out the real topic at the 4-keyword cap.
    "according", "our", "based", "handle", "handles", "handling",
}


def extract_keywords_heuristic(query: str) -> str:
    """Strip filler words/prose down to a handful of significant keywords
    for the GitHub search tool, without calling the LLM."""
    tokens = re.findall(r"[A-Za-z0-9+#.]+", query.lower())
    keywords = [t for t in tokens if t not in _FILLER_WORDS and len(t) > 1]
    if not keywords:
        keywords = tokens  # fall back to the raw tokens rather than an empty query
    # Capped at 4 before, which -- on a longer combined "both docs-and-agent"
    # question -- let early, less specific words use up the whole budget and
    # crowd out the real topic mentioned later in the sentence (e.g. "CI/CD
    # tools" at the end getting dropped). 6 gives that headroom without
    # making the GitHub query so broad it stops matching anything specific.
    return " ".join(keywords[:6])


def extract_topic_or_empty(query: str) -> str:
    """Same filtering as extract_keywords_heuristic, but returns "" instead
    of falling back to raw tokens when nothing meaningful survives -- that
    empty result is the signal a caller (e.g. HackerNews) uses to tell "no
    specific topic, general trending request" apart from "a real topic that
    happens to be short"."""
    tokens = re.findall(r"[A-Za-z0-9+#.]+", query.lower())
    keywords = [t for t in tokens if t not in _FILLER_WORDS and len(t) > 1]
    return " ".join(keywords[:6])


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def fetch_node(state: AgentState) -> dict:
    query = state["query"]
    iteration = state.get("iteration", 0) + 1

    clean_query, flagged = sanitise_query(query)
    if flagged:
        return {
            "final_response": "Rejection: The provided input query failed safety validation criteria.",
            "current_node": "end",
            "iteration": iteration,
            "errors": ["Input query flagged as empty, malicious, or nonsensical by system guardrails."],
            "github_results": [],
            "hn_stories": [],
            "google_results": [],
        }

    # Heuristic keyword extraction instead of an LLM call -- this was adding a
    # full Ollama round trip (20-60s+ on CPU) just to strip filler words
    # before the GitHub search. A stopword filter does the same job for free.
    keywords = extract_keywords_heuristic(clean_query)
    # For HN specifically: a generic "what's trending on HN today" has no
    # real topic once filler words are stripped -- in that case pass "" so
    # search_hackernews fetches the actual front page instead of running a
    # near-empty/junk keyword search against story titles.
    hn_query = extract_topic_or_empty(clean_query)

    results_a = search_github.invoke({"query": keywords, "max_results": 5})
    results_b = search_hackernews.invoke({"query": hn_query, "max_results": 5})

    google_quota = google_search.quota_status()
    google_data = {"results": [], "error": None, "quota_exhausted": False}
    if google_quota["available"]:
        google_data = google_search.search_google(clean_query, max_results=5)

    execution_errors = []
    if "error" in results_a:
        execution_errors.append(f"GitHub: {results_a['error']}")
    if "error" in results_b:
        execution_errors.append(f"HN: {results_b['error']}")
    if google_data.get("error") and not google_data.get("quota_exhausted"):
        execution_errors.append(f"Google: {google_data['error']}")

    repos_list = results_a.get("repos", [])
    stories_list = results_b.get("stories", [])
    google_list = google_data.get("results", [])

    has_results = len(repos_list) > 0 or len(stories_list) > 0 or len(google_list) > 0

    # NOTE: deliberately NOT setting current_node='end' for the empty-results
    # case. The original graded submission did that here, which meant
    # route_after_fetch's guardrail check (`current_node == 'end'`) intercepted
    # every empty-results case on the very first attempt -- the iteration<3
    # retry logic below it was dead code. Using a neutral 'fetched' marker lets
    # route_after_fetch's retry/respond_empty branch actually run.
    return {
        "iteration": iteration,
        "current_node": "analyse" if has_results else "fetched",
        "github_results": repos_list,
        "hn_stories": stories_list,
        "google_results": google_list,
        "errors": execution_errors,
    }


def analyse_node(state: AgentState) -> dict:
    """Combined synthesis + formatting in a single LLM call.

    The graded submission split this into analyse_node (cross-reference
    synthesis) -> respond_node (reformat into the final structure), each a
    separate Ollama round trip. On CPU that's two 20-60s+ calls for every
    agent turn on top of the fetch_node keyword-extraction call, which made
    even a single agent response take minutes. Keyword extraction is now a
    heuristic (see extract_keywords_heuristic), and this merges the other
    two LLM calls into one -- a single prompt that asks for the cross-
    referenced insights AND the final Direct Answer/Supporting Details/
    Closing Summary structure at once. Happy path: 1 LLM call per turn
    instead of 3."""
    query = state["query"]
    results_a = state.get("github_results", [])
    results_b = state.get("hn_stories", [])
    results_c = state.get("google_results", [])

    clean_github = [
        {"name": r.get("name"), "description": r.get("description", "")[:120],
         "stars": r.get("stars", 0), "language": r.get("language", "Unknown")}
        for r in results_a
    ]
    clean_hn = [
        {"title": s.get("title", ""), "points": s.get("points", 0),
         "comments": s.get("comments", 0), "date": s.get("date", "Unknown")}
        for s in results_b
    ]
    clean_google = [
        {"title": g.get("title", ""), "snippet": g.get("snippet", "")[:200], "url": g.get("url", "")}
        for g in results_c
    ]

    history = state.get("chat_history", [])
    history_block = ""
    if history:
        recent = history[-6:]
        history_block = "\n--- RECENT CONVERSATION (for follow-up context) ---\n" + "\n".join(
            f"{h['role']}: {h['content'][:200]}" for h in recent
        )

    combined_prompt = f"""
    You are an elite Tech Research Analyst writing directly for the end user.
    Cross-reference code repository signals, community sentiment, and web
    results, then present your findings as a polished executive summary --
    in ONE pass, not as separate analysis and write-up stages.

    Original User Investigation: "{query}"
    {history_block}

    --- GITHUB REPOSITORIES ---
    {json.dumps(clean_github, indent=2)}

    --- HACKERNEWS SENTIMENT ---
    {json.dumps(clean_hn, indent=2)}

    --- GOOGLE SEARCH RESULTS ---
    {json.dumps(clean_google, indent=2)}

    --- WHAT YOUR ANALYSIS MUST COVER ---
    1. Cross-reference 4-5 specific insights from the sources above.
    2. Note where sources agree or conflict.
    3. Flag uncertainty: outdated data, small sample sizes, polarized opinions.

    --- OUTPUT FORMAT (write directly in this structure, 150-300 words total) ---
    Direct Answer: a clear, high-level answer to the user's question.
    Supporting Details: the specific repo/sentiment/web evidence behind it.
    Closing Summary: the key takeaway, including any uncertainty or risk flags.

    Write with an authoritative, accessible, professional tone. No filler
    intros like "Here is your report" -- begin writing the Direct Answer
    section immediately.
    """
    response = get_llm_creative().invoke([HumanMessage(content=combined_prompt)]).content.strip()

    if not response or len(response.strip()) < 20:
        return {
            "final_response": (
                f'No results were found for "{query}" across GitHub, HackerNews, or Google Search. '
                "Try rephrasing the question or asking something more specific."
            ),
            "current_node": "end",
        }

    # Anti-hallucination: check the response is actually grounded in the
    # source data it was supposed to summarise, not just plausible-sounding
    # text the model generated.
    source_text = json.dumps(clean_github) + json.dumps(clean_hn) + json.dumps(clean_google)
    check = guardrails.faithfulness_check(response, source_text)
    if check["flag"] == "weak":
        response += guardrails.HALLUCINATION_NOTICE

    return {"final_response": response, "current_node": "end"}


def respond_empty_node(state: AgentState) -> dict:
    """Fix for the route_after_fetch bug: reached only when fetch_node found
    nothing after exhausting retries. Produces an explanatory message instead
    of the original bug's empty final_response."""
    query = state["query"]
    return {
        "final_response": (
            f'I searched GitHub, HackerNews, and Google Search for "{query}" but could not find '
            "relevant results after multiple attempts. Try a more specific or differently-worded question."
        ),
        "current_node": "end",
    }


def route_after_fetch(state: AgentState) -> str:
    if state.get("current_node") == "end":
        return "end"

    repos = state.get("github_results", [])
    stories = state.get("hn_stories", [])
    google_results = state.get("google_results", [])
    iteration = state.get("iteration", 0)

    if len(repos) == 0 and len(stories) == 0 and len(google_results) == 0:
        if iteration < 3:
            return "fetch"
        # FIX: route to a node that produces a real message, not straight to END.
        return "respond_empty"

    return "analyse"


def build_agent_graph():
    builder = StateGraph(AgentState)
    builder.add_node("fetch_node", fetch_node)
    builder.add_node("analyse_node", analyse_node)
    builder.add_node("respond_empty_node", respond_empty_node)

    builder.add_edge(START, "fetch_node")
    builder.add_conditional_edges(
        "fetch_node",
        route_after_fetch,
        {
            "analyse": "analyse_node",
            "fetch": "fetch_node",
            "respond_empty": "respond_empty_node",
            "end": END,
        },
    )
    builder.add_edge("analyse_node", END)
    builder.add_edge("respond_empty_node", END)

    return builder.compile(checkpointer=MemorySaver())


_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent_graph()
    return _agent


def run_agent(query: str, thread_id: str = "run-1") -> dict:
    """FIX for the multi-turn memory gap: reads back the prior checkpoint via
    get_state() and carries forward chat_history, instead of always building a
    fresh initial state (which is what the graded submission did despite
    having MemorySaver wired up)."""
    agent = get_agent()
    config = {"configurable": {"thread_id": thread_id}}

    prior = agent.get_state(config)
    prior_history = []
    if prior and prior.values:
        prior_history = prior.values.get("chat_history", [])

    initial = {
        "query": query,
        "final_response": "",
        "errors": [],
        "iteration": 0,
        "current_node": "start",
        "github_results": [],
        "hn_stories": [],
        "google_results": [],
        "analysis": "",
        "chat_history": prior_history,
    }
    result = agent.invoke(initial, config=config)

    # Append this turn to chat_history so the next invocation on this thread_id
    # (same user coming back, or a follow-up question) sees it.
    updated_history = prior_history + [
        {"role": "user", "content": query},
        {"role": "assistant", "content": result.get("final_response", "")},
    ]
    agent.update_state(config, {"chat_history": updated_history})
    result["chat_history"] = updated_history
    return result
