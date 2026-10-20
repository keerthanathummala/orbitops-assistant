"""
Minimal rate limiter: Redis-backed when REDIS_URL is set, in-memory otherwise.

Once deployed, this app is reachable by strangers hitting a single shared
Ollama instance (slow CPU inference) and a shared, global Google Search
quota. Without any throttling, one person -- or one script -- can tie up
the only LLM worker for everyone else, or burn through the whole day's
Google quota in seconds. This isn't trying to stop a sophisticated
attacker; it's the minimum needed so one bad actor (or one buggy client
retry loop) can't take the app down for everyone else.

The in-memory version is per-process: fine for one backend instance, but
with N replicas behind a load balancer each pod keeps its own count, so the
real limit becomes N times the intended one. On Kubernetes REDIS_URL points
every pod at one shared Redis, so the limit holds across replicas. If Redis
is unreachable, each pod falls back to its own in-memory count rather than
taking the whole app down over a rate limiter.
"""

from __future__ import annotations

import os
import time
import threading
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict = defaultdict(deque)


REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None
REDIS_RETRY_AFTER = 10       # seconds to skip Redis after it failed
_redis_down_until = 0.0


def _get_redis():
    global _redis
    if _redis is None:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        # Short timeouts and no retries: the client's default retries made
        # each request wait ~5 s when Redis was down.
        _redis = redis.Redis.from_url(REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5,
                                      retry=Retry(NoBackoff(), 0))
    return _redis


def _check_redis(key: str, max_requests: int, window_seconds: int) -> bool:
    """True/False from the shared counter, or None if Redis can't be reached."""
    global _redis_down_until
    if time.monotonic() < _redis_down_until:
        return None
    # Fixed window: one counter per key per window, expiring with it.
    window = int(time.time() // window_seconds)
    redis_key = f"ratelimit:{key}:{window}"
    try:
        pipe = _get_redis().pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds)
        count, _ = pipe.execute()
    except Exception:
        _redis_down_until = time.monotonic() + REDIS_RETRY_AFTER
        return None
    return count <= max_requests


def _check_memory(key: str, max_requests: int, window_seconds: int) -> bool:
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


def check_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """Returns True if the request is allowed, False if the key has exceeded
    max_requests within the trailing window_seconds. If Redis is down, each
    pod falls back to its own in-memory count for a while, so limits (the
    sign-in one in particular) keep working instead of switching off."""
    if REDIS_URL:
        allowed = _check_redis(key, max_requests, window_seconds)
        if allowed is not None:
            return allowed
    return _check_memory(key, max_requests, window_seconds)
