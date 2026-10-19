"""
FastAPI app wiring together: accounts (auth.py), dispatcher (RAG + Agent),
chat history, document upload (10/session max), the admin page and the
AIOps health check — everything the frontend talks to.

Security/safety measures (see guardrails.py, rate_limit.py for the "why"):
  - Prompt-injection + input-sanity guardrail on every chat message (RAG path
    included — it previously had none).
  - Faithfulness/anti-hallucination check on both pipelines' answers.
  - Sign-in required for everything except the health probes; each thread
    is only readable by the account that created it.
  - thread_id format validation (must be a real UUID, not an arbitrary
    client-supplied string reaching Chroma/SQLite).
  - Per-thread rate limiting on chat and upload.
  - Upload size cap.
  - CORS restricted to configured origins instead of "*".

Run with:  uvicorn app.main:app --reload --port 8000
Requires:  GEMINI_API_KEY set (default), or LLM_PROVIDER=ollama with Ollama running.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import auth
from . import store
from . import dispatcher
from . import rag_core
from . import google_search
from . import file_extract
from . import guardrails
from . import cluster_ops
from . import llm
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
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

APP_VERSION = os.environ.get("APP_VERSION", "dev")

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB — generous for text/docx/pdf notes, not for abuse
CHAT_RATE_LIMIT = (20, 60)      # 20 messages / 60s per thread
UPLOAD_RATE_LIMIT = (10, 60)    # 10 uploads / 60s per thread
HEALTH_CHECK_RATE_LIMIT = (6, 60)  # each check reads the cluster + calls the LLM
AUTH_RATE_LIMIT = (10, 60)      # sign-in/sign-up attempts per IP, slows password guessing


@app.on_event("startup")
def startup():
    store.init_db()
    auth.ensure_admin_from_env()


def _require_valid_thread_id(thread_id: str) -> None:
    if not guardrails.is_valid_thread_id(thread_id):
        raise HTTPException(400, "Invalid thread_id.")


def _require_own_thread(thread_id: str, user: dict) -> None:
    """A thread id that exists must belong to this user. 404 rather than 403
    so other people's thread ids can't be confirmed by probing."""
    _require_valid_thread_id(thread_id)
    exists, owner = store.thread_owner(thread_id)
    if exists and owner != user["id"]:
        raise HTTPException(404, "Chat not found.")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class Credentials(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=auth.MAX_PASSWORD_LENGTH + 1)


def _auth_rate_limit(request: Request) -> None:
    if not check_rate_limit(f"auth:{auth.client_ip(request)}", *AUTH_RATE_LIMIT):
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")


@app.post("/api/auth/signup")
def signup(creds: Credentials, request: Request):
    _auth_rate_limit(request)
    username = creds.username.strip()
    auth.validate_credentials(username, creds.password)
    if store.get_user_by_username(username) is not None:
        raise HTTPException(409, "That username is taken.")
    try:
        user_id = store.create_user(username, auth.hash_password(creds.password))
    except Exception:
        raise HTTPException(409, "That username is taken.")
    user = {"id": user_id, "username": username, "is_admin": 0}
    return auth.start_session(user, request, "signup")


@app.post("/api/auth/login")
def login(creds: Credentials, request: Request):
    _auth_rate_limit(request)
    user = auth.authenticate(creds.username.strip(), creds.password)
    if user is None:
        raise HTTPException(401, "Wrong username or password.")
    return auth.start_session(user, request, "login")


@app.post("/api/auth/logout")
def logout(request: Request, authorization: Optional[str] = Header(None),
           user: dict = Depends(auth.current_user)):
    auth.end_session(authorization)
    store.log_access(user["id"], user["username"], "logout", auth.client_ip(request),
                     request.headers.get("user-agent", ""))
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(auth.current_user)):
    return auth.public_user(user)


# ---------------------------------------------------------------------------
# Admin: who used the app, when and from where. Deliberately no access to
# anyone's chats or uploaded documents.
# ---------------------------------------------------------------------------

@app.get("/api/admin/users")
def admin_users(admin: dict = Depends(auth.admin_user)):
    return store.list_users()


@app.get("/api/admin/activity")
def admin_activity(limit: int = 200, admin: dict = Depends(auth.admin_user)):
    return store.list_access_log(max(1, min(limit, 1000)))


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: dict = Depends(auth.admin_user)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't delete your own admin account.")
    users = {u["id"]: u for u in store.list_users()}
    if user_id not in users:
        raise HTTPException(404, "User not found.")
    if users[user_id]["is_admin"]:
        raise HTTPException(400, "Admin accounts are managed through ADMIN_USERNAME.")
    for thread_id in store.delete_user(user_id):
        rag_core.drop_session(thread_id)
    return {"deleted": users[user_id]["username"]}


# ---------------------------------------------------------------------------
# Health checks (Kubernetes liveness/readiness probes)
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    """Liveness: the process is up and serving requests."""
    return {"status": "ok", "version": APP_VERSION, "pod": os.environ.get("HOSTNAME", ""),
            "llm": llm.status()}


@app.get("/api/ready")
def ready():
    """Readiness: the database is reachable. A rolling update only sends
    traffic to a new pod once this passes."""
    try:
        store.ping()
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ready"}


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

    if not check_rate_limit(f"chat:{thread_id}", *CHAT_RATE_LIMIT):
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


# ---------------------------------------------------------------------------
# AIOps: one-click health check of the live deployment
# ---------------------------------------------------------------------------

@app.post("/api/ops/health-check")
def ops_health_check(thread_id: str = Form(...), user: dict = Depends(auth.current_user)):
    """Scans the app's Kubernetes namespace, flags problems with rule-based
    checks and has the LLM write an incident summary. The result is also
    saved into the chat so it stays in the conversation history."""
    _require_own_thread(thread_id, user)
    if not check_rate_limit(f"health:{thread_id}", *HEALTH_CHECK_RATE_LIMIT):
        raise HTTPException(429, "Too many health checks — try again shortly.")

    session = rag_core.get_or_create_session(thread_id)
    try:
        result = cluster_ops.health_check(rag_session=session)
    except Exception as e:
        if llm.is_quota_error(e):
            raise HTTPException(429, llm.QUOTA_MESSAGE)
        raise

    label = {"healthy": "Healthy", "warning": "Warning", "critical": "Critical",
             "unavailable": "Unavailable"}[result["status"]]
    lines = [f"Health check: {label}", ""]
    for issue in result["issues"]:
        lines.append(f"[{issue['severity'].upper()}] {issue['object']}: {issue['problem']}")
    if result["issues"]:
        lines.append("")
    lines.append(result["summary"])
    text = "\n".join(lines)

    store.ensure_thread(thread_id, title_hint="Cluster health check", user_id=user["id"])
    store.add_message(thread_id, "user", "Run a cluster health check")
    store.add_message(thread_id, "assistant", text, source="ops",
                      citations=json.dumps(result["citations"]) if result["citations"] else None)
    store.set_title_from_first_message(thread_id, "Cluster health check")

    return {"thread_id": thread_id, "status": result["status"], "issues": result["issues"],
            "text": text, "citations": result["citations"]}
