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
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,             -- NULL: utility calls (naming, summarize) or incognito
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    cache_miss INTEGER NOT NULL DEFAULT 0,
    context TEXT,                        -- JSON {messages, n_tools}; only when capture is on
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Monitored egress (Layer 3). Per-project egress policy. A project with no row
-- inherits the shared baseline row (slug '__general__', seeded from
-- egress_seed_hosts). Sensitive projects get their own row with a scoped list.
CREATE TABLE IF NOT EXISTS egress_policy (
    project_slug TEXT PRIMARY KEY,       -- project slug, or '__general__' for the shared baseline
    mode TEXT NOT NULL DEFAULT 'allowlist',    -- allowlist (deny-by-default) | denylist (allow-by-default) | denyall (netless)
    inherit_general INTEGER NOT NULL DEFAULT 1,  -- also permit the general list (allowlist mode only)
    hosts TEXT NOT NULL DEFAULT '[]',    -- JSON array of host patterns (allow or deny per mode)
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Denied host -> approval queue. Approving a row promotes the host into the
-- project's (or the general) allowlist — this is how the list "trains up".
CREATE TABLE IF NOT EXISTS egress_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL,
    host TEXT NOT NULL,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    hit_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
    decided_at TEXT,
    UNIQUE(project_slug, host)
);
-- Every request the proxy sees: the live-feed source AND the volume baseline.
CREATE TABLE IF NOT EXISTS egress_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT,
    conversation_id INTEGER,
    op_id TEXT,
    host TEXT NOT NULL,
    method TEXT,
    path TEXT,
    bytes_out INTEGER NOT NULL DEFAULT 0,
    bytes_in INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT 'allow',      -- allow | deny | cut
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Which secrets a project's guest may use. The proxy injects a {{secret:X}}
-- only if the project holds a granted row for X — a compromised project can't
-- reach every key the operator owns.
CREATE TABLE IF NOT EXISTS project_secret_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL,
    secret_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'granted',     -- granted | pending | revoked
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_slug, secret_name)
);
-- Deterministic diff-gate trips on staged writes. A flagged (path,trigger)
-- blocks approval of that staged file until the operator acknowledges it.
CREATE TABLE IF NOT EXISTS gate_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL,
    path TEXT NOT NULL,
    trigger TEXT NOT NULL,               -- new_import | network_call | high_entropy | secret_leak | logging_removed | assertion_removed
    detail TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged_at TEXT,
    UNIQUE(project_slug, path, trigger)
);
-- Transient security alerts (anomaly, host cut, gate flag, secret leak, stale
-- image). Persisted + ack-able, unlike the poll-derived notifications aggregate.
CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- egress_anomaly | host_cut | gate_flag | secret_leak | image_stale
    severity TEXT NOT NULL DEFAULT 'warn',      -- info | warn | critical
    project_slug TEXT,
    summary TEXT NOT NULL,
    detail TEXT,                         -- JSON payload
    acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged_at TEXT
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_calls_conv ON model_calls(conversation_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_calls_created ON model_calls(created_at)")
        # monitored-egress query paths: the live feed + volume baseline scan by
        # (project, host, time); the pending queue and alerts by open status.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_egress_events_proj ON egress_events(project_slug, created_at)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_egress_events_host ON egress_events(host, created_at)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_egress_pending_status ON egress_pending(status)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gate_flags_ack ON gate_flags(project_slug, acknowledged)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_security_events_ack ON security_events(acknowledged, created_at)")
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


async def open_conversation(db: aiosqlite.Connection, *, project: str | None,
                            title: str, kind: str = "chat",
                            parent: int | None = None, job_id: str | None = None,
                            commit: bool = True) -> int:
    """Create a conversation node and return its id — the one place that resolves
    a project slug to its id and inserts the row.

    `kind` tags the node for the run tree (chat/head/leader/subagent/scout/reader/
    agent/scheduled). `title` is stored verbatim as the summary (callers format
    their own prefixes). Pass commit=False when the caller adds a first message in
    the same transaction and commits itself. Follow-ups (peak confirmation, the
    opening user message) are the caller's, using the returned id."""
    project_id = None
    if project:
        async with db.execute("SELECT id FROM projects WHERE slug = ?", (project,)) as cur:
            row = await cur.fetchone()
        project_id = row["id"] if row else None
    cur = await db.execute(
        "INSERT INTO conversations (project_id, summary, kind, parent_conversation_id, "
        "job_id) VALUES (?, ?, ?, ?, ?)", (project_id, title, kind, parent, job_id))
    if commit:
        await db.commit()
    return cur.lastrowid
