"""
SQLite database for user accounts and daily query tracking.
Uses the stdlib sqlite3 — no ORM needed.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Daily query counts live in this table. Beta sets QUERY_LOG_TABLE=query_log_beta so
# testing on beta.yesdeputy.com doesn't use up the main site's quota; both share users.
_LOG_TABLE = "query_log"
_LOG_TABLE_RE = re.compile(r"^query_log(_[a-z0-9]+)?$")


def init_db(db_path: str, query_log_table: str = "query_log") -> None:
    """Create tables and run any pending migrations. Safe to call on every startup."""
    global _LOG_TABLE
    if not _LOG_TABLE_RE.match(query_log_table):
        raise ValueError(f"Invalid query log table name: {query_log_table!r}")
    _LOG_TABLE = query_log_table
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT    NOT NULL,
                created_at    TEXT    NOT NULL DEFAULT (date('now')),
                is_admin      INTEGER NOT NULL DEFAULT 0,
                is_approved   INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {_LOG_TABLE} (
                user_id  INTEGER NOT NULL REFERENCES users(id),
                log_date TEXT    NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, log_date)
            )
        """)
        # Migration: add is_approved column to existing databases
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "is_approved" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_approved INTEGER NOT NULL DEFAULT 0")
            # Existing users (created before approval was added) are pre-approved
            conn.execute("UPDATE users SET is_approved = 1")
        # Migration: email verification. Users who registered before it existed count as verified.
        if "email_verified" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE users SET email_verified = 1")
        # Migration: bumped on every password change, so older JWTs stop working
        if "token_version" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
        # Single-use links sent by email. Only a hash of each token is stored.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                purpose    TEXT    NOT NULL,
                token_hash TEXT    NOT NULL UNIQUE,
                created_at TEXT    NOT NULL,
                expires_at TEXT    NOT NULL,
                used_at    TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS email_tokens_user ON email_tokens (user_id, purpose)")
        # Migration: separate daily count for searches made with the user's own API key
        log_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_LOG_TABLE})")}
        if "byok_count" not in log_cols:
            conn.execute(f"ALTER TABLE {_LOG_TABLE} ADD COLUMN byok_count INTEGER NOT NULL DEFAULT 0")


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── User operations ────────────────────────────────────────────────────────

def create_user(db_path: str, email: str, password_hash: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, is_approved) VALUES (?, ?, 0)",
            (email.lower().strip(), password_hash),
        )
        return cur.lastrowid


def get_user_by_email(db_path: str, email: str) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(db_path: str, user_id: int) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def approve_user(db_path: str, email: str) -> bool:
    """Approve a user by email. Returns True only if they were pending until now."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE users SET is_approved = 1 WHERE email = ? AND is_approved = 0",
            (email.lower().strip(),),
        )
        return cur.rowcount > 0


def approve_user_by_id(db_path: str, user_id: int) -> bool:
    """Approve a user by ID. Returns True only if they were pending until now."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE users SET is_approved = 1 WHERE id = ? AND is_approved = 0", (user_id,)
        )
        return cur.rowcount > 0


def set_email_verified(db_path: str, user_id: int) -> bool:
    """Mark a user's address as confirmed. Returns True if it wasn't already."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE users SET email_verified = 1 WHERE id = ? AND email_verified = 0", (user_id,)
        )
        return cur.rowcount > 0


def set_password(db_path: str, user_id: int, password_hash: str) -> int:
    """Store a new password hash and invalidate existing sessions. Returns the new token_version."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, token_version = token_version + 1 WHERE id = ?",
            (password_hash, user_id),
        )
        return conn.execute(
            "SELECT token_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()[0]


def delete_user(db_path: str, user_id: int) -> bool:
    """Delete a user and their query log. Returns True if found and deleted."""
    with _connect(db_path) as conn:
        # Every site's counts (main and beta) share the users table
        log_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'query_log%'")]
        for table in log_tables:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM email_tokens WHERE user_id = ?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount > 0


def list_users(db_path: str) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, email, created_at, is_admin, is_approved, email_verified "
            "FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Email tokens (verify, reset, delete links) ─────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_email_token(db_path: str, user_id: int, purpose: str, ttl: timedelta) -> str:
    """
    Issue a single-use token and return it. Only its hash is stored, so the raw
    token exists only in the email. Older unused tokens for the same purpose stop working.
    """
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE email_tokens SET used_at = ? "
            "WHERE user_id = ? AND purpose = ? AND used_at IS NULL",
            (now.isoformat(), user_id, purpose),
        )
        conn.execute(
            "INSERT INTO email_tokens (user_id, purpose, token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, purpose, _hash_token(token), now.isoformat(), (now + ttl).isoformat()),
        )
    return token


def consume_email_token(db_path: str, token: str, purpose: str) -> int | None:
    """Use up a token. Returns its user_id, or None if it's unknown, used or expired."""
    now = _utcnow().isoformat()
    with _connect(db_path) as conn:
        # One conditional UPDATE, so two clicks on the same link can't both succeed
        row = conn.execute(
            "UPDATE email_tokens SET used_at = ? "
            "WHERE token_hash = ? AND purpose = ? AND used_at IS NULL AND expires_at > ? "
            "RETURNING user_id",
            (now, _hash_token(token), purpose, now),
        ).fetchone()
        return row[0] if row else None


def recent_token_count(db_path: str, user_id: int, purpose: str, minutes: int) -> int:
    """How many tokens of this purpose were issued to the user recently (for rate limiting)."""
    since = (_utcnow() - timedelta(minutes=minutes)).isoformat()
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT count(*) FROM email_tokens WHERE user_id = ? AND purpose = ? AND created_at > ?",
            (user_id, purpose, since),
        ).fetchone()[0]


# ── Query rate limiting ────────────────────────────────────────────────────

def get_query_count(db_path: str, user_id: int) -> int:
    today = date.today().isoformat()
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT count FROM {_LOG_TABLE} WHERE user_id = ? AND log_date = ?",
            (user_id, today),
        ).fetchone()
        return row["count"] if row else 0


# Which query_log column a quota uses: free queries on the server's keys, or
# searches made with the user's own key (whose retrieval still uses ours)
_QUOTA_COLUMNS = {"free": "count", "byok": "byok_count"}


def reserve_query(
    db_path: str, user_id: int, log_date: str, limit: int, *, quota: str = "free",
) -> int | None:
    """
    Count one query before any work is done. Returns the new count, or None if
    the user is already at `limit`.

    The limit check and the increment are one UPDATE, so concurrent requests
    (including from the other uvicorn worker) can't all pass the check.
    """
    col = _QUOTA_COLUMNS[quota]
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO {_LOG_TABLE} (user_id, log_date) VALUES (?, ?) "
            "ON CONFLICT (user_id, log_date) DO NOTHING",
            (user_id, log_date),
        )
        cur = conn.execute(
            f"UPDATE {_LOG_TABLE} SET {col} = {col} + 1 "
            f"WHERE user_id = ? AND log_date = ? AND {col} < ?",
            (user_id, log_date, limit),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            f"SELECT {col} FROM {_LOG_TABLE} WHERE user_id = ? AND log_date = ?",
            (user_id, log_date),
        ).fetchone()
        return row[0]


def refund_query(db_path: str, user_id: int, log_date: str, *, quota: str = "free") -> None:
    """Give back a reserved query that failed before producing an answer."""
    col = _QUOTA_COLUMNS[quota]
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {_LOG_TABLE} SET {col} = {col} - 1 "
            f"WHERE user_id = ? AND log_date = ? AND {col} > 0",
            (user_id, log_date),
        )
