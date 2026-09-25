import aiosqlite

from .config import settings, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- API tokens minted for a logged-in computer/CLI (`jav3 login`).
-- Only the sha256 of the token is stored; the raw token is shown once, when
-- the login code is redeemed, and never again. Revocable; expires at
-- expires_at or after device_token_idle_days without use (last_used_at); dies
-- with the user it was minted for (user_id).
CREATE TABLE IF NOT EXISTS device_tokens (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    hostname TEXT,
    platform TEXT,
    paired_by TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0,
    -- what the token may reach: 'cli' (chat, via auth.require_actor) or
    -- 'desk' (only /api/desk/ws, backend/desk.py). Neither reaches the other.
    scope TEXT NOT NULL DEFAULT 'cli'
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
    -- run-tree (M7): a conversation is a node in an agent job. NULL parent +
    -- kind 'chat' is an ordinary chat; head/leader/subagent are job nodes.
    parent_conversation_id INTEGER REFERENCES conversations(id),
    kind TEXT NOT NULL DEFAULT 'chat',
    rollup TEXT,
    job_id TEXT
);
-- Sidebar folders for chats. A chat is in at most one (conversations.folder_id,
-- added in init_db); deleting a folder unfiles its chats, never deletes them.
-- position is the operator's order; ties fall back to id.
CREATE TABLE IF NOT EXISTS chat_folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
CREATE TABLE IF NOT EXISTS session_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS fetched_urls (
    id INTEGER PRIMARY KEY,
    session TEXT NOT NULL,           -- operation scope (web_session), 'global' fallback
    url TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session, url)
);
CREATE TABLE IF NOT EXISTS git_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'commit',      -- commit | remote (connect+push)
    message TEXT NOT NULL,           -- commit message, or the remote URL
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
    pending_approval INTEGER NOT NULL DEFAULT 0,  -- Jav3-proposed, not yet decided
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
    verdict TEXT NOT NULL DEFAULT 'allow',      -- allow | deny | cut | auto_allow | auto_deny
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Egress auto mode (backend/egress_auto.py): hosts the auto-guesser let
-- through. Kept OUT of egress_policy.hosts on purpose — an auto entry is scoped
-- to the one project that asked (it never widens the shared general list),
-- expires on its own, and one row is one-click revocable. Rows are kept after
-- revoke/promote/expiry: they are the daily-cap ledger and the audit trail.
CREATE TABLE IF NOT EXISTS egress_auto_allow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL,
    host TEXT NOT NULL,
    rule TEXT NOT NULL,                  -- known | model
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,                     -- operator revoked it
    promoted_at TEXT                     -- operator kept it (moved onto the real allowlist)
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
-- Transient security alerts (anomaly, host cut, gate flag, secret leak, stale
-- image). Persisted + ack-able, unlike the poll-derived notifications aggregate.
CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- egress_anomaly | host_cut | gate_flag | secret_leak | image_stale | egress_auto
    severity TEXT NOT NULL DEFAULT 'warn',      -- info | warn | critical
    project_slug TEXT,
    summary TEXT NOT NULL,
    detail TEXT,                         -- JSON payload
    acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged_at TEXT
);
-- Triage reviewer (backend/reviewer.py): one row per sweep, one per action.
-- The log is the audit + undo surface for the reviewer's autonomous
-- approves/acks; triage_* columns on the queue tables (added in init_db)
-- carry each item's verdict + reason into the Review/Network views.
CREATE TABLE IF NOT EXISTS triage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'manual',      -- manual | auto
    examined INTEGER NOT NULL DEFAULT 0,
    allowed INTEGER NOT NULL DEFAULT 0,
    acked INTEGER NOT NULL DEFAULT 0,
    flagged INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS triage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES triage_runs(id),
    item_kind TEXT NOT NULL,             -- egress | alert
    item_id INTEGER NOT NULL,
    project_slug TEXT,
    subject TEXT,                        -- the host, or the alert summary head
    verdict TEXT NOT NULL,               -- allow | ack | flag
    reason TEXT,
    action TEXT NOT NULL,                -- approved | acked | flagged
    detail TEXT,                         -- JSON (e.g. which allowlist an approval extended)
    undone INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- One agent addressing another (WP5). The row IS the message: `send_message`
