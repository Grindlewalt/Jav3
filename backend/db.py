import aiosqlite

from .config import settings, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    github_remote TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    summary TEXT,
    fidelity_tier INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    status TEXT NOT NULL DEFAULT 'pending',
    exec_log_path TEXT,
    net_log_path TEXT,
    pushed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS session_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'agent' | 'jarvis'
    agent_slug TEXT,                 -- when kind = agent
    project_slug TEXT,               -- context to run in (optional)
    task TEXT NOT NULL,
    cadence_kind TEXT NOT NULL,      -- 'daily' | 'interval'
    daily_at TEXT,                   -- 'HH:MM' local, when cadence = daily
    interval_minutes INTEGER,        -- when cadence = interval
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run TEXT NOT NULL,          -- ISO local
    last_run TEXT,
    last_result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def get_db() -> aiosqlite.Connection:
    ensure_dirs()
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def init_db() -> None:
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        async with db.execute("PRAGMA table_info(projects)") as cur:
            cols = [r["name"] for r in await cur.fetchall()]
        if "deleted_at" not in cols:
            await db.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT")
        await db.commit()
    finally:
        await db.close()


async def get_state(db: aiosqlite.Connection, key: str) -> str | None:
    async with db.execute("SELECT value FROM session_state WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else None


async def set_state(db: aiosqlite.Connection, key: str, value: str | None) -> None:
    if value is None:
        await db.execute("DELETE FROM session_state WHERE key = ?", (key,))
    else:
        await db.execute(
            "INSERT INTO session_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    await db.commit()
