"""
api/auth.py

JWT-based authentication and API key validation for CoreAI.
Handles token issuance, verification, and scope enforcement.

Contact: security@coreai.com
"""

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

JWT_SECRET = os.environ["SECRET_KEY"]
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINS = int(os.environ.get("JWT_EXPIRY_MINS", "60"))

bearer_scheme = HTTPBearer(auto_error=False)


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------


class TokenPayload(BaseModel):
    sub: str
    scopes: List[str]
    exp: int
    iat: int
    jti: Optional[str] = None


class AuthContext(BaseModel):
    subject: str
    scopes: List[str]
    is_api_key: bool = False

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes and "admin" not in self.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Scope required: {scope}",
            )


# ------------------------------------------------------------------
# Token issuance
# ------------------------------------------------------------------


def create_access_token(
    subject: str,
    scopes: List[str],
    expires_delta: Optional[timedelta] = None,
) -> str:
    now = datetime.now(timezone.utc)
    expires = now + (expires_delta or timedelta(minutes=JWT_EXPIRY_MINS))
    payload = {
        "sub": subject,
        "scopes": scopes,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_service_token(service_name: str) -> str:
    """Long-lived token for internal service-to-service auth (24h)."""
    return create_access_token(
        subject=f"svc:{service_name}",
        scopes=["completions:read", "completions:write", "tasks:read", "tasks:write"],
        expires_delta=timedelta(hours=24),
    )


# ------------------------------------------------------------------
# Token verification
# ------------------------------------------------------------------


def decode_token(token: str) -> TokenPayload:
    try:
        raw = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return TokenPayload(**raw)
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError as exc:
        logger.warning("JWT decode error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ------------------------------------------------------------------
# API key validation
# ------------------------------------------------------------------


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def validate_api_key(raw_key: str, key_store: Dict) -> Optional[AuthContext]:
    key_hash = _hash_api_key(raw_key)
    meta = key_store.get(key_hash)
    if not meta:
        return None
    return AuthContext(subject=meta["owner"], scopes=meta["scopes"], is_api_key=True)


# ------------------------------------------------------------------
# FastAPI dependencies
# ------------------------------------------------------------------


async def get_auth_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> AuthContext:
    """
    Accepts either:
      - Bearer JWT in Authorization header
      - Raw API key in X-API-Key header
    """
    if x_api_key:
        from database.db import get_key_store

        ctx = validate_api_key(x_api_key, get_key_store())
        if not ctx:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )
        return ctx

    if credentials:
        payload = decode_token(credentials.credentials)
        return AuthContext(subject=payload.sub, scopes=payload.scopes)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_scope(scope: str):
    """Dependency factory — enforces a specific scope."""

    async def _check(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        ctx.require_scope(scope)
        return ctx

    return _check


# ------------------------------------------------------------------
# HMAC webhook signing
# ------------------------------------------------------------------

SIGNING_SECRET = os.environ.get("WEBHOOK_SIGNING_SECRET", "")


def verify_request_signature(
    payload: bytes,
    signature: str,
    timestamp: str,
    tolerance_secs: int = 300,
) -> bool:
    try:
        ts = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - ts) > tolerance_secs:
        logger.warning("Request signature outside tolerance window")
        return False

    signed = f"{ts}.{payload.decode()}".encode()
    expected = hmac.new(SIGNING_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
