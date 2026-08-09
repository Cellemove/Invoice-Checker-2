"""Supabase JWT verification, used as a FastAPI dependency.

Auth is currently DISCONNECTED for internal use: endpoints depend on
`get_optional_user`, which returns a shared internal user when no valid token is
present. The strict verifier `get_current_user` is kept intact and is used
automatically whenever `AUTH_REQUIRED=true` is set in the environment — so this
module stays a drop-in way to re-enable real authentication later without
touching the routers.

Tokens (when supplied) are verified offline using the project's JWT secret
(HS256). The decoded `sub` claim is the Supabase user id.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .config import get_settings

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str | None
    token: str


def _decode_token(token: str) -> AuthUser:
    """Decode and validate a Supabase JWT, or raise JWTError/HTTPException."""
    settings = get_settings()
    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server auth is not configured (SUPABASE_JWT_SECRET missing).",
        )
    payload = jwt.decode(
        token,
        settings.supabase_jwt_secret,
        algorithms=["HS256"],
        audience="authenticated",
        options={"verify_aud": True},
    )
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token does not contain a subject (sub) claim.",
        )
    return AuthUser(id=user_id, email=payload.get("email"), token=token)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser:
    """Strict dependency: require and validate a bearer token (401 otherwise)."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return _decode_token(credentials.credentials)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser:
    """Relaxed dependency used while auth is disconnected (internal use).

    * If `AUTH_REQUIRED=true`, behaves exactly like `get_current_user`.
    * Otherwise: honours a valid token when one is supplied (so the dormant
      login flow still works), but falls back to a shared internal user when no
      usable token is present — never blocking the request.
    """
    settings = get_settings()
    if settings.auth_required:
        return get_current_user(credentials)

    if credentials and credentials.credentials and settings.supabase_jwt_secret:
        try:
            return _decode_token(credentials.credentials)
        except (JWTError, HTTPException):
            # Ignore bad/expired tokens when auth is optional.
            pass

    return AuthUser(id=settings.default_user_id, email=None, token="")
