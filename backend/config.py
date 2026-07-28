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
    # The guest loop may request an alternate model base_url (e.g. a local
    # ollama), but the HOST attaches the real key to that request — so an
    # unchecked guest-supplied base_url is a key-exfil seam (point the host at
    # an attacker endpoint, harvest the Bearer key). Only these hosts are
    # honored; anything else is refused and the call falls back to the default.
    # deepseek_base_url is always allowed on top of this list.
    model_base_url_allowlist: list[str] = ["http://127.0.0.1:11434",
                                           "http://localhost:11434"]
    model_name: str = "deepseek-v4-flash"
    # Models the nav switcher may select at runtime (persisted in session_state,
    # no restart). Agents with an explicit model pin are unaffected by the switch.
    model_choices: list[str] = ["deepseek-v4-flash", "deepseek-v4-pro"]
    # v4-flash caps output at 384K (verified accepted by the API). The old
    # 4096 was a v3-era default: large tool-call payloads (whole-file writes)
    # hit it mid-arguments and dispatched as empty {} args.
    model_max_tokens: int = 384_000
    # Main generation temperature. 0.7 keeps personality and fluency; the
    # no-tools self-check pass (which runs at 0.0) is what enforces rules, so
    # the main turn doesn't need to run cold. Tunable via JARVIS_MODEL_TEMPERATURE.
    model_temperature: float = 0.7

    # DeepSeek pricing per 1M tokens (USD), for the Logs cost tab. Input is
    # split by the API into cache hit/miss; output is flat. Override via
    # JARVIS_PRICE_* when the provider reprices. The flat price_* fields are
    # the fallback for models not in model_prices (and stay = flash).
    price_cache_hit_per_m: float = 0.0028
    price_cache_miss_per_m: float = 0.14
    price_output_per_m: float = 0.28
    model_prices: dict[str, dict[str, float]] = {
        "deepseek-v4-flash": {"cache_hit": 0.0028, "cache_miss": 0.14,
                              "output": 0.28},
        "deepseek-v4-pro": {"cache_hit": 0.003625, "cache_miss": 0.435,
                            "output": 0.87},
    }
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
    # v4-flash is a 1M-token window (input + output share it); 64k was v3's.
    model_context_window: int = 1_000_000
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
    vm_shell_port: int = 5557            # guest co-working PTY; host dials CID : this
    vm_guest_cid: int = 3                # guest CID (>=3); host is always CID 2
    # Operator co-working shell INTO the guest (browser terminal panel + the
    # `guest-shell` CLI). Both go through the host broker, which pins the guest
    # (no idle-scrub mid-session) and is the only path in — the guest stays
    # unreachable except through the supervisor. On by default; a kill switch.
    guest_shell_enabled: bool = True
    vm_memory_mb: int = 768
    vm_cpus: int = 2
    vm_boot_timeout_seconds: int = 120
    # When true, chat turns run the ReAct loop INSIDE the guest (Phase 3), with
    # host tools brokered over vsock. Off by default until the guest path is the
    # proven default (cutover in M4); flip per-turn tests via the flag.
    use_guest_loop: bool = False
    # True only inside the guest (the guest config shim overrides it). run_code
    # keys on this: on the host it is always False, so the tool refuses to run
    # code anywhere the sandbox isn't.
    in_guest: bool = False
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
    # SearXNG is a metasearch proxy; its default engine mix on the main server
    # is mostly rate-limited/blocked (google/ddg/brave/startpage/qwant all
    # return 0 results — measured 2026-07-21). Pin the engines that still work
    # so web_search returns real links instead of just Wikipedia infoboxes.
    # Empty string = let SearXNG use its own default set.
    searxng_engines: str = "bing,mojeek,wikipedia"
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

    # --- Monitored egress (Layer 3) ---------------------------------------
    # OFF by default: the guest stays netless (`-nic none`) until this is flipped
    # on and soaked, exactly like the use_guest_loop cutover. When true the guest
    # gets a tap NIC bridged to the host; ALL egress crosses the host proxy on
    # vm_egress_proxy_port, the LAN is dropped by nftables, and DNS is forced
    # through the host resolver (backend/vm/egress_proxy.py + vm/net_up.sh). The
    # guest still holds no key — secrets are injected at the proxy, on the wire.
    vm_egress: bool = False
    vm_egress_tap: str = "jvtap0"
    vm_egress_host_ip: str = "10.201.0.1"     # host side of the point-to-point
    vm_egress_proxy_port: int = 8443          # host TLS-terminating forward proxy
    vm_egress_pcap: bool = True               # tcpdump ring buffer on the tap
    # NOTE: the guest IP (10.201.0.2), DNS port, and the RFC1918 LAN-denied
    # ranges are FIXED constants baked into vm/net/jarvis-egress.nft,
    # vm/net/dnsmasq-egress.conf and guest/backend/server.py — they are not
    # configurable here (a settings knob nothing reads would silently drift
    # from what nftables actually enforces).
    # A brand-new project inherits this shared "general" allowlist (deny-by-
    # default vs the open internet), which trains up as new hosts are approved.
    # Seeded with the developer toolchain so pip/npm/git work on day one;
    # everything else is denied + queued. Sensitive projects get a scoped policy
    # (their own egress_policy row) instead of inheriting this.
    egress_seed_hosts: list[str] = [
        "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
        "github.com", "codeload.github.com", "objects.githubusercontent.com",
        "raw.githubusercontent.com", "api.github.com", "deb.debian.org",
        "security.debian.org", "crates.io", "static.crates.io", "proxy.golang.org",
    ]

    # --- Egress anomaly detection (Layer 3) -------------------------------
    # A trip auto-cuts the offending host (nftables drop) and raises a
    # security_event. A new/unapproved host does NOT trip these — it is simply
    # denied and queued for approval; only exfil-shaped behaviour alerts.
    egress_entropy_threshold: float = 3.8     # Shannon bits/char of the hostname; DGA / DNS-tunnel tell
    egress_volume_multiple: float = 8.0       # bytes-to-host over this * baseline = a spike
    egress_volume_min_bytes: int = 1_000_000  # ignore spikes below this (tiny-baseline noise)
    egress_beacon_min_hits: int = 6           # regular hits to one host before cadence is judged
    egress_beacon_cv_max: float = 0.15        # inter-arrival coefficient-of-variation below this = beacon

    # --- Triage reviewer (backend/reviewer.py) ----------------------------
    # The isolated no-tools second reader that clears routine noise from the
    # review/network queues. The sweep interval is the auto cadence (<= 0
    # removes the background task entirely; the GUI toggle pauses it at
    # runtime); the budget bounds one run's model spend.
    reviewer_interval_seconds: int = 300
    reviewer_batch_size: int = 40             # items per model call
    reviewer_max_items: int = 400             # items per run (a run per sweep drains a backlog)
    reviewer_budget_input: int = 1_500_000    # per-run token caps
    reviewer_budget_output: int = 60_000
    # The never-auto-approve entropy floor. Deliberately looser than
    # egress_entropy_threshold (3.8): that one alerts on live traffic, but as
    # a triage guard it flags ordinary long hostnames
    # (raw.githubusercontent.com = 3.80). DGA-shaped names run > 4.2; below
    # the floor the model still judges the host and can flag it on its own.
    reviewer_entropy_guard: float = 4.2

    # --- Golden image lifecycle (Layer 1) ---------------------------------
    # The VM widget goes amber when the running image is older than this; a
    # monthly systemd timer rebuilds a fresh versioned base (never in place).
    vm_image_max_age_days: int = 35


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
