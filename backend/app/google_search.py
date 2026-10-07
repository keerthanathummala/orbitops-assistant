"""
Google Custom Search JSON API tool — free tier, 100 queries/day.

The quota is GLOBAL (tied to one API key shared by every visitor to the
deployed app, not per-user) and resets at midnight Pacific Time. When it's
exhausted, only this tool is disabled for the rest of the day — the rest of
the chat (RAG + GitHub/HackerNews agent) keeps working. This matches the
explicit decision made while designing this project: "disable just the
Google piece".

State is persisted to a small JSON file so the counter survives backend
restarts (otherwise a restart would silently reset a quota that Google's
side has not actually reset).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx

DAILY_LIMIT = 100
PACIFIC_OFFSET_STD = timedelta(hours=-8)   # PST; good enough without a tz database dependency
STATE_PATH = Path(__file__).resolve().parent / "google_quota_state.json"

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")


def _pacific_date_str(moment: Optional[datetime] = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    pacific = moment + PACIFIC_OFFSET_STD
    return pacific.strftime("%Y-%m-%d")


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"date": _pacific_date_str(), "count": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def _current_count() -> dict:
    state = _load_state()
    today = _pacific_date_str()
    if state.get("date") != today:
        state = {"date": today, "count": 0}
        _save_state(state)
    return state


def quota_status() -> dict:
    """Used · limit for today's (Pacific) quota, and whether search is available."""
    state = _current_count()
    return {
        "used": state["count"],
        "limit": DAILY_LIMIT,
        "available": state["count"] < DAILY_LIMIT and bool(GOOGLE_API_KEY and GOOGLE_CSE_ID),
        "configured": bool(GOOGLE_API_KEY and GOOGLE_CSE_ID),
    }


def _increment() -> None:
    state = _current_count()
    state["count"] += 1
    _save_state(state)


def search_google(query: str, max_results: int = 5) -> dict:
    """Search the web via Google Custom Search JSON API.

    Returns {'results': [...], 'error': str|None, 'quota_exhausted': bool}.
    Caller MUST check quota_status()/quota_exhausted before presenting results,
    and should fall back to RAG/GitHub/HackerNews when this tool is unavailable.
    """
    status = quota_status()
    if not status["configured"]:
        return {"results": [], "error": "Google Search is not configured on this deployment.",
                "quota_exhausted": False}
    if not status["available"]:
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
        _increment()  # count the call only once it actually succeeds
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
        return {"results": [], "error": f"Google Search API error {e.response.status_code}",
                "quota_exhausted": False}
    except Exception as e:
        return {"results": [], "error": str(e), "quota_exhausted": False}
