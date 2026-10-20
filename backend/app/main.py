"""
The chat service: chat (dispatcher: RAG + Agent), chat history and
document upload (10/session max) -- the part of the app that does the heavy
lifting and scales out under load.

Accounts/admin (auth_api.py) and the AIOps features (ops_api.py) run as
their own services on Kubernetes. When AUTH_SERVICE_URL / OPS_SERVICE_URL
aren't set (running locally with one uvicorn), this app mounts those
routers itself so a single process still serves everything.

Security/safety measures (see guardrails.py, rate_limit.py for the "why"):
  - Prompt-injection + input-sanity guardrail on every chat message (RAG path
    included).
  - Faithfulness/anti-hallucination check on both pipelines' answers.
  - Sign-in required for everything except the health probes; each thread
    is only readable by the account that created it.
  - thread_id format validation (must be a real UUID, not an arbitrary
    client-supplied string reaching Chroma/SQLite).
  - Per-account rate limiting on chat and upload.
  - Upload size cap.
  - CORS restricted to configured origins instead of "*".

Run with:  uvicorn app.main:app --reload --port 8000
Requires:  GEMINI_API_KEY set (default), or LLM_PROVIDER=ollama with Ollama running.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import auth
from . import common
from . import store
from . import dispatcher
from . import rag_core
from . import google_search
from . import file_extract
from . import guardrails
from . import llm
from .rate_limit import check_rate_limit

app = FastAPI(title="OrbitOps Assistant API")
common.add_cors(app)
common.add_probes(app, "chat")

# Separate services on Kubernetes; mounted here when running as one process.
SPLIT_AUTH = bool(os.environ.get("AUTH_SERVICE_URL"))
SPLIT_OPS = bool(os.environ.get("OPS_SERVICE_URL"))

if not SPLIT_AUTH:
    from . import auth_api
    auth_api.on_threads_deleted = lambda thread_ids: [rag_core.drop_session(t) for t in thread_ids]
    app.include_router(auth_api.router)

if not SPLIT_OPS:
    from . import ops_api
    app.include_router(ops_api.router)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB — generous for text/docx/pdf notes, not for abuse
# Per account, not per chat: starting a new chat for every message must not
# get around the limit and use up the shared Gemini and Google quota.
CHAT_RATE_LIMIT = (20, 60)      # 20 messages / 60s per account
UPLOAD_RATE_LIMIT = (10, 60)    # 10 uploads / 60s per account


@app.on_event("startup")
def startup():
    store.init_db()
    if not SPLIT_AUTH:
        auth.ensure_admin_from_env()
    rag_core.warm_up()


_require_own_thread = common.require_own_thread


# ---------------------------------------------------------------------------
# Internal: called by the auth service when the admin deletes an account.
# Not reachable from outside the cluster (nginx only proxies /api/).
# ---------------------------------------------------------------------------

class DropThreads(BaseModel):
    thread_ids: List[str]


@app.post("/internal/threads/drop")
def internal_drop_threads(req: DropThreads):
    for thread_id in req.thread_ids:
        if guardrails.is_valid_thread_id(thread_id):
            rag_core.drop_session(thread_id)
    return {"dropped": len(req.thread_ids)}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    thread_id: Optional[str] = None
    message: str = Field(..., max_length=guardrails.MAX_QUERY_LENGTH + 50)


class ChatResponse(BaseModel):
    thread_id: str
    route: str
    segments: list


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: dict = Depends(auth.current_user)):
    if not req.message or not req.message.strip():
        raise HTTPException(400, "message must not be empty")

    if req.thread_id is not None:
        _require_own_thread(req.thread_id, user)
    thread_id = req.thread_id or store.new_thread_id()

    if not check_rate_limit(f"chat:{user['id']}", *CHAT_RATE_LIMIT):
        raise HTTPException(429, "Too many messages — slow down and try again shortly.")

    store.ensure_thread(thread_id, title_hint=req.message, user_id=user["id"])
    try:
        result = dispatcher.handle_message(thread_id, req.message)
    except Exception as e:
        if llm.is_quota_error(e):
            raise HTTPException(429, llm.QUOTA_MESSAGE)
        raise
    store.add_message(thread_id, "user", req.message)

    for seg in result["segments"]:
        store.add_message(
            thread_id, "assistant", seg["text"], source=seg["source"],
            citations=json.dumps(seg["citations"]) if seg["citations"] else None,
        )

    store.set_title_from_first_message(thread_id, req.message)

    return {"thread_id": thread_id, "route": result["route"], "segments": result["segments"]}


# ---------------------------------------------------------------------------
# Returning to previous chats (the signed-in user's own)
# ---------------------------------------------------------------------------

@app.get("/api/threads")
def list_threads(user: dict = Depends(auth.current_user)):
    return store.list_threads(user["id"])


@app.get("/api/threads/{thread_id}/messages")
def thread_messages(thread_id: str, user: dict = Depends(auth.current_user)):
    _require_own_thread(thread_id, user)
    return store.get_messages(thread_id)


# ---------------------------------------------------------------------------
# Document upload (max 10 per session, added on top of the base corpus)
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_document(thread_id: str = Form(...), file: UploadFile = File(...),
                          user: dict = Depends(auth.current_user)):
    _require_own_thread(thread_id, user)
    store.ensure_thread(thread_id, title_hint=file.filename or "New chat", user_id=user["id"])

    if not check_rate_limit(f"upload:{user['id']}", *UPLOAD_RATE_LIMIT):
        raise HTTPException(429, "Too many uploads — slow down and try again shortly.")

    session = rag_core.get_or_create_session(thread_id)
    if session.uploaded_count >= rag_core.MAX_UPLOADS:
        raise HTTPException(400, f"Upload limit reached ({rag_core.MAX_UPLOADS} documents max).")

    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File too large — {MAX_UPLOAD_BYTES // (1024*1024)}MB max.")

    try:
        text = file_extract.extract_text(file.filename, raw_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not text.strip():
        raise HTTPException(400, f"No extractable text found in {file.filename}.")

    try:
        session.add_uploaded_document(file.filename, text)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"uploaded": file.filename, "uploaded_count": session.uploaded_count,
            "max_uploads": rag_core.MAX_UPLOADS}


@app.get("/api/uploads")
def list_uploads(thread_id: str, user: dict = Depends(auth.current_user)):
    _require_own_thread(thread_id, user)
    return store.list_uploads(thread_id)


@app.delete("/api/uploads/{upload_id}")
def remove_upload(upload_id: int, thread_id: str, user: dict = Depends(auth.current_user)):
    """Remove one uploaded document from a chat, freeing a slot under the
    10-file limit. Every replica drops it from its index on its next sync."""
    _require_own_thread(thread_id, user)
    if not store.delete_upload(thread_id, upload_id):
        raise HTTPException(404, "File not found.")
    session = rag_core.get_or_create_session(thread_id)
    return {"removed": upload_id, "uploaded_count": session.uploaded_count,
            "max_uploads": rag_core.MAX_UPLOADS}


# ---------------------------------------------------------------------------
# Status pills (docs indexed, Google quota)
# ---------------------------------------------------------------------------

@app.get("/api/status")
def status(thread_id: str, user: dict = Depends(auth.current_user)):
    _require_own_thread(thread_id, user)
    docs_indexed = dispatcher.docs_indexed_count(thread_id)
    return {"docs_indexed": docs_indexed, "google_search": google_search.quota_status()}
