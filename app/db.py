import logging
import pathlib

import asyncpg

from . import config

log = logging.getLogger("seatres.db")
MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"

pool: asyncpg.Pool | None = None


async def connect() -> asyncpg.Pool:
    global pool
    pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
        command_timeout=30,
    )
    return pool


async def migrate() -> None:
    """Apply migrations/*.sql in order. An advisory lock makes this safe when
    several instances boot at once; every statement is idempotent (IF NOT EXISTS)."""
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(727274)")
        try:
            for f in sorted(MIGRATIONS.glob("*.sql")):
                await conn.execute(f.read_text())
                log.info("migration applied", extra={"migration": f.name})
        finally:
            await conn.execute("SELECT pg_advisory_unlock(727274)")


async def close() -> None:
    if pool is not None:
        await pool.close()


def acquire():
    assert pool is not None, "db pool not initialised"
    return pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT)
