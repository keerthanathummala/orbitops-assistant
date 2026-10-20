"""
The auth service: sign-up, sign-in and the admin page, as its own
Deployment (k8s/23-auth.yaml). It loads no models, so it starts in a few
seconds and needs a fraction of the chat service's memory.

Run with:  uvicorn app.auth_service:app --port 8000
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI

from . import auth
from . import auth_api
from . import common
from . import store

CHAT_SERVICE_URL = os.environ.get("CHAT_SERVICE_URL", "http://backend:8000")

app = FastAPI(title="OrbitOps Assistant auth service")
common.add_cors(app)
common.add_probes(app, "auth")
app.include_router(auth_api.router)


def _drop_on_chat_service(thread_ids):
    """Deleted chats' vector collections live in Chroma, which only the chat
    service talks to, so ask it (inside the cluster) to remove them. This
    reaches one chat pod; the others forget the chats on their own (see
    rag_core._prune_deleted_sessions)."""
    thread_ids = list(thread_ids)
    if not thread_ids:
        return
    try:
        httpx.post(f"{CHAT_SERVICE_URL}/internal/threads/drop",
                   json={"thread_ids": thread_ids}, timeout=10).raise_for_status()
    except httpx.HTTPError as e:
        # The account is already deleted; leftover collections are harmless
        # but worth seeing in the logs.
        print(f"Could not remove the deleted chats' vector collections: {e}", flush=True)


auth_api.on_threads_deleted = _drop_on_chat_service


@app.on_event("startup")
def startup():
    store.init_db()
    auth.ensure_admin_from_env()
