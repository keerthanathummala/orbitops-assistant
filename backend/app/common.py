"""
Pieces every OrbitOps service shares: CORS, the Kubernetes probe endpoints,
and the thread-ownership check.

The backend is split into three services that use the same code and
database (see README "Architecture"):
  - chat  (app.main)        chat, history, uploads, docs/web answers
  - auth  (app.auth_service) sign-up, sign-in, admin page
  - ops   (app.ops_service)  reads the cluster for the AIOps features
Each one runs these probes so Kubernetes can restart it or hold traffic
back from it independently.
"""

from __future__ import annotations

import os
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import guardrails
from . import llm
from . import store

APP_VERSION = os.environ.get("APP_VERSION", "dev")


def add_cors(app: FastAPI) -> None:
    # Default to local dev origins only. Set FRONTEND_ORIGINS (comma-
    # separated) to your deployed frontend's real origin before going live --
    # "*" lets any website's JavaScript call this API using a visitor's browser.
    origins_env = os.environ.get("FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins_env.split(",") if o.strip()],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )


def add_probes(app: FastAPI, service: str) -> None:
    @app.get("/api/health")
    def health():
        """Liveness: the process is up and serving requests."""
        return {"status": "ok", "service": service, "version": APP_VERSION,
                "pod": os.environ.get("HOSTNAME", ""), "llm": llm.status()}

    @app.get("/api/ready")
    def ready():
        """Readiness: startup has finished (database schema created, models
        loaded), which is all a rolling update needs to wait for. It
        deliberately doesn't re-check MySQL: if MySQL restarts, every pod
        failing readiness at once would take the whole API off the load
        balancer. Pods stay in service and answer with a clear 503 instead
        (see add_db_error_handler)."""
        return {"status": "ready", "service": service}

    add_db_error_handler(app)


def _db_errors() -> tuple:
    errors = [sqlite3.OperationalError]
    try:
        import pymysql
        errors += [pymysql.err.OperationalError, pymysql.err.InterfaceError]
    except ImportError:
        pass
    return tuple(errors)


def add_db_error_handler(app: FastAPI) -> None:
    """While the database is unreachable (MySQL restarting or moving to
    another node), answer with a 503 and a message the frontend can show,
    rather than an unexplained 500."""
    async def handler(request: Request, exc: Exception):
        return JSONResponse(status_code=503, content={
            "detail": "The database is unavailable for a moment. Please try again shortly."})

    for error in _db_errors():
        app.add_exception_handler(error, handler)


def require_valid_thread_id(thread_id: str) -> None:
    if not guardrails.is_valid_thread_id(thread_id):
        raise HTTPException(400, "Invalid thread_id.")


def require_own_thread(thread_id: str, user: dict) -> None:
    """A thread id that exists must belong to this user. 404 rather than 403
    so other people's thread ids can't be confirmed by probing."""
    require_valid_thread_id(thread_id)
    exists, owner = store.thread_owner(thread_id)
    if exists and owner != user["id"]:
        raise HTTPException(404, "Chat not found.")
