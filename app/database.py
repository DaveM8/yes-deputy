"""
SQLite database for user accounts and daily query tracking.
Uses the stdlib sqlite3 — no ORM needed.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path


def init_db(db_path: str) -> None:
    """Create tables and run any pending migrations. Safe to call on every startup."""
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

            CREATE TABLE IF NOT EXISTS query_log (
                user_id  INTEGER NOT NULL REFERENCES users(id),
                log_date TEXT    NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, log_date)
            );
        """)
        # Migration: add is_approved column to existing databases
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "is_approved" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_approved INTEGER NOT NULL DEFAULT 0")
            # Existing users (created before approval was added) are pre-approved
            conn.execute("UPDATE users SET is_approved = 1")
        # Migration: separate daily count for searches made with the user's own API key
        log_cols = {row[1] for row in conn.execute("PRAGMA table_info(query_log)")}
        if "byok_count" not in log_cols:
            conn.execute("ALTER TABLE query_log ADD COLUMN byok_count INTEGER NOT NULL DEFAULT 0")


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
    """Approve a user by email. Returns True if found and updated."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE users SET is_approved = 1 WHERE email = ?",
            (email.lower().strip(),),
        )
        return cur.rowcount > 0


def approve_user_by_id(db_path: str, user_id: int) -> bool:
    """Approve a user by ID. Returns True if found and updated."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE users SET is_approved = 1 WHERE id = ?", (user_id,)
        )
        return cur.rowcount > 0


def delete_user(db_path: str, user_id: int) -> bool:
    """Delete a user and their query log. Returns True if found and deleted."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM query_log WHERE user_id = ?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount > 0


def list_users(db_path: str) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, email, created_at, is_admin, is_approved FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Query rate limiting ────────────────────────────────────────────────────

def get_query_count(db_path: str, user_id: int) -> int:
    today = date.today().isoformat()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT count FROM query_log WHERE user_id = ? AND log_date = ?",
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
            "INSERT INTO query_log (user_id, log_date) VALUES (?, ?) "
            "ON CONFLICT (user_id, log_date) DO NOTHING",
            (user_id, log_date),
        )
        cur = conn.execute(
            f"UPDATE query_log SET {col} = {col} + 1 "
            f"WHERE user_id = ? AND log_date = ? AND {col} < ?",
            (user_id, log_date, limit),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            f"SELECT {col} FROM query_log WHERE user_id = ? AND log_date = ?",
            (user_id, log_date),
        ).fetchone()
        return row[0]


def refund_query(db_path: str, user_id: int, log_date: str, *, quota: str = "free") -> None:
    """Give back a reserved query that failed before producing an answer."""
    col = _QUOTA_COLUMNS[quota]
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE query_log SET {col} = {col} - 1 "
            f"WHERE user_id = ? AND log_date = ? AND {col} > 0",
            (user_id, log_date),
        )
