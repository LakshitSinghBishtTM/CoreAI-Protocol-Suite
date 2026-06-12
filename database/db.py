"""
CoreAI Protocol Suite - Database
Async SQLAlchemy engine and session management.
Reads config from environment variables or database.yml.
"""

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool, AsyncAdaptedQueuePool
from sqlalchemy import text


# ------------------------------------------------------------------ #
# Connection URL construction
# ------------------------------------------------------------------ #

def _build_url() -> str:
    """
    Build async DB URL from environment.
    Falls back to SQLite for local dev if no DATABASE_URL set.
    """
    url = os.getenv("DATABASE_URL")
    if url:
        # SQLAlchemy async drivers need +asyncpg for postgres
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    # Individual env vars (matches database.yml layout)
    engine   = os.getenv("DB_ENGINE", "sqlite")
    host     = os.getenv("DB_HOST", "localhost")
    port     = os.getenv("DB_PORT", "5432")
    user     = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres")
    name     = os.getenv("DB_NAME", "coreai_dev")

    if engine == "sqlite":
        path = os.getenv("DB_PATH", "/tmp/coreai_dev.db")
        return f"sqlite+aiosqlite:///{path}"

    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


# ------------------------------------------------------------------ #
# Engine singleton
# ------------------------------------------------------------------ #

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker] = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


def get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


def init_db(
    url: Optional[str] = None,
    pool_size: int = 10,
    max_overflow: int = 5,
    echo: bool = False,
) -> AsyncEngine:
    """
    Initialize the async engine and session factory.
    Call once at application startup.
    """
    global _engine, _session_factory

    db_url = url or _build_url()
    is_sqlite = db_url.startswith("sqlite")

    engine_kwargs = dict(echo=echo)
    if is_sqlite:
        # SQLite doesn't support connection pooling
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs["pool_size"] = pool_size
        engine_kwargs["max_overflow"] = max_overflow
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_recycle"] = 900  # matches idle_timeout in database.yml

    _engine = create_async_engine(db_url, **engine_kwargs)
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    logger.info(f"Database engine initialized: {db_url.split('@')[-1]}")  # no creds in log
    return _engine


async def close_db():
    """Dispose engine connections. Call on shutdown."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database connections closed")


# ------------------------------------------------------------------ #
# Session context manager
# ------------------------------------------------------------------ #

@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for DB sessions.

        async with get_session() as session:
            result = await session.execute(select(RequestLog))
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ------------------------------------------------------------------ #
# Health check
# ------------------------------------------------------------------ #

async def ping() -> bool:
    """Verify database connectivity. Returns True if reachable."""
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database ping failed: {e}")
        return False
