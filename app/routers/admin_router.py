"""
Admin API — list, approve, and delete users.
All routes require is_admin=1.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import require_admin
from app.config import settings
from app.database import approve_user_by_id, delete_user, list_users

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
) -> dict:
    """Approve a pending user by ID."""
    if not approve_user_by_id(settings.db_path, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return {"status": "approved", "user_id": user_id}


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
