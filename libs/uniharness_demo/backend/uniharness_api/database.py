"""SQLite database layer for UniHarness demo backend.

Provides:
- ``get_db()`` — factory for ``aiosqlite.Connection``
- Schema creation with ``CREATE TABLE IF NOT EXISTS``
- Schema migration via ``PRAGMA user_version`` (no Alembic)
- Write serialisation via a module-level ``asyncio.Lock``
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

from uniharness_api.paths import db_path

logger = logging.getLogger(__name__)

# -- Schema version ----------------------------------------------------------
# Increment this when the schema changes.  Migration functions below map
# old_version -> upgrade function.
CURRENT_SCHEMA_VERSION = 1

# -- Write lock --------------------------------------------------------------
_write_lock = asyncio.Lock()


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """Yield an aiosqlite connection with WAL mode and foreign keys enabled.

    Usage::

        async with get_db() as db:
            await db.execute("SELECT ...")
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Create tables and run any pending migrations.

    Safe to call at every startup — uses ``CREATE TABLE IF NOT EXISTS``
    and checks ``PRAGMA user_version`` before migrating.
    """
    async with _write_lock:
        async with get_db() as db:
            version = await _get_user_version(db)
            if version == 0:
                await _create_tables_v1(db)
                await _set_user_version(db, 1)
                logger.info("Database initialised at schema v1: %s", db_path())
            elif version < CURRENT_SCHEMA_VERSION:
                await _migrate(db, version)
            else:
                logger.debug("Database schema up-to-date (v%d): %s", version, db_path())


async def acquire_write_lock() -> None:
    """Acquire the module-level write lock (exposed for store layer)."""
    await _write_lock.acquire()


def release_write_lock() -> None:
    """Release the module-level write lock (exposed for store layer)."""
    _write_lock.release()


# ---------------------------------------------------------------------------
# Schema v1
# ---------------------------------------------------------------------------

_V1_TABLES: list[str] = [
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT DEFAULT 'New conversation',
        model_id TEXT,
        mode TEXT DEFAULT 'cowork',
        session_name TEXT,
        working_dir TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT DEFAULT '',
        blocks TEXT,
        attachments TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        mode TEXT DEFAULT 'cowork',
        session_name TEXT,
        working_dir TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        claimed_by_conversation_id TEXT,
        claimed_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id TEXT NOT NULL,
        conversation_id TEXT,
        event_type TEXT NOT NULL,
        subject TEXT NOT NULL,
        details TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_messages_conversation
        ON messages(conversation_id)""",
    """CREATE INDEX IF NOT EXISTS idx_audit_logs_trace
        ON audit_logs(trace_id)""",
    """CREATE INDEX IF NOT EXISTS idx_audit_logs_conversation
        ON audit_logs(conversation_id)""",
]


async def _create_tables_v1(db: aiosqlite.Connection) -> None:
    """Execute all v1 CREATE TABLE / INDEX statements."""
    for sql in _V1_TABLES:
        await db.execute(sql)
    await db.commit()


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


async def _get_user_version(db: aiosqlite.Connection) -> int:
    """Return the current ``PRAGMA user_version``."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _set_user_version(db: aiosqlite.Connection, version: int) -> None:
    """Update the schema version."""
    await db.execute(f"PRAGMA user_version = {int(version)}")


async def _migrate(db: aiosqlite.Connection, from_version: int) -> None:
    """Run migrations sequentially from ``from_version`` to current."""
    for v in range(from_version + 1, CURRENT_SCHEMA_VERSION + 1):
        migrator = _MIGRATIONS.get(v)
        if migrator is not None:
            logger.info("Running migration v%d -> v%d", v - 1, v)
            await migrator(db)
        await _set_user_version(db, v)
    logger.info("Database migrated to v%d: %s", CURRENT_SCHEMA_VERSION, db_path())


# Registry: target_version -> async migration function
_MIGRATIONS: dict[int, object] = {
    # Future migrations:
    # 2: _migrate_v1_to_v2,
}