-- writes it and returns, the recipient's loop claims it between iterations.
-- Nothing about delivery lives in memory, so a message survives a restart and
-- can never be shed under backpressure the way the browser bus deliberately
-- sheds token events (bus.publish_to).
--
-- Addressed EITHER to a conversation (to_conversation_id, exact) or to an agent
-- slug (to_agent_slug, whichever turn running as that agent claims it first).
-- delivered_at NULL is the inbox; the claim is a single UPDATE ... RETURNING,
-- so two concurrent turns of one slug can never both take the same message.
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY,
    from_conversation_id INTEGER REFERENCES conversations(id),
    from_label TEXT NOT NULL,            -- agent slug, or 'jav3' for a plain chat
    to_conversation_id INTEGER REFERENCES conversations(id),
    to_agent_slug TEXT,
    project_slug TEXT,                   -- the SENDER's project, for context
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT,                   -- NULL = still in the inbox
    delivered_to INTEGER REFERENCES conversations(id)
);
-- Computer use (backend/desk.py). Grants are per desk token and set ONLY from
-- Settings (cookie routes); a desk client never writes them. shell is
-- off|ask|trusted; trusted lapses at trusted_until. allowlist is a JSON list
-- of argv patterns that run without an approval.
CREATE TABLE IF NOT EXISTS desk_grants (
    device_id INTEGER PRIMARY KEY REFERENCES device_tokens(id) ON DELETE CASCADE,
    screen INTEGER NOT NULL DEFAULT 0,
    input INTEGER NOT NULL DEFAULT 0,
    shell TEXT NOT NULL DEFAULT 'off',
    trusted_until TEXT,
    allowlist TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- one row per desk action (the model's side is in tool_calls). Typed text is
-- stored as its length + sha256, never the text.
CREATE TABLE IF NOT EXISTS desk_actions (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL,
    verb TEXT NOT NULL,
    params TEXT,
    conversation_id INTEGER,
    op_id TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    approver TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_desk_actions_device ON desk_actions(device_id, id);
-- shell commands waiting on the operator (allow once / always / deny)
CREATE TABLE IF NOT EXISTS desk_shell_pending (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    cwd TEXT,
    conversation_id INTEGER,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|allowed|denied|expired
    decided_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
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
        if "persist_approved" not in cols:
            # approved persistence inside the guest VM: the operator's opt-in
            # (GUI only) for a project's /persist disk. In the DB, not
            # .workspace.json — the agent writes that file (workspace_panel),
            # and an approval the agent can grant itself is no approval.
            await db.execute("ALTER TABLE projects ADD COLUMN "
                             "persist_approved INTEGER NOT NULL DEFAULT 0")
            await db.execute("ALTER TABLE projects ADD COLUMN persist_approved_at TEXT")
        # device tokens gained a lifetime and an owner (2026-09 login review):
        # an absolute expiry, an idle clock, and the user id they die with.
        # Existing rows are backfilled so nothing that was live gets a free
        # pass: expiry from created_at, last use from the old last_seen, the
        # owner by the username that minted it — a token whose minting user is
        # gone gets no owner, and so stops verifying.
        async with db.execute("PRAGMA table_info(device_tokens)") as cur:
            dcols = [r["name"] for r in await cur.fetchall()]
        for col, decl in (("user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
                          ("expires_at", "TEXT"), ("last_used_at", "TEXT"),
                          # every token minted before scopes was a CLI's
                          ("scope", "TEXT NOT NULL DEFAULT 'cli'")):
            if col not in dcols:
                await db.execute(f"ALTER TABLE device_tokens ADD COLUMN {col} {decl}")
        await db.execute(
            "UPDATE device_tokens SET expires_at = datetime(created_at, ?) "
            "WHERE expires_at IS NULL", (f"+{int(settings.device_token_ttl_days)} days",))
        if "last_used_at" not in dcols and "last_seen" in dcols:
            await db.execute("UPDATE device_tokens SET last_used_at = last_seen")
        if "user_id" not in dcols:
            # once, at migration: re-running it would re-attach an orphaned
            # token to a NEW account that reused a deleted user's name
            await db.execute(
                "UPDATE device_tokens SET user_id = (SELECT id FROM users "
                "WHERE users.username = device_tokens.paired_by)")
        # run-tree columns on an already-created conversations table
        async with db.execute("PRAGMA table_info(conversations)") as cur:
            ccols = [r["name"] for r in await cur.fetchall()]
        # schedules proposed by Jav3 (schedule_update tool) land disabled
        # with this flag set; the bell surfaces them and the operator's
        # enable/pause decision clears it
        async with db.execute("PRAGMA table_info(schedules)") as cur:
            scols = [r["name"] for r in await cur.fetchall()]
        if "pending_approval" not in scols:
            await db.execute("ALTER TABLE schedules ADD COLUMN "
                             "pending_approval INTEGER NOT NULL DEFAULT 0")
        # deleting a schedule is a soft delete (same bin idiom as projects):
        # the row stops running immediately but stays restorable until the
        # scheduler sweeps it past its window
        if "deleted_at" not in scols:
            await db.execute("ALTER TABLE schedules ADD COLUMN deleted_at TEXT")
        # provider/model the run is pinned to; NULL = the agent's own pin
        # (agent kind) or the default model
        if "model" not in scols:
            await db.execute("ALTER TABLE schedules ADD COLUMN model TEXT")
        async with db.execute("PRAGMA table_info(git_requests)") as cur:
            gcols = [r["name"] for r in await cur.fetchall()]
        if "kind" not in gcols:
            # remote-connect requests ride the same approval queue as commits
            await db.execute("ALTER TABLE git_requests ADD COLUMN "
                             "kind TEXT NOT NULL DEFAULT 'commit'")
        # triage reviewer verdict columns on the two queue tables
        for table in ("egress_pending", "security_events"):
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                tcols = [r["name"] for r in await cur.fetchall()]
            for col in ("triage_verdict", "triage_reason", "triage_at"):
                if col not in tcols:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        # egress auto mode's guess for a queued host (allow | deny | unsure |
        # revoked) — the dedupe for its security event and the memo that stops
        # it re-asking the model on every retry of the same host
        async with db.execute("PRAGMA table_info(egress_pending)") as cur:
            pcols = [r["name"] for r in await cur.fetchall()]
        for col in ("auto_verdict", "auto_rule", "auto_reason", "auto_at"):
            if col not in pcols:
                await db.execute(f"ALTER TABLE egress_pending ADD COLUMN {col} TEXT")
        # messages gained `model`: with voice running a 4B locally and DeepSeek
        # only on escalation, "which brain wrote this" stopped being knowable
        # from the reply alone — and that is exactly what the operator needs to
        # trust a transcript. NULL means the turn predates this column.
        async with db.execute("PRAGMA table_info(messages)") as cur:
            mcols = [r["name"] for r in await cur.fetchall()]
        if mcols and "model" not in mcols:
            await db.execute("ALTER TABLE messages ADD COLUMN model TEXT")
        # tool_calls gained `message_id`: which assistant reply this call
        # belongs to. Without it a past turn's tool work cannot be replayed
        # into the model-facing history (timestamps are second-resolution and
        # a voice turn fits inside one second), and a history that shows only
        # prose teaches a small model that talking IS acting. NULL == a row
        # from before this column, which simply replays without its trace.
        async with db.execute("PRAGMA table_info(tool_calls)") as cur:
            tcols = [r["name"] for r in await cur.fetchall()]
        if tcols and "message_id" not in tcols:
            await db.execute("ALTER TABLE tool_calls ADD COLUMN message_id INTEGER")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_msg ON tool_calls(message_id)")
        # the Logs list and every transcript read tool_calls by conversation;
        # without this each of those was a full scan of the table
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_conv ON tool_calls(conversation_id)")
        for col, decl in (("parent_conversation_id", "INTEGER"),
                          ("kind", "TEXT NOT NULL DEFAULT 'chat'"),
                          ("rollup", "TEXT"), ("job_id", "TEXT"),
                          # tier-2 compaction checkpoint: the structured
                          # summary + id of the last message it covers
                          ("compact_summary", "TEXT"),
                          ("compact_upto", "INTEGER"),
                          # 0: follow whatever project is loaded (the historic
                          # behaviour, and the default for a new chat).
                          # 1: project_id is the answer verbatim — including
                          # NULL, which means deliberately no project, so the
                          # turn falls back to the chat's artifact store.
                          ("project_locked", "INTEGER NOT NULL DEFAULT 0"),
                          # which agent definition this conversation runs AS.
                          # NULL is central Jav3 (every chat before this
                          # column, funnel nodes, temp agents). A slug means the
                          # turn's system prompt IS agents/<slug>/AGENT.md —
                          # never "on behalf of": the comms inbox claims mail by
                          # this column, so a funnel leaf stamped with its
                          # launcher's slug would steal that agent's messages.
                          # Provenance is parent_conversation_id instead.
                          ("agent_slug", "TEXT"),
                          # incognito marker, and the source of truth for
                          # "is this conversation ephemeral". The turn's
                          # ephemeral flag lives on the broker envelope, which
                          # is released BEFORE _run_chat_turn wipes the row —
                          # so between the two a registry read said "not
                          # ephemeral" while the row still existed, and a
                          # guessed id could be messaged into a turn about to be
                          # erased. This column outlives the envelope and is
                          # deleted WITH the row (_drop_references), so the
                          # incognito refusal holds until the row is actually
                          # gone. It never persists past the turn: an incognito
                          # row is wiped at turn end, and this goes with it.
                          ("ephemeral", "INTEGER NOT NULL DEFAULT 0"),
                          # the device token (device_tokens.id) that opened
                          # this conversation; NULL = the operator's session
                          # or an internal run. Attribution, so the operator
                          # can see which threads a computer started.
                          ("device_id", "INTEGER"),
                          # sidebar organisation. The FK points OUT of the
                          # conversation, so deleting a chat needs nothing from
                          # _drop_references; deleting a folder unfiles its chats
                          # (SET NULL — foreign_keys is ON in get_db).
                          ("folder_id",
                           "INTEGER REFERENCES chat_folders(id) ON DELETE SET NULL"),
                          ("starred", "INTEGER NOT NULL DEFAULT 0"),
                          # the provider/model this thread is pinned to
                          # (POST /api/chat `model`); NULL follows the default
                          ("model", "TEXT")):
            if col not in ccols:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {col} {decl}")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_parent ON conversations(parent_conversation_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_job ON conversations(job_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_agent ON conversations(agent_slug)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_folder ON conversations(folder_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_starred ON conversations(starred) "
            "WHERE starred = 1")
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
            "CREATE INDEX IF NOT EXISTS idx_egress_auto_allow_proj "
            "ON egress_auto_allow(project_slug, created_at)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_security_events_ack ON security_events(acknowledged, created_at)")
        # the inbox claim runs once per ReAct round of every addressable turn,
        # so it has to be an index hit and not a scan of every message ever sent
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_msg_inbox_conv "
            "ON agent_messages(delivered_at, to_conversation_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_msg_inbox_slug "
            "ON agent_messages(delivered_at, to_agent_slug)")
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
                            locked: bool = False, agent: str | None = None,
                            ephemeral: bool = False, device_id: int | None = None,
                            commit: bool = True) -> int:
    """Create a conversation node and return its id — the one place that resolves
    a project slug to its id and inserts the row.

    `kind` tags the node for the run tree (chat/head/leader/subagent/scout/reader/
    agent/scheduled). `title` is stored verbatim as the summary (callers format
    their own prefixes). Pass commit=False when the caller adds a first message in
    the same transaction and commits itself. Follow-ups (peak confirmation, the
    opening user message) are the caller's, using the returned id.

    `locked` pins the binding: the turn uses this project (or no project at all,
    if `project` is None) instead of following whatever is loaded globally.

    `agent` is the definition slug this conversation runs AS (None = central
    Jav3). Set once, at creation: an identity that could change mid-thread
    would leave a transcript nobody can attribute.

    `ephemeral` marks an incognito conversation. It is the row-level source of
    truth another agent's send_message consults (agentmsg._is_incognito) to
    refuse messaging a turn that is about to be wiped — the broker envelope that
    also carries this is released too early to be relied on. Deleted with the
    row at turn end, so it leaves no trace."""
    project_id = None
    if project:
        async with db.execute("SELECT id FROM projects WHERE slug = ?", (project,)) as cur:
            row = await cur.fetchone()
        project_id = row["id"] if row else None
    cur = await db.execute(
        "INSERT INTO conversations (project_id, summary, kind, parent_conversation_id, "
        "job_id, project_locked, agent_slug, ephemeral, device_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, title, kind, parent, job_id, 1 if locked else 0, agent,
         1 if ephemeral else 0, device_id))
    if commit:
        await db.commit()
    return cur.lastrowid


