"""
Account and admin endpoints (/api/auth/*, /api/admin/*).

On Kubernetes these run in their own auth service (auth_service.py), so a
burst of chat traffic or a slow model load on the chat pods never stops
people signing in, and the admin page stays up on its own. Run locally,
main.py mounts the same router so one process still serves everything.
Sessions live in the shared database, so the chat and ops services check a
token with one lookup and never need to call this service.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import auth
from . import store
from .rate_limit import check_rate_limit

AUTH_RATE_LIMIT = (10, 60)      # sign-in/sign-up attempts per IP, slows password guessing

router = APIRouter()

# Called with a deleted user's thread ids so their document indexes go too.
# main.py points this at rag_core; the auth service asks the chat service.
on_threads_deleted: Callable[[Iterable[str]], None] = lambda thread_ids: None


class Credentials(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=auth.MAX_PASSWORD_LENGTH + 1)


def _auth_rate_limit(request: Request) -> None:
    if not check_rate_limit(f"auth:{auth.client_ip(request)}", *AUTH_RATE_LIMIT):
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")


@router.post("/api/auth/signup")
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


@router.post("/api/auth/login")
def login(creds: Credentials, request: Request):
    _auth_rate_limit(request)
    user = auth.authenticate(creds.username.strip(), creds.password)
    if user is None:
        raise HTTPException(401, "Wrong username or password.")
    return auth.start_session(user, request, "login")


@router.post("/api/auth/logout")
def logout(request: Request, authorization: Optional[str] = Header(None),
           user: dict = Depends(auth.current_user)):
    auth.end_session(authorization)
    store.log_access(user["id"], user["username"], "logout", auth.client_ip(request),
                     request.headers.get("user-agent", ""))
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: dict = Depends(auth.current_user)):
    return auth.public_user(user)


# ---------------------------------------------------------------------------
# Admin: who used the app, when and from where. Deliberately no access to
# anyone's chats or uploaded documents.
# ---------------------------------------------------------------------------

@router.get("/api/admin/users")
def admin_users(admin: dict = Depends(auth.admin_user)):
    return store.list_users()


@router.get("/api/admin/activity")
def admin_activity(limit: int = 200, admin: dict = Depends(auth.admin_user)):
    return store.list_access_log(max(1, min(limit, 1000)))


@router.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: dict = Depends(auth.admin_user)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't delete your own admin account.")
    users = {u["id"]: u for u in store.list_users()}
    if user_id not in users:
        raise HTTPException(404, "User not found.")
    if users[user_id]["is_admin"]:
        raise HTTPException(400, "Admin accounts are managed through ADMIN_USERNAME.")
    thread_ids = store.delete_user(user_id)
    try:
        on_threads_deleted(thread_ids)
    except Exception:
        # The account and chats are already gone; a leftover vector
        # collection is harmless and nobody can reach it any more.
        pass
    return {"deleted": users[user_id]["username"]}
