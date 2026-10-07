"""
Minimal in-memory rate limiter.

Once deployed, this app is reachable by strangers hitting a single shared
Ollama instance (slow CPU inference) and a shared, global Google Search
quota. Without any throttling, one person -- or one script -- can tie up
the only LLM worker for everyone else, or burn through the whole day's
Google quota in seconds. This isn't trying to stop a sophisticated
attacker; it's the minimum needed so one bad actor (or one buggy client
retry loop) can't take the app down for everyone else.

In-memory and per-process: fine for a single backend instance, which is
what this project is scoped to run as. A real multi-instance deployment
would need a shared store (Redis) instead -- noted, not built, since
that's beyond what one free-tier VM needs.
"""

from __future__ import annotations

import time
import threading
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict = defaultdict(deque)


def check_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """Returns True if the request is allowed, False if the key has exceeded
    max_requests within the trailing window_seconds."""
    now = time.monotonic()
    with _lock:
        bucket = _hits[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_requests:
            return False
        bucket.append(now)
        return True
