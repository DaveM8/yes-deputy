#!/usr/bin/env python3
"""
Admin management CLI for Yes Deputy.

Usage:
    .venv/bin/python manage.py users          # list all users
    .venv/bin/python manage.py approve <email>
    .venv/bin/python manage.py create-admin <email> <password>
"""
from __future__ import annotations

import sys

# Load .env before importing settings
from app.config import settings
from app.database import approve_user, create_user, get_user_by_email, init_db, list_users
from app.auth import hash_password


def cmd_users() -> None:
    users = list_users(settings.db_path)
    if not users:
        print("No users found.")
        return
    print(f"{'ID':<5} {'Email':<35} {'Created':<12} {'Admin':<6} {'Approved'}")
    print("-" * 70)
    for u in users:
        admin  = "yes" if u["is_admin"]    else "no"
        approved = "yes" if u["is_approved"] else "PENDING"
        print(f"{u['id']:<5} {u['email']:<35} {u['created_at']:<12} {admin:<6} {approved}")


def cmd_approve(email: str) -> None:
    if approve_user(settings.db_path, email):
        print(f"Approved: {email}")
    else:
        print(f"User not found: {email}")
        sys.exit(1)


def cmd_create_admin(email: str, password: str) -> None:
    existing = get_user_by_email(settings.db_path, email)
    if existing:
        print(f"User already exists: {email}")
        sys.exit(1)
    import sqlite3
    from pathlib import Path
    from contextlib import contextmanager

    # Use create_user then promote to admin + approved directly
    user_id = create_user(settings.db_path, email, hash_password(password))

    import sqlite3 as _sq
    conn = _sq.connect(settings.db_path)
    conn.execute(
        "UPDATE users SET is_admin = 1, is_approved = 1 WHERE id = ?", (user_id,)
    )
    conn.commit()
    conn.close()
    print(f"Admin user created and approved: {email}")


def main() -> None:
    init_db(settings.db_path)
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd == "users":
        cmd_users()
    elif cmd == "approve" and len(args) == 2:
        cmd_approve(args[1])
    elif cmd == "create-admin" and len(args) == 3:
        cmd_create_admin(args[1], args[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
