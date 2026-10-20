"""
Accounts: sign-up / sign-in with a username and password, bearer-token
sessions, and the admin role.

  - Passwords are hashed with scrypt (Python stdlib) and a random salt; the
    password itself is never stored or logged.
  - A session token is a random 256-bit string returned once at sign-in. The
    database only keeps its sha256, so a leaked database can't be replayed
    as a session. Sessions last SESSION_DAYS and live in the shared
    database, so every backend replica accepts them.
  - The admin account comes from ADMIN_USERNAME / ADMIN_PASSWORD (secrets),
    created or reset at startup. There is no sign-up path to admin.
  - Each sign-up, sign-in and sign-out is written to the access log with the
    time, client IP and browser, which is what the admin page shows.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException, Request

from . import store

SESSION_DAYS = 7
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


# Verifying against this when the username doesn't exist keeps sign-in
# timing the same either way, so it can't be used to discover usernames.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def validate_credentials(username: str, password: str) -> None:
    if not USERNAME_RE.match(username or ""):
        raise HTTPException(400, "Username must be 3-32 characters: letters, numbers, . _ or -")
    if not (MIN_PASSWORD_LENGTH <= len(password or "") <= MAX_PASSWORD_LENGTH):
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request: Request) -> str:
    """nginx sets X-Real-IP to the address it received the request from. On
    GKE the frontend Service uses externalTrafficPolicy: Local so that is the
    visitor's real address rather than a node's."""
    return (request.headers.get("x-real-ip")
            or (request.client.host if request.client else "")
            or "unknown")[:64]


def start_session(user: dict, request: Request, event: str) -> dict:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    store.create_session(_token_hash(token), user["id"], expires.isoformat())
    ip = client_ip(request)
    store.log_access(user["id"], user["username"], event, ip, request.headers.get("user-agent", ""))
    store.touch_user(user["id"], ip)
    return {"token": token, "user": public_user(user)}


def public_user(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])}


def authenticate(username: str, password: str) -> Optional[dict]:
    user = store.get_user_by_username(username or "")
    if user is None:
        verify_password(password or "", _DUMMY_HASH)
        return None
    return user if verify_password(password or "", user["password_hash"]) else None


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please sign in.")
    return authorization[7:].strip()


def current_user(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    """FastAPI dependency: the signed-in user, or 401."""
    token = _bearer(authorization)
    row = store.get_session_user(_token_hash(token))
    if row is None or row["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    # Record activity at most once a minute per user, not on every request.
    last = row.get("last_seen_at") or ""
    if last < (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat():
        store.touch_user(row["id"], client_ip(request))
    return row


def admin_user(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    user = current_user(request, authorization)
    if not user["is_admin"]:
        raise HTTPException(403, "Admins only.")
    return user


def end_session(authorization: Optional[str]) -> None:
    store.delete_session(_token_hash(_bearer(authorization)))


def ensure_admin_from_env() -> None:
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    # Printed at startup so a missing or invalid admin setting is visible in
    # the backend window / pod logs instead of only as a failed sign-in.
    if not username or not password:
        print("Admin account: not configured (set ADMIN_USERNAME and ADMIN_PASSWORD).", flush=True)
        return
    try:
        validate_credentials(username, password)
    except HTTPException as e:
        print(f"Admin account: not created. {e.detail}", flush=True)
        return
    print(f"Admin account: '{username}' is ready.", flush=True)
    try:
        store.upsert_admin(username, hash_password(password))
    except Exception:
        # Two replicas starting together can race on the INSERT; the other
        # one created the account, which is all this needs.
        pass
