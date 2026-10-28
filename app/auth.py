"""
Password hashing (bcrypt) and JWT token helpers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from typing import Annotated

from app.config import settings
from app.database import get_user_by_id

_JWT_ALGORITHM = "HS256"
_TOKEN_EXPIRE_DAYS = 30

_bearer = HTTPBearer(auto_error=False)


# ── Password ───────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT ────────────────────────────────────────────────────────────────────

def create_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(days=_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALGORITHM)


def decode_token(token: str) -> int:
    """Return user_id or raise HTTPException."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALGORITHM])
        return int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


# ── FastAPI dependency ─────────────────────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Dependency: resolve JWT → user dict. Raises 401 if missing/invalid."""
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    user_id = decode_token(credentials.credentials)
    user = get_user_by_id(settings.db_path, user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    # Both sites share JWT_SECRET, so a main-site token is valid here too
    if settings.admin_only and not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This site is open to admins only")
    return user


def require_admin(
    current_user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Dependency: like get_current_user but raises 403 if not an admin."""
    if not current_user.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return current_user


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict | None:
    """Like get_current_user but returns None instead of raising for public routes."""
    if not credentials:
        return None
    try:
        user_id = decode_token(credentials.credentials)
        return get_user_by_id(settings.db_path, user_id)
    except HTTPException:
        return None
