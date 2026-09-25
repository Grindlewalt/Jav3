import logging
import os
import secrets
import shutil
from pathlib import Path

from pydantic import PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)

# The durable-state dirs a pre-state-dir install kept at the repo root. skills/
# is left out of the "has content" probe on purpose: the repo ships skills there,
# so a fresh checkout would always look like a legacy install.
STATE_DIRS = ("memory", "projects", "skills", "agents", "data")
_LEGACY_PROBE = ("memory", "projects", "agents")


def _has_content(d: Path) -> bool:
    try:
        return any(p.name != ".gitkeep" for p in d.iterdir())
    except OSError:
        return False


def has_state(root: Path) -> bool:
    """True when `root` holds a real Jav3 state layout (a DB, or any memory/
    project/agent file) rather than empty scaffolding."""
    return ((root / "data" / "jarvis.db").exists()
            or any(_has_content(root / n) for n in _LEGACY_PROBE))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=os.path.expanduser("~/.config/jarvis/env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_dir: Path = BASE_DIR
    # ONE relocatable root for everything durable (JARVIS_STATE_DIR). The dirs
    # below are derived from it unless set explicitly (an explicit
    # JARVIS_<NAME>_DIR always wins). The code checkout holds no state, so it can
    # be re-cloned or moved freely and a backup is one directory. A box still on
    # the old in-checkout layout keeps running from it until
    # `python -m backend.cli migrate-state` moves it (see _resolve_state_dirs).
    state_dir: Path = Path(os.path.expanduser("~/.local/share/jarvis"))
    data_dir: Path | None = None
    memory_dir: Path | None = None
    projects_dir: Path | None = None
    # Serve each project's git repo read-only over HTTP at /git/<slug>, so the
    # operator can `git clone/pull http://<host>/git/<slug>` over the LAN. Basic
    # auth against the app's own users; pull-only (never receive-pack).
    git_serve_enabled: bool = True
    skills_dir: Path | None = None
    agents_dir: Path | None = None
    # Tools are CODE (handler.py modules shipped in the repo, pushed to the
    # guest), not state — they stay in the checkout.
    tools_dir: Path = BASE_DIR / "tools"
    frontend_dist: Path = BASE_DIR / "frontend" / "dist"

    db_path: Path | None = None

    jwt_secret: str = ""
    jwt_ttl_hours: int = 24 * 7
    # A `jav3 login` device token dies this many days after it was minted, or
    # after this many days without being used, whichever comes first. The CLI
    # answers either with "run: jav3 login".
    device_token_ttl_days: int = 90
    device_token_idle_days: int = 30
    # Add `Secure` to the session cookie so it never rides plaintext HTTP. OFF
    # by default because a LAN deployment is reached over plain http (a Secure
    # cookie would silently never be sent there); set true only when every
    # client reaches Jav3 over HTTPS, e.g. behind a TLS reverse proxy.
    cookie_secure: bool = False
    # Extra hostnames allowed as the Origin of a cookie-authed state-changing
    # request, on top of the request's own Host. The CSRF-origin check refuses a
    # request whose Origin is any OTHER host — SameSite=Lax does not stop a
    # same-site sibling (another app served under the same parent domain).
    # Add a name here only for a page on another host that legitimately posts
    # to Jav3.
    # Each entry matches by hostname under any scheme: a bare host allows any
    # port, host:port only that port. The server's own LAN names are allowed
    # without listing them, but only on lan_port. A reverse proxy that
    # forwards the public Host needs nothing here; list its public name if it
    # rewrites Host (JARVIS_CSRF_ALLOWED_HOSTS='["<public-host>"]').
    csrf_allowed_hosts: list[str] = []
    # The committer email on every commit Jav3 makes in a project repo.
    git_author_email: str = "jav3@localhost"

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
    # Empty = derived (see _derive_service_urls): localhost ollama plus the
    # services_host's ollama (:11434), its GPU-pinned voice instance (:11435)
    # and llama.cpp voice tier (:11436). An explicit list replaces all of it.
    model_base_url_allowlist: list[str] = []
    # "deepseek-flash" is the API's name for the current Flash — V4.1 Flash
    # since 2026-09-10. The old "deepseek-v4-flash" still resolves to it, and
    # "deepseek-v4-pro" is routed to it (at Flash price) from 2026-09-14 until
    # a V4.1 Pro exists, so Pro is not offered: the name would buy a costlier
    # copy of the same model.
    model_name: str = "deepseek-flash"
    # Models the nav switcher may select at runtime (persisted in session_state,
    # no restart). Agents with an explicit model pin are unaffected by the switch.
    model_choices: list[str] = ["deepseek-flash"]
    # Human label + one-line blurb per model id, served by GET /api/model so
    # the GUI never hardcodes model names or versions. A choice missing here
    # falls back to its raw id. Keep blurbs free of versions/dates — the id
    # is a rolling alias, so anything dated here goes stale on their release.
    model_labels: dict[str, dict[str, str]] = {
        "deepseek-flash": {"label": "flash",
                           "blurb": "DeepSeek's current Flash model"},
    }
    # Flash caps output at 384K (verified accepted by the API on v4, and the
    # v4.1 limit is the same). The old 4096 was a v3-era default: large
    # tool-call payloads (whole-file writes) hit it mid-arguments and
    # dispatched as empty {} args.
    model_max_tokens: int = 384_000
    # Main generation temperature. 0.7 keeps personality and fluency; the
    # no-tools self-check pass (which runs at 0.0) is what enforces rules, so
    # the main turn doesn't need to run cold. Tunable via JARVIS_MODEL_TEMPERATURE.
    model_temperature: float = 0.7

    # DeepSeek pricing per 1M tokens (USD), for the Logs cost tab. Input is
    # split by the API into cache hit/miss; output is flat. Override via
    # JARVIS_PRICE_* when the provider reprices. The flat price_* fields are
    # the fallback for models not in model_prices (and stay = flash).
    #
    # Off-peak list price (DeepSeek's peak — 01:00-04:00 and 06:00-10:00 UTC,
    # Mon-Fri — is double; the peak gate in agent/model.py is what asks before
    # spending in it). The old names keep the prices they were billed at, so
    # rows recorded under them still cost what they cost.
    price_cache_hit_per_m: float = 0.003
    price_cache_miss_per_m: float = 0.15
    price_output_per_m: float = 0.60
    model_prices: dict[str, dict[str, float]] = {
        "deepseek-flash": {"cache_hit": 0.003, "cache_miss": 0.15,
                           "output": 0.60},
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
    # The server's own LAN names/IPs are allowed on top of this (backend/lan.py).
    media_hosts: list[str] = ["upload.wikimedia.org", "i.imgur.com"]

    # --- LAN (backend/lan.py) -----------------------------------------------
    # Advertise the server over mDNS (_http._tcp) so the LAN reaches it at
    # http://<instance_name>.local:<lan_port>. Failure is non-fatal: a box with
    # no multicast just isn't discoverable.
    mdns: bool = True
    # The mDNS instance/host label. Empty = the app's own name, lowercased; the
    # machine's hostname if that yields nothing usable.
    instance_name: str = ""
    # The port uvicorn listens on (scripts/jarvis.service --port). Only used to
    # advertise; it does not change the bind.
    lan_port: int = 8000
    # The host the companion services (SearXNG, ollama/llama.cpp, the voice
    # sidecar) live on. Each service URL below that is left unset is derived
    # from it; an explicitly set per-service URL always wins.
    services_host: str = "localhost"

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

    # The explicit orchestrator (backend/plan.py): how many checklist items run
    # at once (clamped to the funnel's fan-out cap), how many times a failed
    # item is re-spawned, and how long an item may go without a tool call or a
    # message before the runner nudges it (and, one window later, re-spawns it
    # once). The tick is how often the runner reconciles operator edits.
    plan_max_concurrent: int = 3
    plan_attempts_max: int = 2
    plan_stall_seconds: int = 300
    plan_tick_seconds: float = 5.0

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
    # How many recent screenshots stay in-context as real image blocks; older
    # ones become a text stub. Each screenshot is ~1k+ tokens and re-sent every
    # iteration, so a browsing loop only keeps the CURRENT view by default.
    screenshot_keep_recent: int = 1

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
    vm_dir: Path | None = None           # default <data_dir>/vm
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
    # (`use_guest_loop` is gone — M4e, 2026-08-02. Every turn's ReAct loop runs
    # inside the guest with host tools brokered over vsock; there is no host-side
    # loop left to fall back to. The Pi ran the guest path from 07-15 to 08-02
    # without once needing the fallback.)
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
    # Approved persistence (v2.1): a per-project data disk at
    # <vm_dir>/persist/<slug>.qcow2, hot-attached to the guest and mounted at
    # /persist ONLY for turns of a project the operator approved (GUI, never the
    # agent). Everything else about the guest stays disposable. The disk is
    # sparse; this is its hard virtual size. `vm_persist_enabled` is the global
    # kill switch — off, no disk is ever attached whatever a project says.
    vm_persist_enabled: bool = True
    vm_persist_max_mb: int = 2048

    # Web access (secure + inert). The agent never touches the raw internet:
    # host-side tools query SearXNG and fetch pages, strip them to plain text,
    # and refuse internal/private targets (SSRF guard).
    searxng_url: str = ""               # derived: http://<services_host>:8080
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

    # --- Voice desktop mode -------------------------------------------------
    # OFF by default. The Pi never runs audio ML: the browser captures/plays,
    # and STT/TTS run on the voicebox sidecar (voicebox/ in this repo, deployed
    # on the main server) — the backend just relays opaque PCM bytes between
    # the two websockets and orchestrates turns/barge-in/clones.
    voice_enabled: bool = False
    voice_sidecar_url: str = ""         # derived: ws://<services_host>:8100/ws
    voice_sidecar_token: str = ""       # must match VOICEBOX_TOKEN on the sidecar
    voice_max_workers: int = 3          # backgrounded twins per voice session
    # Local fast tier: when set (e.g. "llama3.1:8b"), voice turns run on this
    # ollama model by default — conversational stuff, media control, quick
    # questions stay on the operator's own GPUs with no API cost or peak gate.
    # The model escalates to DeepSeek only by [ESCALATE] + spoken permission,
    # or immediately when the operator says "smart model" / "deepseek".
    # Empty string = every voice turn runs on DeepSeek as before.
    voice_local_model: str = ""
    voice_local_base_url: str = ""      # derived: http://<services_host>:11434/v1
    # How much of each past tool result the local tier's history replays. The
    # trace exists to show that acting happens through tool calls, not to
    # re-feed the payload — a couple of lines is the whole signal, and the
    # window is 16k. 0 disables the replay.
    voice_local_tool_trace_chars: int = 200
    # Sampling for the local tier only (never sent to DeepSeek). A 4B with a
    # prose-heavy history loops on its own last phrasing; a small presence
    # penalty is the cheap half of the fix. The token cap is a runaway guard —
    # generous, because truncating a real answer mid-sentence is a worse
    # failure than a rare long ramble the operator can simply talk over
    # (barge-in is the real backstop). The 384k default is nonsense here.
    voice_local_presence_penalty: float = 0.5
    voice_local_max_tokens: int = 4096
    # llama.cpp's -c for the voice tier. Keep in step with scripts/
    # llama-voice.service: this is what compaction sizes the local tier's
    # history against, and overflowing it does not error — it silently drops
    # the front of the prompt, taking the tool definitions with it, which
    # looks exactly like a model too dumb to call tools.
    # 32k since 2026-08-04: the LLM now has the 12 GB card to itself (TTS and
    # whisper both moved to the Ti), so the KV for it is free real estate —
    # 9B weights + 32k KV is 6.3 GB of 12 GB. This roughly triples how long a
    # voice conversation runs before compaction has to stall a turn.
    voice_local_context_window: int = 32_768
    # All ~30 library titles ride the local tier's system prompt: matching a
    # spoken title against a list it can already see beats a music_search
    # round trip (see one-call-tools-not-conversations). Refreshed on this
    # TTL, in the background — the prompt is the llama.cpp KV prefix, so it
    # must stay byte-stable between refreshes.
    voice_library_in_prompt: bool = True
    voice_library_max_tracks: int = 120
    voice_library_ttl_seconds: int = 900
    # wake-word standby: seconds of idle before he stops listening for
    # anything but "hey Jav3" again (only in effect when the sidecar
    # reports a wake model in its ready message)
    # Seconds of quiet before he drops back to wake-word standby. Short on
    # purpose: re-arming costs one word ("Jav3, ...") and the same utterance
    # carries the request, so a tight window is cheap — and it stops him
    # answering a conversation that was never aimed at him.
    voice_wake_timeout: int = 15
    # Quiet hours for the double-clap gesture. The clap is the one trigger with
    # no confirmation step and no words in it, so a dropped book at 3am starts
    # music; overnight it is muted. "hey Jav3" is deliberately NOT gated —
    # it takes a spoken sentence to fire, so it cannot go off by accident, and
    # the operator still wants a working Jav3 at night. The window wraps
    # midnight; set start == end to disable without touching the flag.
    voice_clap_curfew: bool = True
    voice_clap_curfew_start: str = "22:30"
    voice_clap_curfew_end: str = "07:30"
    # Where the voice display is mirrored, if anywhere: the projection mapper's
    # /voice endpoint (same host/token as its MCP server). Empty = off, and the
    # push is best-effort — a projector that is not plugged in must never slow
    # a spoken turn down.
    voice_projector_feed: bool = True
    # Bearer token for the headless desktop client (clients/voicedesk/). It has
    # no browser and no login session, so it cannot present the cookie the
    # /voice page does. Empty = the token path does not exist, which is the
    # right default: an unset secret must never mean "no check".
    voice_client_token: str = ""

    # --- MCP clients (Jav3 reaching OUT) ---------------------------------
    # The projection mapper's in-app MCP server. Off unless both are set. What
    # Jav3 may do there is fixed by OUR tools/projector_*/TOOL.md manifest,
    # not by what the server advertises — see backend/mcp.py for why that
    # distinction is the whole security design.
    mcp_projector_url: str = ""          # e.g. http://10.0.0.40:8765/mcp
    mcp_projector_token: str = ""
    # Run the drift check (the server's tools/list vs our pin) once per client,
    # before the first call. Cheap and cached; a server that is off just skips it.
    mcp_verify_manifest: bool = True
    # ...and file a security event when the server offers something unpinned.
    # OFF until a real tools/list has been seen once: no round trip has ever
    # completed against the projector app, `security.raise_event` has no dedup,
    # and a server whose names merely differ from our pin would file a warning
    # per call. The journal line from the check above is what tells you it is
    # safe to turn this on.
    mcp_alert_unpinned: bool = False

    # OpenClaw skill import (backend/skillimport.py). Git URLs must resolve to
    # a public host unless the host is named here (comma-separated) — e.g. the
    # operator's own git server holding a curated, vetted skill set; http:// is
    # accepted only for these. ClawHub, the public registry that shipped
    # hundreds of malicious skills, is OFF unless explicitly switched on.
    skill_import_allow_hosts: str = ""
    skill_import_clawhub: bool = False
    skill_import_clawhub_url: str = "https://clawhub.ai"

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

    # --- Egress auto mode (backend/egress_auto.py) -------------------------
    # Off unless the operator flips it (globally or per project). A host it
    # lets through is scoped to that one project and expires after ttl_days
    # unless the operator promotes it; past daily_cap auto-allows in 24h a
    # project's new hosts wait for the operator again. The model is asked only
    # when the deterministic rules have no answer, with its own tiny budget and
    # a timeout (the guest's request is held open while it decides).
    egress_auto_ttl_days: int = 7
    egress_auto_daily_cap: int = 20
    egress_auto_model_timeout: float = 20.0
    egress_auto_budget_input: int = 4_000
    egress_auto_budget_output: int = 300

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

    # --- Backups (backend/backup.py, rclone) ------------------------------
    # `rclone sync` of the durable state to `backup_remote` (an rclone remote
    # path, e.g. "myremote:jav3-backup"; the rclone config itself is the
    # user's, at rclone's default path). Empty = backups off. These are the
    # defaults; PUT /api/backup/config overlays them (backend/backup.py).
    backup_remote: str = ""
    backup_rclone: str = "rclone"
    # Also back up the env file, secrets.json and the JWT secret — ONLY ever
    # through an rclone crypt layer: either a crypt remote the user defined
    # (`backup_crypt_remote`) or one built on the fly over
    # <backup_remote>/secrets from these passwords. Neither set = refuse.
    backup_include_secrets: bool = False
    backup_crypt_remote: str = ""
    backup_crypt_password: str = ""
    backup_crypt_password2: str = ""

    @model_validator(mode="after")
    def _derive_service_urls(self):
        """Fill every service URL left unset from services_host. Emptiness (not
        model_fields_set) is the test, so JARVIS_SEARXNG_URL= also means
        "derive"; any non-empty explicit value wins untouched."""
        h = self.services_host.strip() or "localhost"
        if ":" in h and not h.startswith("["):
            h = f"[{h}]"                 # bare IPv6 literal
        if not self.searxng_url:
            self.searxng_url = f"http://{h}:8080"
        if not self.voice_sidecar_url:
            self.voice_sidecar_url = f"ws://{h}:8100/ws"
        if not self.voice_local_base_url:
            self.voice_local_base_url = f"http://{h}:11434/v1"
        if not self.model_base_url_allowlist:
            self.model_base_url_allowlist = list(dict.fromkeys(
                ["http://127.0.0.1:11434", "http://localhost:11434"]
                + [f"http://{h}:{p}" for p in (11434, 11435, 11436)]))
        return self

    # True when the state dir was empty but the repo checkout still held a
    # pre-state-dir layout, so the dirs resolved there instead (see below).
    _legacy_layout: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _resolve_state_dirs(self):
        explicit = self.model_fields_set
        root = self.state_dir
        # A deploy must never strand the running box: if the state dir holds
        # nothing yet but the checkout does, keep using the checkout (ensure_dirs
        # warns). Data is only ever moved by the explicit migrate-state command.
        if (self.state_dir.resolve() != self.base_dir.resolve()
                and not has_state(self.state_dir) and has_state(self.base_dir)):
            root = self.base_dir
            self._legacy_layout = True
        for name in ("data", "memory", "projects", "skills", "agents"):
            field = f"{name}_dir"
            if field not in explicit or getattr(self, field) is None:
                setattr(self, field, root / name)
        if "db_path" not in explicit or self.db_path is None:
            self.db_path = self.data_dir / "jarvis.db"
        if "vm_dir" not in explicit or self.vm_dir is None:
            self.vm_dir = self.data_dir / "vm"
        return self

    @property
    def legacy_layout(self) -> bool:
        return self._legacy_layout


settings = Settings()
_warned_legacy = False


def ensure_dirs() -> None:
    global _warned_legacy
    if settings.legacy_layout and not _warned_legacy:
        _warned_legacy = True
        log.warning(
            "Jav3 state is still inside the code checkout (%s) and the state "
            "dir %s is empty — running from the old location. Stop the service "
            "and run `python -m backend.cli migrate-state` to move it.",
            settings.base_dir, settings.state_dir)
    for d in (settings.data_dir, settings.memory_dir, settings.memory_dir / "notes",
              settings.projects_dir, settings.skills_dir, settings.agents_dir,
              settings.tools_dir, settings.vm_dir):
        d.mkdir(parents=True, exist_ok=True)
    _seed_shipped_skills()


def _seed_shipped_skills() -> None:
    """The repo ships a few skills; a skills dir outside the checkout starts
    with copies of them. Never overwrites — once copied, the skill is the
    operator's to edit or delete (a deleted one is not resurrected while its
    marker stays)."""
    shipped = settings.base_dir / "skills"
    if not shipped.is_dir() or shipped.resolve() == settings.skills_dir.resolve():
        return
    marker = settings.skills_dir / ".seeded"
    seen = set(marker.read_text().split()) if marker.exists() else set()
    new = []
    for src in sorted(shipped.iterdir()):
        if not (src / "SKILL.md").is_file() or src.name in seen:
            continue
        dest = settings.skills_dir / src.name
        if not dest.exists():
            shutil.copytree(src, dest)
        new.append(src.name)
    if new:
        marker.write_text("\n".join(sorted(seen | set(new))) + "\n")


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
