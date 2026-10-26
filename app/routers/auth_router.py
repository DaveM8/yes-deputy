from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from app.auth import (
    create_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import settings
from app.database import create_user, get_query_count, get_user_by_email
from app.email_service import notify_admin_new_user

router = APIRouter(prefix="/api/auth")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v.strip()):
            raise ValueError("Invalid email address")
        return v.strip().lower()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest) -> dict:
    if get_user_by_email(settings.db_path, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    create_user(settings.db_path, body.email, hash_password(body.password))
    # Fire-and-forget — never blocks or raises to the caller
    await notify_admin_new_user(body.email)
    return {
        "status": "pending",
        "message": (
            "Your account has been created and is awaiting approval. "
            "You will be able to log in once an admin has approved your account."
        ),
    }


@router.post("/login")
def login(body: LoginRequest) -> dict:
    user = get_user_by_email(settings.db_path, body.email)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user["is_approved"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account is awaiting approval. Please check back soon.",
        )
    return {"token": create_token(user["id"])}


@router.get("/me")
def me(current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    used = get_query_count(settings.db_path, current_user["id"])
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "is_admin": bool(current_user["is_admin"]),
        "queries_used_today": used,
        "free_limit": settings.free_queries_per_day,
        "queries_remaining": max(0, settings.free_queries_per_day - used),
    }
