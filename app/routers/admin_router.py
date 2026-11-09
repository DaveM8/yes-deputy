"""
Admin API — list, approve, and delete users.
All routes require is_admin=1.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.auth import require_admin
from app.config import settings
from app.database import approve_user_by_id, delete_user, get_user_by_id, list_users
from app.email_service import send_account_approved

router = APIRouter(prefix="/api/admin")


@router.get("/users")
def admin_list_users(
    _admin: Annotated[dict, Depends(require_admin)],
) -> list[dict]:
    """Return all users, pending first."""
    users = list_users(settings.db_path)
    # Sort: pending (not approved) first, then by registration date desc
    return sorted(users, key=lambda u: (u["is_approved"], u["created_at"]))


@router.post("/users/{user_id}/approve", status_code=status.HTTP_200_OK)
def admin_approve_user(
    user_id: int,
    _admin: Annotated[dict, Depends(require_admin)],
    background: BackgroundTasks,
) -> dict:
    """Approve a pending user by ID, and email them that they can sign in."""
    user = get_user_by_id(settings.db_path, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    # Only the first approval sends the email, so a double click doesn't send two
    emailed = approve_user_by_id(settings.db_path, user_id)
    if emailed:
        background.add_task(send_account_approved, user["email"])
    return {"status": "approved", "user_id": user_id, "emailed": emailed}


@router.delete("/users/{user_id}", status_code=status.HTTP_200_OK)
def admin_delete_user(
    user_id: int,
    admin: Annotated[dict, Depends(require_admin)],
) -> dict:
    """Delete a user (reject or remove). Admins cannot delete themselves."""
    if admin["id"] == user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account")
    if not delete_user(settings.db_path, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return {"status": "deleted", "user_id": user_id}
