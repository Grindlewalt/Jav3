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

    # Workspace runner (light host-side sandbox; the QEMU VM is pass 2)
    run_python: str = "python3"
    run_timeout_seconds: int = 60
    run_max_mem_mb: int = 768

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

    # Sandbox VM (M3). Persistent: boots once and stays up; nuke is a
    # recovery action (recreate the overlay), not a per-run ritual.
    vm_dir: Path = BASE_DIR / "data" / "vm"
    # M4 moved the VM onto a tap network — direct address, plain SSH port.
    vm_ssh_host: str = "10.66.0.10"
    vm_ssh_port: int = 22
    vm_ssh_user: str = "agent"
    vm_unit: str = "jarvis-vm.service"
    vm_workspace: str = "/workspace"
    vm_run_timeout_seconds: int = 300
    vm_boot_timeout_seconds: int = 120
    vm_push_max_mb: int = 64

    # M4 sandbox review console. The nft table + drop-log prefixes the gate
    # analysis keys off; RFC-1918 defines what counts as "your LAN"; the globs
    # flag a run touching secrets / proprietary / financial paths.
    nft_table: str = "jarvis_vm"
    lan_cidrs: list[str] = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    sandbox_sensitive_globs: list[str] = [
        "**/.env", "**/.env.*", "**/secrets/**", "**/*.pem", "**/*.key",
        "**/id_rsa", "**/id_ed25519", "**/credentials*", "**/.aws/**",
        "finance/**", "**/proprietary/**",
    ]


settings = Settings()


def ensure_dirs() -> None:
    for d in (settings.data_dir, settings.memory_dir, settings.memory_dir / "notes",
              settings.projects_dir, settings.skills_dir, settings.agents_dir,
              settings.tools_dir):
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
