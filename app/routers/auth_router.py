from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import AfterValidator, BaseModel

from app.auth import (
    create_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import settings
from app.database import (
    consume_email_token,
    create_email_token,
    create_user,
    delete_user,
    get_query_count,
    get_user_by_email,
    get_user_by_id,
    recent_token_count,
    set_email_verified,
    set_password,
)
from app.email_service import (
    notify_admin_new_user,
    send_account_deleted,
    send_already_registered,
    send_delete_confirmation,
    send_password_changed,
    send_password_reset,
    send_verification,
)

router = APIRouter(prefix="/api/auth")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# How long each emailed link works
_TOKEN_TTL = {
    "verify": timedelta(hours=48),
    "reset": timedelta(hours=1),
    "delete": timedelta(hours=1),
}
# At most this many emails of one kind per address per hour. Responses don't
# change when the limit is hit, so it can't be used to probe for accounts.
_EMAILS_PER_HOUR = 3

_CHECK_EMAIL = (
    "Check your email: we've sent a link to confirm your address. "
    "If it doesn't arrive within a few minutes, look in your spam folder."
)
_INVALID_LINK = "This link is invalid or has expired. Please request a new one."


def _clean_email(v: str) -> str:
    if not _EMAIL_RE.match(v.strip()):
        raise ValueError("Invalid email address")
    return v.strip().lower()


def _check_password(v: str) -> str:
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters")
    return v


Email = Annotated[str, AfterValidator(_clean_email)]
NewPassword = Annotated[str, AfterValidator(_check_password)]


class RegisterRequest(BaseModel):
    email: Email
    password: NewPassword


class LoginRequest(BaseModel):
    email: str
    password: str


class EmailRequest(BaseModel):
    email: Email


class TokenRequest(BaseModel):
    token: str


class ResetRequest(BaseModel):
    token: str
    password: NewPassword


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: NewPassword


class PasswordRequest(BaseModel):
    password: str


def _issue(user: dict, purpose: str) -> str | None:
    """A new emailed-link token, or None if this address has had too many lately."""
    if recent_token_count(settings.db_path, user["id"], purpose, 60) >= _EMAILS_PER_HOUR:
        return None
    return create_email_token(settings.db_path, user["id"], purpose, _TOKEN_TTL[purpose])


def _blocked_on_this_site(user: dict | None) -> bool:
    # The beta site (ADMIN_ONLY=1) sends no account emails to non-admins
    return settings.admin_only and not (user and user["is_admin"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, background: BackgroundTasks) -> dict:
    if settings.admin_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is closed on this site.")
    # Hash first, so the response takes as long whether or not the address is known
    password_hash = hash_password(body.password)
    user = get_user_by_email(settings.db_path, body.email)
    if user is None:
        create_user(settings.db_path, body.email, password_hash)
        user = get_user_by_email(settings.db_path, body.email)
    # The same reply in every case, so registering can't reveal who has an account.
    # An existing account's password is never changed here.
    if not user["email_verified"]:
        if token := _issue(user, "verify"):
            background.add_task(send_verification, user["email"], token)
    elif token := _issue(user, "reset"):
        background.add_task(send_already_registered, user["email"], token)
    return {"status": "check_email", "message": _CHECK_EMAIL}


@router.post("/verify")
def verify_email(body: TokenRequest, background: BackgroundTasks) -> dict:
    user_id = consume_email_token(settings.db_path, body.token, "verify")
    user = get_user_by_id(settings.db_path, user_id) if user_id else None
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _INVALID_LINK)
    # The admin hears about a sign-up only once the address is shown to be real
    if set_email_verified(settings.db_path, user["id"]) and not user["is_approved"]:
        background.add_task(notify_admin_new_user, user["email"])
    if user["is_approved"]:
        return {"message": "Email confirmed. You can now sign in."}
    return {"message": (
        "Email confirmed. An admin will review your account, "
        "and we'll email you when it's approved."
    )}


@router.post("/resend-verification")
def resend_verification(body: EmailRequest, background: BackgroundTasks) -> dict:
    user = get_user_by_email(settings.db_path, body.email)
    if user and not user["email_verified"] and not _blocked_on_this_site(user):
        if token := _issue(user, "verify"):
            background.add_task(send_verification, user["email"], token)
    return {"message": _CHECK_EMAIL}


@router.post("/login")
def login(body: LoginRequest) -> dict:
    user = get_user_by_email(settings.db_path, body.email)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if settings.admin_only and not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This site is open to admins only.")
    if not user["email_verified"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, {
            "code": "email_unverified",
            "message": "Please confirm your email address first, using the link we emailed you.",
        })
    if not user["is_approved"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account is awaiting approval. We'll email you when it's approved.",
        )
    return {"token": create_token(user)}


@router.post("/forgot-password")
def forgot_password(body: EmailRequest, background: BackgroundTasks) -> dict:
    user = get_user_by_email(settings.db_path, body.email)
    if user and not _blocked_on_this_site(user):
        if token := _issue(user, "reset"):
            background.add_task(send_password_reset, user["email"], token)
    return {"message": (
        "If there's an account for that address, we've emailed it a link to set a new password. "
        "The link works for 1 hour."
    )}


@router.post("/reset-password")
def reset_password(body: ResetRequest, background: BackgroundTasks) -> dict:
    user_id = consume_email_token(settings.db_path, body.token, "reset")
    user = get_user_by_id(settings.db_path, user_id) if user_id else None
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _INVALID_LINK)
    set_password(settings.db_path, user["id"], hash_password(body.password))
    # Following the link proves the address too
    if set_email_verified(settings.db_path, user["id"]) and not user["is_approved"]:
        background.add_task(notify_admin_new_user, user["email"])
    background.add_task(send_password_changed, user["email"])
    return {"message": "Your password has been changed. You can now sign in."}


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    background: BackgroundTasks,
) -> dict:
    if not verify_password(body.current_password, current_user["password_hash"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your current password is incorrect")
    version = set_password(settings.db_path, current_user["id"], hash_password(body.new_password))
    background.add_task(send_password_changed, current_user["email"])
    # Other devices' tokens now fail; this one gets a fresh token so it stays signed in
    return {
        "message": "Password changed. Other devices have been signed out.",
        "token": create_token({**current_user, "token_version": version}),
    }


@router.post("/delete-account")
def request_account_deletion(
    body: PasswordRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    background: BackgroundTasks,
) -> dict:
    if current_user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin accounts can't be deleted from here.")
    if not verify_password(body.password, current_user["password_hash"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Incorrect password")
    if token := _issue(current_user, "delete"):
        background.add_task(send_delete_confirmation, current_user["email"], token)
    return {"message": (
        "We've emailed you a link to confirm. Your account is deleted only when you open it. "
        "The link works for 1 hour."
    )}


@router.post("/delete-account/confirm")
def confirm_account_deletion(body: TokenRequest, background: BackgroundTasks) -> dict:
    user_id = consume_email_token(settings.db_path, body.token, "delete")
    user = get_user_by_id(settings.db_path, user_id) if user_id else None
    if not user or user["is_admin"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _INVALID_LINK)
    delete_user(settings.db_path, user["id"])
    background.add_task(send_account_deleted, user["email"])
    return {"message": "Your account has been deleted."}


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
