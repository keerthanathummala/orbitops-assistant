"""
FastAPI app wiring together: dispatcher (RAG + Agent), SQLite chat history,
document upload (10/session max), and Google quota status — everything the
frontend (Compass) talks to.

Security/safety measures (see guardrails.py, rate_limit.py for the "why"):
  - Prompt-injection + input-sanity guardrail on every chat message (RAG path
    included — it previously had none).
  - Faithfulness/anti-hallucination check on both pipelines' answers.
  - thread_id format validation (must be a real UUID, not an arbitrary
    client-supplied string reaching Chroma/SQLite).
  - Per-thread rate limiting on chat and upload.
  - Upload size cap.
  - CORS restricted to configured origins instead of "*".

Run with:  uvicorn app.main:app --reload --port 8000
Requires:  Ollama running locally with `qwen2.5:7b` pulled (`ollama serve`).
"""

from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import store
from . import dispatcher
from . import rag_core
from . import google_search
from . import file_extract
from . import guardrails
from .rate_limit import check_rate_limit

app = FastAPI(title="OrbitOps Assistant API")

# CORS: default to local dev origins only. Set FRONTEND_ORIGINS (comma-
# separated) to your deployed frontend's real origin before going live —
# "*" lets any website's JavaScript call this API using a visitor's browser.
_origins_env = os.environ.get("FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB — generous for text/docx/pdf notes, not for abuse
CHAT_RATE_LIMIT = (20, 60)      # 20 messages / 60s per thread
UPLOAD_RATE_LIMIT = (10, 60)    # 10 uploads / 60s per thread


@app.on_event("startup")
def startup():
    store.init_db()


def _require_valid_thread_id(thread_id: str) -> None:
    if not guardrails.is_valid_thread_id(thread_id):
        raise HTTPException(400, "Invalid thread_id.")


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
def chat(req: ChatRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(400, "message must not be empty")

    # thread_id is client-supplied with no login behind it -- only trust it
    # if it's actually a UUID this app would have generated.
    if req.thread_id is not None:
        _require_valid_thread_id(req.thread_id)
    thread_id = req.thread_id or store.new_thread_id()

    if not check_rate_limit(f"chat:{thread_id}", *CHAT_RATE_LIMIT):
        raise HTTPException(429, "Too many messages — slow down and try again shortly.")

    store.ensure_thread(thread_id, title_hint=req.message)
    store.add_message(thread_id, "user", req.message)
    result = dispatcher.handle_message(thread_id, req.message)

    for seg in result["segments"]:
        store.add_message(
            thread_id, "assistant", seg["text"], source=seg["source"],
            citations=json.dumps(seg["citations"]) if seg["citations"] else None,
        )

    store.set_title_from_first_message(thread_id, req.message)

    return {"thread_id": thread_id, "route": result["route"], "segments": result["segments"]}


# ---------------------------------------------------------------------------
# Returning to previous chats (no login — frontend tracks its own thread_ids)
# ---------------------------------------------------------------------------

@app.get("/api/threads")
def list_threads(ids: str = ""):
    thread_ids = [t for t in ids.split(",") if t and guardrails.is_valid_thread_id(t)]
    return store.list_threads(thread_ids)


@app.get("/api/threads/{thread_id}/messages")
def thread_messages(thread_id: str):
    _require_valid_thread_id(thread_id)
    return store.get_messages(thread_id)


# ---------------------------------------------------------------------------
# Document upload (max 10 per session, added on top of the base corpus)
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_document(thread_id: str = Form(...), file: UploadFile = File(...)):
    _require_valid_thread_id(thread_id)

    if not check_rate_limit(f"upload:{thread_id}", *UPLOAD_RATE_LIMIT):
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


# ---------------------------------------------------------------------------
# Status pills (docs indexed, Google quota)
# ---------------------------------------------------------------------------

@app.get("/api/status")
def status(thread_id: str):
    _require_valid_thread_id(thread_id)
    docs_indexed = dispatcher.docs_indexed_count(thread_id)
    return {"docs_indexed": docs_indexed, "google_search": google_search.quota_status()}
