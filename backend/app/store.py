"""
Persistence for chat history and uploaded documents.

MySQL when MYSQL_HOST is set (the cloud deployment: one database shared by
every backend replica, so a chat's history doesn't depend on which pod the
load balancer picks, and survives rolling updates). Otherwise a local SQLite
file, so the app still runs on a dev machine with no database server.

Replaces relying on LangGraph's in-memory MemorySaver for durability: that
checkpointer only lives as long as the backend process does, so a restart
would wipe every conversation. This module persists the full, display-ready
transcript (both RAG and Agent turns) per thread_id to disk, independent of
the agent's own internal graph state.

Accounts: every thread belongs to the user who created it (threads.user_id),
and only that user can read it. The admin can list accounts and their
login history (time, IP address, browser) and delete accounts, but no
endpoint gives the admin access to anyone's chats.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

MYSQL_HOST = os.environ.get("MYSQL_HOST", "")
DB_PATH = Path(os.environ.get("SQLITE_PATH", Path(__file__).resolve().parent / "chat_history.db"))

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id   TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT NOT NULL,
    role        TEXT NOT NULL,       -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    source      TEXT,                -- 'rag' | 'agent' | 'both' | NULL (user turns)
    citations   TEXT,                -- JSON string, RAG doc citations if any
    created_at  TEXT NOT NULL,
    FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
);

CREATE TABLE IF NOT EXISTS uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT NOT NULL,
    filename    TEXT NOT NULL,
    content     TEXT NOT NULL,       -- extracted text, not the raw file
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,     -- scrypt (see auth.py), never the password itself
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    last_ip       TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,    -- sha256 of the bearer token
    user_id     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    username    TEXT NOT NULL,       -- kept after the account is deleted
    event       TEXT NOT NULL,       -- 'signup' | 'login' | 'logout'
    ip          TEXT,
    user_agent  TEXT,
    created_at  TEXT NOT NULL
)
"""

MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id   VARCHAR(36) PRIMARY KEY,
    title       VARCHAR(255) NOT NULL,
    created_at  VARCHAR(40) NOT NULL
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS messages (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    thread_id   VARCHAR(36) NOT NULL,
    role        VARCHAR(16) NOT NULL,
    content     MEDIUMTEXT NOT NULL,
    source      VARCHAR(16),
    citations   TEXT,
    created_at  VARCHAR(40) NOT NULL,
    INDEX idx_messages_thread (thread_id),
    FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS uploads (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    thread_id   VARCHAR(36) NOT NULL,
    filename    VARCHAR(255) NOT NULL,
    content     MEDIUMTEXT NOT NULL,
    created_at  VARCHAR(40) NOT NULL,
    INDEX idx_uploads_thread (thread_id)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS users (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    username      VARCHAR(32) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    is_admin      TINYINT NOT NULL DEFAULT 0,
    created_at    VARCHAR(40) NOT NULL,
    last_seen_at  VARCHAR(40),
    last_ip       VARCHAR(64)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  CHAR(64) PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    created_at  VARCHAR(40) NOT NULL,
    expires_at  VARCHAR(40) NOT NULL,
    INDEX idx_sessions_user (user_id)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS access_log (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id     BIGINT,
    username    VARCHAR(32) NOT NULL,
    event       VARCHAR(16) NOT NULL,
    ip          VARCHAR(64),
    user_agent  VARCHAR(255),
    created_at  VARCHAR(40) NOT NULL,
    INDEX idx_access_created (created_at)
) CHARACTER SET utf8mb4
"""


class _Conn:
    """Tiny wrapper so the queries below are written once with '?'
    placeholders and run unchanged on SQLite or MySQL."""

    def __init__(self, raw, mysql: bool):
        self.raw = raw
        self.mysql = mysql

    def execute(self, sql: str, params=()):
        if self.mysql:
            cur = self.raw.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self.raw.execute(sql, params)


def _connect():
    if MYSQL_HOST:
        import pymysql
        raw = pymysql.connect(
            host=MYSQL_HOST,
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "orbitops"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "orbitops"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        return _Conn(raw, mysql=True)
    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    return _Conn(raw, mysql=False)


@contextmanager
def _conn():
    conn = _connect()
    try:
        yield conn
        conn.raw.commit()
    finally:
        conn.raw.close()


def init_db(retries: int = 30, delay: float = 2.0) -> None:
    # In Kubernetes/compose the database container may still be starting
    # when the backend boots, so retry instead of crashing straight away.
    for attempt in range(retries):
        try:
            with _conn() as conn:
                schema = MYSQL_SCHEMA if conn.mysql else SQLITE_SCHEMA
                for statement in schema.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                _add_column_if_missing(conn, "threads", "user_id",
                                       "BIGINT" if conn.mysql else "INTEGER")
            return
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


def _add_column_if_missing(conn, table: str, column: str, sql_type: str) -> None:
    """Databases created before accounts existed have no threads.user_id."""
    if conn.mysql:
        rows = conn.execute(f"SHOW COLUMNS FROM {table}").fetchall()
        names = {r["Field"] for r in rows}
    else:
        names = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ping() -> None:
    """Raises if the database is unreachable (used by the health check)."""
    with _conn() as conn:
        conn.execute("SELECT 1")


def new_thread_id() -> str:
    return str(uuid.uuid4())


def ensure_thread(thread_id: str, title_hint: str = "New chat", user_id: Optional[int] = None) -> None:
    with _conn() as conn:
        row = conn.execute("SELECT thread_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO threads (thread_id, title, created_at, user_id) VALUES (?, ?, ?, ?)",
                (thread_id, title_hint[:60], _now(), user_id),
            )


def thread_owner(thread_id: str):
    """(exists, user_id) for a thread."""
    with _conn() as conn:
        row = conn.execute("SELECT user_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
        return (row is not None, row["user_id"] if row is not None else None)


def add_message(thread_id: str, role: str, content: str, source: Optional[str] = None,
                 citations: Optional[str] = None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (thread_id, role, content, source, citations, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, role, content, source, citations, datetime.now(timezone.utc).isoformat()),
        )


def get_messages(thread_id: str) -> List[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content, source, citations, created_at FROM messages "
            "WHERE thread_id = ? ORDER BY id ASC",
            (thread_id,),
        ).fetchall()
        messages = [dict(r) for r in rows]
        # citations is stored as a JSON string column -- deserialise it back
        # into a real list/None before it reaches the frontend, which expects
        # to .map() over it directly.
        for m in messages:
            raw = m.get("citations")
            if raw:
                try:
                    m["citations"] = json.loads(raw)
                except (TypeError, ValueError):
                    m["citations"] = None
            else:
                m["citations"] = None
        return messages


def list_threads(user_id: int) -> List[dict]:
    """The signed-in user's own threads, most recently active first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT thread_id, title, created_at FROM threads WHERE user_id = ?", (user_id,)
        ).fetchall()
        threads = {r["thread_id"]: dict(r) for r in rows}
        thread_ids = list(threads)

        for tid in thread_ids:
            last = conn.execute(
                "SELECT content, created_at FROM messages WHERE thread_id = ? AND role = 'user' "
                "ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if tid in threads and last:
                threads[tid]["preview"] = last["content"][:80]
                threads[tid]["last_at"] = last["created_at"]

        ordered = [threads[tid] for tid in thread_ids if tid in threads]
        ordered.sort(key=lambda t: t.get("last_at", t["created_at"]), reverse=True)
        return ordered


def set_title_from_first_message(thread_id: str, message: str) -> None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM messages WHERE thread_id = ? AND role = 'user'", (thread_id,)
        ).fetchone()
        if row["c"] <= 1:
            conn.execute("UPDATE threads SET title = ? WHERE thread_id = ?", (message[:60], thread_id))


def get_agent_history(thread_id: str, max_turns: int = 10) -> List[dict]:
    """Previous Agent turns in this thread as [{'role', 'content'}, ...]
    pairs, which is the shape agent_core's chat_history expects. Only turns
    the Agent answered are included, matching what its own checkpoint used
    to hold. Read from the database so it's the same on every replica."""
    history: List[dict] = []
    last_user: Optional[str] = None
    for m in get_messages(thread_id):
        if m["role"] == "user":
            last_user = m["content"]
        elif m["source"] == "agent" and last_user is not None:
            history += [{"role": "user", "content": last_user},
                        {"role": "assistant", "content": m["content"]}]
            last_user = None
    return history[-2 * max_turns:]


# ---------------------------------------------------------------------------
# Uploaded documents (extracted text), shared by every backend replica
# ---------------------------------------------------------------------------

def add_upload(thread_id: str, filename: str, content: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO uploads (thread_id, filename, content, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, filename[:255], content, datetime.now(timezone.utc).isoformat()),
        )


def count_uploads(thread_id: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM uploads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return int(row["c"])


def get_upload_ids(thread_id: str) -> List[int]:
    with _conn() as conn:
        rows = conn.execute("SELECT id FROM uploads WHERE thread_id = ? ORDER BY id ASC",
                            (thread_id,)).fetchall()
        return [int(r["id"]) for r in rows]


def get_upload(thread_id: str, upload_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT filename, content FROM uploads WHERE thread_id = ? AND id = ?",
                           (thread_id, upload_id)).fetchone()
        return dict(row) if row else None


def list_uploads(thread_id: str) -> List[dict]:
    """Uploaded files for the chat's file list (names only, not the text)."""
    with _conn() as conn:
        rows = conn.execute("SELECT id, filename, created_at FROM uploads WHERE thread_id = ? "
                            "ORDER BY id ASC", (thread_id,)).fetchall()
        return [dict(r) for r in rows]


def delete_upload(thread_id: str, upload_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM uploads WHERE thread_id = ? AND id = ?", (thread_id, upload_id))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Accounts, sessions and the admin's access log
# ---------------------------------------------------------------------------

def create_user(username: str, password_hash: str, is_admin: bool = False) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, 1 if is_admin else 0, _now()),
        )
        return int(cur.lastrowid)


def get_user_by_username(username: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def upsert_admin(username: str, password_hash: str) -> None:
    """Create the admin account from env, or reset its password to match."""
    with _conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                (username, password_hash, _now()),
            )
        else:
            conn.execute("UPDATE users SET password_hash = ?, is_admin = 1 WHERE id = ?",
                         (password_hash, row["id"]))


def create_session(token_hash: str, user_id: int, expires_at: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, _now(), expires_at),
        )


def get_session_user(token_hash: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT u.id, u.username, u.is_admin, u.last_seen_at, s.expires_at FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None


def delete_session(token_hash: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def touch_user(user_id: int, ip: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE users SET last_seen_at = ?, last_ip = ? WHERE id = ?",
                     (_now(), ip, user_id))


def log_access(user_id: Optional[int], username: str, event: str, ip: str, user_agent: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO access_log (user_id, username, event, ip, user_agent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, event, ip, (user_agent or "")[:255], _now()),
        )


def list_users() -> List[dict]:
    """Accounts for the admin page: identity and activity only, no chat content."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT u.id, u.username, u.is_admin, u.created_at, u.last_seen_at, u.last_ip, "
            "(SELECT COUNT(*) FROM access_log a WHERE a.user_id = u.id AND a.event IN ('signup', 'login')) AS logins "
            "FROM users u ORDER BY u.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_access_log(limit: int = 200) -> List[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT username, event, ip, user_agent, created_at, "
            "(user_id IS NOT NULL AND user_id IN (SELECT id FROM users)) AS active "
            "FROM access_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user(user_id: int) -> List[str]:
    """Delete an account with its sessions and chats. The access log keeps
    the username so the admin's history stays complete. Returns the deleted
    thread ids so their vector collections can be dropped too."""
    with _conn() as conn:
        thread_ids = [r["thread_id"] for r in conn.execute(
            "SELECT thread_id FROM threads WHERE user_id = ?", (user_id,)).fetchall()]
        for tid in thread_ids:
            conn.execute("DELETE FROM messages WHERE thread_id = ?", (tid,))
            conn.execute("DELETE FROM uploads WHERE thread_id = ?", (tid,))
            conn.execute("DELETE FROM threads WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE access_log SET user_id = NULL WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return thread_ids
