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
    fidelity_tier INTEGER NOT NULL DEFAULT 0,
    -- run-tree (M7): a conversation is a node in an agent job. NULL parent +
    -- kind 'chat' is an ordinary chat; head/leader/subagent are job nodes.
    parent_conversation_id INTEGER REFERENCES conversations(id),
    kind TEXT NOT NULL DEFAULT 'chat',
    rollup TEXT,
    job_id TEXT
);
-- indexes on the run-tree columns are created in init_db AFTER the migration
-- ALTERs, so they don't reference columns a pre-existing DB hasn't gained yet.
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
CREATE TABLE IF NOT EXISTS fetched_urls (
    id INTEGER PRIMARY KEY,
    session TEXT NOT NULL,           -- research scope (project slug, or 'global')
    url TEXT NOT NULL,
    title TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session, url)
);
CREATE TABLE IF NOT EXISTS git_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL,
    message TEXT NOT NULL,
    paths TEXT,                      -- JSON array or NULL = all changes
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    commit_sha TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    decided_at TEXT
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
    pending_approval INTEGER NOT NULL DEFAULT 0,  -- Jarvis-proposed, not yet decided
    next_run TEXT NOT NULL,          -- ISO local
    last_run TEXT,
    last_result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    cache_miss INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sandbox_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dest TEXT NOT NULL,              -- human label (hostname if known, else ip)
    ip TEXT NOT NULL,               -- the address programmed into nftables
    port INTEGER NOT NULL,
    proto TEXT NOT NULL DEFAULT 'tcp',
    scope TEXT NOT NULL DEFAULT 'wan',   -- wan | lan
    note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(ip, port, proto)
);
"""


async def get_db() -> aiosqlite.Connection:
    ensure_dirs()
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    # WAL + a busy timeout let parallel agent nodes write the single DB file
    # concurrently without 'database is locked' (M7 runs many nodes at once).
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 5000")
    return db


async def init_db() -> None:
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        async with db.execute("PRAGMA table_info(projects)") as cur:
            cols = [r["name"] for r in await cur.fetchall()]
        if "deleted_at" not in cols:
            await db.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT")
        if "is_hidden" not in cols:
            # artifact stores: per-chat projects that hold files made in
            # project-less chats — invisible on the Projects dashboard until
            # converted or merged (the Artifacts page is their view)
            await db.execute(
                "ALTER TABLE projects ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0")
        if "autonomy" not in cols:
            # autonomy dial: read_only|stage|gated|full (NULL == full, unrestricted)
            await db.execute("ALTER TABLE projects ADD COLUMN autonomy TEXT")
        # run-tree columns on an already-created conversations table
        async with db.execute("PRAGMA table_info(conversations)") as cur:
            ccols = [r["name"] for r in await cur.fetchall()]
        # schedules proposed by Jarvis (schedule_update tool) land disabled
        # with this flag set; the bell surfaces them and the operator's
        # enable/pause decision clears it
        async with db.execute("PRAGMA table_info(schedules)") as cur:
            scols = [r["name"] for r in await cur.fetchall()]
        if "pending_approval" not in scols:
            await db.execute("ALTER TABLE schedules ADD COLUMN "
                             "pending_approval INTEGER NOT NULL DEFAULT 0")
        # egress-allowlist hardening: a rule can carry an expiry (NULL = never)
        # so temporary allowances (e.g. "PyPI for this session") auto-revoke.
        async with db.execute("PRAGMA table_info(sandbox_rules)") as cur:
            srcols = [r["name"] for r in await cur.fetchall()]
        if "expires_at" not in srcols:
            await db.execute("ALTER TABLE sandbox_rules ADD COLUMN expires_at TEXT")
        for col, decl in (("parent_conversation_id", "INTEGER"),
                          ("kind", "TEXT NOT NULL DEFAULT 'chat'"),
                          ("rollup", "TEXT"), ("job_id", "TEXT"),
                          # tier-2 compaction checkpoint: the structured
                          # summary + id of the last message it covers
                          ("compact_summary", "TEXT"),
                          ("compact_upto", "INTEGER")):
            if col not in ccols:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {col} {decl}")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_parent ON conversations(parent_conversation_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_job ON conversations(job_id)")
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
