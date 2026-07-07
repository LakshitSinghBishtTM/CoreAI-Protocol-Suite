"""
CoreAI Protocol Suite - Migrations
Lightweight migration runner. Applies SQL files from migrations/ in order.
Tracks applied migrations in a _schema_migrations table.

Usage:
    python -m database.migrations up        # apply all pending
    python -m database.migrations status    # show applied/pending
    python -m database.migrations down 1    # roll back last N (if down scripts exist)
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import text

from .db import get_session, init_db, ping

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS _schema_migrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL UNIQUE,
    applied_at  TEXT NOT NULL
);
"""

# PostgreSQL version (autoincrement syntax differs)
CREATE_MIGRATIONS_TABLE_PG = """
CREATE TABLE IF NOT EXISTS _schema_migrations (
    id          SERIAL PRIMARY KEY,
    filename    TEXT NOT NULL UNIQUE,
    applied_at  TIMESTAMP NOT NULL
);
"""


async def _ensure_migrations_table(session):
    """Create tracking table if it doesn't exist."""
    url = str(session.bind.url) if hasattr(session, "bind") else ""
    ddl = (
        CREATE_MIGRATIONS_TABLE_PG
        if "postgresql" in str(session.get_bind())
        else CREATE_MIGRATIONS_TABLE
    )
    try:
        await session.execute(text(ddl))
    except Exception:
        await session.execute(text(CREATE_MIGRATIONS_TABLE))


async def _get_applied(session) -> set[str]:
    """Return set of already-applied migration filenames."""
    try:
        result = await session.execute(
            text("SELECT filename FROM _schema_migrations ORDER BY id")
        )
        return {row[0] for row in result.fetchall()}
    except Exception:
        return set()


def _discover_migrations(direction: str = "up") -> list[Path]:
    """
    Discover migration files.
    Up migrations:   NNN_name.sql
    Down migrations: NNN_name.down.sql
    """
    if not MIGRATIONS_DIR.exists():
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return []

    pattern = "*.down.sql" if direction == "down" else "*.sql"
    files = sorted(
        f
        for f in MIGRATIONS_DIR.glob(pattern)
        if direction == "down" or not f.name.endswith(".down.sql")
    )
    return files


async def migrate_up(target: str = None):
    """Apply all pending up migrations, or up to target filename."""
    async with get_session() as session:
        await _ensure_migrations_table(session)
        applied = await _get_applied(session)

        pending = [f for f in _discover_migrations("up") if f.name not in applied]

        if target:
            pending = [f for f in pending if f.name <= target]

        if not pending:
            logger.info("No pending migrations.")
            return

        logger.info(f"Applying {len(pending)} migration(s)...")

        for migration_file in pending:
            sql = migration_file.read_text()
            logger.info(f"  → {migration_file.name}")

            try:
                # Execute each statement separately
                statements = [s.strip() for s in sql.split(";") if s.strip()]
                for stmt in statements:
                    await session.execute(text(stmt))

                await session.execute(
                    text(
                        "INSERT INTO _schema_migrations (filename, applied_at) "
                        "VALUES (:filename, :applied_at)"
                    ),
                    {
                        "filename": migration_file.name,
                        "applied_at": datetime.utcnow().isoformat(),
                    },
                )
                logger.info(f"  ✓ {migration_file.name}")

            except Exception as e:
                logger.error(f"  ✗ {migration_file.name}: {e}")
                raise RuntimeError(
                    f"Migration failed: {migration_file.name}\n{e}"
                ) from e

        logger.info("All migrations applied.")


async def migrate_down(steps: int = 1):
    """Roll back last N applied migrations (requires .down.sql files)."""
    async with get_session() as session:
        await _ensure_migrations_table(session)

        result = await session.execute(
            text("SELECT filename FROM _schema_migrations ORDER BY id DESC LIMIT :n"),
            {"n": steps},
        )
        to_revert = [row[0] for row in result.fetchall()]

        if not to_revert:
            logger.info("Nothing to roll back.")
            return

        down_files = {f.name: f for f in _discover_migrations("down")}

        for filename in to_revert:
            down_name = filename.replace(".sql", ".down.sql")
            if down_name not in down_files:
                logger.warning(f"  No down migration for {filename}, skipping")
                continue

            sql = down_files[down_name].read_text()
            logger.info(f"  ← {down_name}")

            statements = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                await session.execute(text(stmt))

            await session.execute(
                text("DELETE FROM _schema_migrations WHERE filename = :filename"),
                {"filename": filename},
            )
            logger.info(f"  ✓ Reverted {filename}")


async def status():
    """Print applied and pending migration status."""
    async with get_session() as session:
        await _ensure_migrations_table(session)
        applied = await _get_applied(session)

    all_files = _discover_migrations("up")

    print(f"\n{'File':<40} {'Status'}")
    print("-" * 55)
    for f in all_files:
        state = "applied" if f.name in applied else "pending"
        marker = "✓" if state == "applied" else "·"
        print(f"  {marker} {f.name:<38} {state}")

    pending_count = sum(1 for f in all_files if f.name not in applied)
    print(f"\n{len(applied)} applied, {pending_count} pending\n")


async def create_all_tables():
    """
    Dev shortcut: create all ORM-defined tables directly via SQLAlchemy.
    Skips SQL migration files — use for fresh local dev setup only.
    """
    from .db import get_engine
    from .models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("All tables created via ORM metadata.")


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #


async def _main():
    init_db()

    if not await ping():
        logger.error("Cannot reach database. Check connection settings.")
        sys.exit(1)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "up"

    if cmd == "up":
        target = sys.argv[2] if len(sys.argv) > 2 else None
        await migrate_up(target)
    elif cmd == "down":
        steps = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        await migrate_down(steps)
    elif cmd == "status":
        await status()
    elif cmd == "init":
        await create_all_tables()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python -m database.migrations [up|down|status|init]")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
