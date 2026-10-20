"""
Google Custom Search JSON API tool — free tier, 100 queries/day.

The quota is GLOBAL (tied to one API key shared by every visitor to the
deployed app, not per-user) and resets at midnight Pacific Time. When it's
exhausted, only this tool is disabled for the rest of the day — the rest of
the chat (RAG + GitHub/HackerNews agent) keeps working. This matches the
explicit decision made while designing this project: "disable just the
Google piece".

The counter is kept in the shared database (store.py), so every chat pod
counts against the same 100 and a restart doesn't reset a quota that
Google's side has not actually reset. It used to be a JSON file inside each
pod, which let N pods make N x 100 searches.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from . import store

DAILY_LIMIT = 100
PACIFIC_OFFSET_STD = timedelta(hours=-8)   # PST; good enough without a tz database dependency

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")


def _pacific_date_str(moment: Optional[datetime] = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    pacific = moment + PACIFIC_OFFSET_STD
    return pacific.strftime("%Y-%m-%d")


def quota_status() -> dict:
    """Used · limit for today's (Pacific) quota, and whether search is available."""
    configured = bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)
    try:
        used = store.google_quota_used(_pacific_date_str())
    except Exception:
        used = 0  # database briefly unavailable: don't fail the status pill
    return {
        "used": used,
        "limit": DAILY_LIMIT,
        "available": used < DAILY_LIMIT and configured,
        "configured": configured,
    }


def search_google(query: str, max_results: int = 5) -> dict:
    """Search the web via Google Custom Search JSON API.

    Returns {'results': [...], 'error': str|None, 'quota_exhausted': bool}.
    Caller MUST check quota_status()/quota_exhausted before presenting results,
    and should fall back to RAG/GitHub/HackerNews when this tool is unavailable.
    """
    if not (GOOGLE_API_KEY and GOOGLE_CSE_ID):
        return {"results": [], "error": "Google Search is not configured on this deployment.",
                "quota_exhausted": False}
    # Reserve the search before making it, so pods can't overshoot the limit
    # together; a request that fails gives its search back.
    day = _pacific_date_str()
    try:
        reserved = store.google_quota_reserve(day, DAILY_LIMIT)
    except Exception:
        return {"results": [], "error": "Google Search is unavailable for a moment.",
                "quota_exhausted": False}
    if not reserved:
        return {"results": [], "error": "Daily Google Search quota reached — try again tomorrow.",
                "quota_exhausted": True}

    try:
        r = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": GOOGLE_API_KEY,
                "cx": GOOGLE_CSE_ID,
                "q": query,
                "num": min(max_results, 10),
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = [
            {
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
            }
            for item in data.get("items", [])
        ]
        return {"results": results, "error": None, "quota_exhausted": False}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            store.google_quota_exhaust(day, DAILY_LIMIT)
            return {"results": [], "error": "Daily Google Search quota reached — try again tomorrow.",
                    "quota_exhausted": True}
        store.google_quota_release(day)
        return {"results": [], "error": f"Google Search API error {e.response.status_code}",
                "quota_exhausted": False}
    except Exception as e:
        store.google_quota_release(day)
        return {"results": [], "error": str(e), "quota_exhausted": False}
