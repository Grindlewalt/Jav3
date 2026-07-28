"""Per-project autonomy dial — how much the agent may do unattended.

Four levels, increasing capability:

  read_only  only observe (read files, search, browse the web)
  stage      + file writes (live — the quarantine is gone; writes are
             advisory-scanned and git is the undo surface)
  gated      + spawn agents/research
  full       + propose git commits (the current, unrestricted default)

A project with no setting is `full` — so this is opt-in and never regresses
existing behavior. Enforcement is an **allowlist**: `read_only` exposes only the
explicit read set, and any tool we don't recognise defaults to `full`-only, so a
new or unknown tool is never accidentally handed to a restricted project.

This dial only narrows which tools the model is even offered on a turn; the
durable boundaries (write scans, commit approval) enforce independently.
"""

LEVELS = ("read_only", "stage", "gated", "full")
_RANK = {name: i for i, name in enumerate(LEVELS)}

# explicit categorisation by the minimum level a tool needs
_READ = {
    "git_diff", "git_status", "list_files", "read_file", "memory_read",
    "read_and_summarize", "web_read", "web_search", "search_codebase",
    "load_project",
}
_STAGE = {
    "write_file", "edit_file", "dashboard", "crawl_codebase", "journal_update",
    "todo_update", "memory_write", "create_agent", "schedule_update",
}
_GATED = {
    "research", "spawn_agent", "spawn_temp_agent", "deploy_agents",
    # code execution: contained in the no-key/no-net guest, but running code
    # is still more than file edits — gated tier
    "run_code",
}
_COMMIT = {"git_commit_request", "git_remote_request"}

# Tools a subagent or team worker is NEVER handed, regardless of the autonomy
# dial above: they never launch whole teams and never mint persistent
# infrastructure (new agents, schedules) — those stay a conversation-head
# decision. Enforced where subagent/worker tool sets are built
# (agents_run._agent_tools, deploy_agents); deploy_agents' own _in_funnel
# contextvar is a second, whole-subtree fence.
NON_DELEGABLE = frozenset({
    "spawn_agent", "spawn_temp_agent", "deploy_agents", "create_agent",
    "schedule_update",
})

# the spawn tools alone nest, capped (2026-07-23 operator ask): an agent at
# spawn depth d < MAX_SPAWN_DEPTH gets spawn_agent/spawn_temp_agent back
# (head chat = depth 0, so Jarvis -> agent -> agent, then leaf). The cap is
# the fork-bomb fence; the shared per-op Budget fences total cost. Funnel
# workers (deploy_agents, runs_api) keep the full NON_DELEGABLE set — the
# orchestrator builds its own capped tree and workers must not sprout
# side-trees around it.
MAX_SPAWN_DEPTH = 2


def tool_min_rank(name: str) -> int:
    """Lowest autonomy rank at which `name` is offered. Unknown -> full (3)."""
    if name in _READ:
        return 0
    if name in _STAGE:
        return 1
    if name in _GATED:
        return 2
    if name in _COMMIT:
        return 3
    return 3            # unrecognised tools only at full autonomy


def normalize(level: str | None) -> str | None:
    """A stored value -> a valid level, or None (== full, unfiltered)."""
    if level in _RANK and level != "full":
        return level
    return None         # None / 'full' / anything unknown => no restriction


def allows(level: str | None, tool_name: str) -> bool:
    lvl = normalize(level)
    if lvl is None:
        return True
    return tool_min_rank(tool_name) <= _RANK[lvl]


def filter_entries(entries: list[dict], level: str | None) -> list[dict]:
    """Keep only the registry entries a project at `level` may use."""
    if normalize(level) is None:
        return entries
    return [e for e in entries if allows(level, e.get("name", ""))]
