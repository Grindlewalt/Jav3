import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=os.path.expanduser("~/.config/jarvis/env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    memory_dir: Path = BASE_DIR / "memory"
    projects_dir: Path = BASE_DIR / "projects"
    skills_dir: Path = BASE_DIR / "skills"
    agents_dir: Path = BASE_DIR / "agents"
    tools_dir: Path = BASE_DIR / "tools"
    frontend_dist: Path = BASE_DIR / "frontend" / "dist"

    db_path: Path = BASE_DIR / "data" / "jarvis.db"

    jwt_secret: str = ""
    jwt_ttl_hours: int = 24 * 7

    # Operator API keys the agent uses by {{secret:NAME}} placeholder but
    # never sees (backend/secrets.py). Lives next to the env file.
    secrets_path: Path = Path(os.path.expanduser("~/.config/jarvis/secrets.json"))

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    model_max_tokens: int = 4096
    # Main generation temperature. 0.7 keeps personality and fluency; the
    # no-tools self-check pass (which runs at 0.0) is what enforces rules, so
    # the main turn doesn't need to run cold. Tunable via JARVIS_MODEL_TEMPERATURE.
    model_temperature: float = 0.7

    # DeepSeek pricing per 1M tokens (USD), for the Logs cost tab. Input is
    # split by the API into cache hit/miss; output is flat. Override via
    # JARVIS_PRICE_* when the provider reprices.
    price_cache_hit_per_m: float = 0.0028
    price_cache_miss_per_m: float = 0.14
    price_output_per_m: float = 0.28
    # Raw-context capture (the exact message array sent per model call) is
    # opt-in and heavy; captured blobs older than this are nulled out.
    context_capture_keep_days: int = 7

    # Remote hosts whose images/video the render surfaces (chat markdown + the
    # dashboard iframe) may auto-load. Everything else is blocked, so a model
    # can't beacon data out through a resource URL to an arbitrary host. Same
    # spirit as an egress allowlist; tune via JARVIS_MEDIA_HOSTS (JSON list).
    media_hosts: list[str] = ["atomosnas", "upload.wikimedia.org", "i.imgur.com"]

    # Peak-pricing windows, local time, "HH:MM-HH:MM". May cross midnight.
    peak_windows: list[str] = ["18:00-21:00", "23:00-03:00"]
    # How long a user's "yes, use the API" answer stays valid.
    peak_confirm_ttl_minutes: int = 60

    # Backstop for the main/chat loop. Subagents get a much tighter cap below:
    # a research subagent reading 1-3 sources needs a handful of rounds, not 60
    # — leaving it high let subagents read 40-85 pages and burn millions of
    # tokens re-sending the pile each iteration.
    max_react_iterations: int = 60
    subagent_max_iterations: int = 12
    recent_message_limit: int = 40

    # Delegation pressure: a long turn gets steered mid-flight. At
    # `delegate_nudge_round` a note pushes the model to hand remaining
    # gathering to research/spawn_agent and to work a todo plan; at 2/3 of
    # the round cap a wrap-up note tells it to start concluding.
    delegate_nudge_round: int = 12

    # Tier-2 conversation compaction: when system prompt + history approach
    # the model's context window, the older portion is summarized into a
    # structured brief persisted on the conversation, and only the recent
    # ~compact_recent_fraction of tokens rides verbatim. The trigger is an
    # EFFECTIVE window: context − reserved output (model_max_tokens) − buffer
    # (tool specs, rule injection, chars/4 estimate error). After
    # compact_failures_max consecutive summarize failures a conversation
    # falls back to the plain recent_message_limit window.
    model_context_window: int = 64_000
    compact_buffer_tokens: int = 8_000
    compact_recent_fraction: float = 0.3
    compact_failures_max: int = 3
    compact_transcript_max_chars: int = 200_000

    # Tool results inside the ReAct loop. A result is re-sent on EVERY later
    # iteration of the turn, so an uncapped read_file dump is the one quadratic
    # cost in the system: cap what enters the message list, and once a result
    # is `keep_recent` rounds old, replace anything bigger than `evict_chars`
    # with a one-line stub (the model can re-call the tool if it still needs it).
    tool_result_max_chars: int = 12_000
    tool_result_evict_chars: int = 4_000
    tool_result_keep_recent: int = 2

    # Dead-end circuit-breaker (the convo-12 post-mortem: 173 tool calls of
    # near-duplicate searches and failing installs, never concluding). After
    # `error_streak` consecutive failed/empty tool calls the model gets a
    # corrective note; at `force_answer` tools are withdrawn so it must stop
    # and report what it couldn't find. Identical repeats of a read-only call
    # within a turn short-circuit without dispatching.
    dead_end_error_streak: int = 4
    dead_end_force_answer: int = 8

    # Post-tool plan re-check: every N tool rounds (when the todo tool is
    # offered and no other nudge fired that round) a one-line progress check
    # rides the last tool result — mark done items, aim the next call at the
    # next open item. Counters the drift where a long turn free-associates
    # its next call instead of following its own plan. 0 disables.
    plan_recheck_every: int = 6

    # Hand-rolled web gathering: once a turn has made this many direct
    # web_search/web_read/read_and_summarize calls (and the research tool is
    # offered), a one-shot note tells the model to hand the remainder to
    # research instead of reading pages itself. 0 disables.
    web_handroll_nudge: int = 6

    # Transient model-API failures (connect errors, 5xx) retry with exponential
    # backoff — but only if no tokens have streamed to the client yet, so a
    # retry can never duplicate visible output.
    model_retries: int = 2
    model_retry_backoff_seconds: float = 0.5

    # Token budget (chars/4 estimate) for the active-project block of the
    # system prompt: project.md plus the operator-ticked context files. Files
    # past the budget degrade to a path index readable on demand with read_file.
    project_context_budget_tokens: int = 12_000

    # A spawned agent's report rides the PARENT loop's context for the rest of
    # the turn; reports past this size get compacted to a summary first.
    agent_report_max_chars: int = 4_000

    # Per-operation token budget (shared across every agent in a chat turn or a
    # research job). DeepSeek caches prompt prefixes automatically, so the input
    # cap is generous. ~5M in / ~1M out is roughly a cent (cached) to a dime.
    max_op_input_tokens: int = 5_000_000
    max_op_output_tokens: int = 1_000_000

    # F5 interim: a chat turn that changed project files but never called
    # journal_update gets one auto-written journal line, so project.md stays
    # current without relying on the model remembering.
    auto_journal: bool = True

    # Auto-approve the FINAL research document (research/<topic>.md) so it goes
    # straight to canonical instead of waiting in the approval queue. Node
    # scratch files under runs/ stay staged regardless.
    research_auto_approve: bool = True

    # Archive upload caps (POST /upload_archive extraction)
    upload_max_uncompressed_mb: int = 200
    upload_max_files: int = 5000

    # Workspace runner (light host-side runner: rlimits + timeout)
    run_python: str = "python3"
    run_timeout_seconds: int = 60
    run_max_mem_mb: int = 768

    # Sandbox VM (Phase 2: a disposable KVM/QEMU guest reachable ONLY over vsock).
    # The guest has no NIC; its one path off-box is the host model gateway, which
    # listens on vsock port `vm_vsock_port`. base-<version>.qcow2 is the read-only
    # golden image (built by vm/build_base.sh); guests run a qcow2 overlay on it.
    vm_dir: Path = BASE_DIR / "data" / "vm"
    vm_image_version: str = "v1"
    vm_vsock_port: int = 5555            # host gateway; guest dials CID 2 : this
    vm_guest_cid: int = 3                # guest CID (>=3); host is always CID 2
    vm_memory_mb: int = 768
    vm_cpus: int = 2
    vm_boot_timeout_seconds: int = 120
    # When true, chat turns run the ReAct loop INSIDE the guest (Phase 3), with
    # host tools brokered over vsock. Off by default until the guest path is the
    # proven default (cutover in M4); flip per-turn tests via the flag.
    use_guest_loop: bool = False
    # Idle scrub (M4c): reboot the single guest once it has been idle (no in-flight
    # turn) for this many seconds, so the next operation batch lands in a FRESH
    # guest instead of inheriting the previous one's state. The reboot happens
    # during idle time, so it costs no per-turn latency and needs no second guest
    # (a warm pool is the wrong fit for this Pi's memory). 0 disables it — the
    # guest then persists across operations until a manual /api/vm/nuke.
    vm_idle_scrub_seconds: int = 0
    vm_reaper_interval_seconds: int = 30

    # Web access (secure + inert). The agent never touches the raw internet:
    # host-side tools query SearXNG and fetch pages, strip them to plain text,
    # and refuse internal/private targets (SSRF guard).
    searxng_url: str = "http://10.0.0.58:8080"
    web_search_results: int = 8
    web_fetch_timeout: int = 15
    web_max_bytes: int = 2_000_000      # stop reading a page past this
    # cap the inert text handed to the model. Was 20k (~5k tokens/page); a
    # research subagent reads a few pages and re-sends them each loop, so a
    # smaller slice cuts token throughput hard while keeping the useful content.
    web_max_chars: int = 6_000
    # short-TTL cache of fetched page text (and summaries keyed by focus), so a
    # re-read within a task skips the download AND the summarize model call.
    web_cache_ttl_seconds: int = 900
    web_cache_max_entries: int = 50


settings = Settings()


def ensure_dirs() -> None:
    for d in (settings.data_dir, settings.memory_dir, settings.memory_dir / "notes",
              settings.projects_dir, settings.skills_dir, settings.agents_dir,
              settings.tools_dir, settings.vm_dir):
        d.mkdir(parents=True, exist_ok=True)


def get_jwt_secret() -> str:
    """Env-provided secret wins; otherwise generate once and persist under data/."""
    if settings.jwt_secret:
        return settings.jwt_secret
    ensure_dirs()
    secret_file = settings.data_dir / "jwt_secret"
    if not secret_file.exists():
        secret_file.write_text(secrets.token_urlsafe(48))
        secret_file.chmod(0o600)
    return secret_file.read_text().strip()
