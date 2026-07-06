"""
CoreAI Protocol Suite - Auth Middleware
API key authentication for incoming requests.
Keys are validated against SHA-256 hashes stored in the database.
"""

import hashlib
import os
from typing import Optional

from fastapi import Request, HTTPException, Security
from fastapi.security import APIKeyHeader
from loguru import logger

from database.db import get_session
from database.models import APIKey

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

_MASTER_KEY_HASH: Optional[str] = None


def _get_master_key_hash() -> Optional[str]:
    global _MASTER_KEY_HASH
    if _MASTER_KEY_HASH is None:
        master = os.getenv("COREAI_MASTER_KEY")
        if master:
            _MASTER_KEY_HASH = _hash_key(master)
    return _MASTER_KEY_HASH


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _lookup_db_key(key_hash: str) -> Optional[APIKey]:
    """Check database for a matching active API key."""
    try:
        from sqlalchemy import select

        async with get_session() as session:
            result = await session.execute(
                select(APIKey).where(
                    APIKey.key_hash == key_hash,
                    APIKey.active == True,  # noqa: E712
                )
            )
            return result.scalar_one_or_none()
    except Exception as e:
        logger.error(f"Auth DB lookup failed: {e}")
        return None


async def require_api_key(
    request: Request,
    api_key: Optional[str] = Security(API_KEY_HEADER),
) -> str:
    """
    FastAPI dependency. Validates X-API-Key header.
    Raises 401 if missing, 403 if invalid.
    """
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")

    key_hash = _hash_key(api_key)

    master_hash = _get_master_key_hash()
    if master_hash and key_hash == master_hash:
        logger.debug("Request authenticated via master key")
        request.state.api_key_id = "master"
        return api_key

    record = await _lookup_db_key(key_hash)
    if not record:
        logger.warning(f"Invalid API key attempt (hash prefix: {key_hash[:8]}...)")
        raise HTTPException(status_code=403, detail="Invalid or expired API key")

    request.state.api_key_id = record.id
    logger.debug(f"Request authenticated (key_id={record.id}, owner={record.owner})")
    return api_key


async def optional_api_key(
    request: Request,
    api_key: Optional[str] = Security(API_KEY_HEADER),
) -> Optional[str]:
    """Like require_api_key but doesn't raise — for semi-public endpoints."""
    if not api_key:
        request.state.api_key_id = None
        return None
    try:
        return await require_api_key(request, api_key)
    except HTTPException:
        request.state.api_key_id = None
        return None