async def launcher(db: aiosqlite.Connection) -> tuple[int | None, str | None]:
    """(conversation that launched the current work, agent it works for).

    The first is the running turn's conversation (runtime.conversation_id — the
    broker restores it for a brokered tool), so spawned agents and funnel/
    research heads record it as their `parent` and the run tree stays connected
    where Jav3 delegates. None outside a turn (an HTTP-launched job, a
    schedule), and None if that row is already gone — a dangling parent would
    fail the foreign key.

    The second is the nearest ancestor's `agent_slug`: a funnel launched by a
    temp agent that `builder` spawned still belongs to builder. It is for
    labelling events and outputs, NEVER for stamping a child's own agent_slug
    (see the column's comment in init_db)."""
    from . import runtime
    cid = runtime.conversation_id.get()
    if cid is None:
        return None, None
    lineage = await _lineage_slugs(db, cid)
    if lineage is None:
        return None, None
    return cid, next((s for s in lineage if s), None)


async def owning_agent(db: aiosqlite.Connection, cid: int | None) -> str | None:
    """The nearest `agent_slug` on this conversation or its ancestors."""
    lineage = await _lineage_slugs(db, cid) if cid is not None else None
    return next((s for s in lineage or () if s), None)


async def _lineage_slugs(db: aiosqlite.Connection, cid: int) -> list | None:
    """agent_slug of the row and each ancestor, nearest first (None: no row).
    Depth-capped so a malformed parent cycle cannot spin."""
    async with db.execute(
        "WITH RECURSIVE up(id, parent, slug, d) AS ("
        "  SELECT id, parent_conversation_id, agent_slug, 0 FROM conversations "
        "  WHERE id = ? "
        "  UNION ALL SELECT c.id, c.parent_conversation_id, c.agent_slug, up.d + 1 "
        "  FROM conversations c JOIN up ON c.id = up.parent WHERE up.d < 32) "
        "SELECT slug FROM up ORDER BY d", (cid,)) as cur:
        rows = await cur.fetchall()
    return [r["slug"] for r in rows] if rows else None
