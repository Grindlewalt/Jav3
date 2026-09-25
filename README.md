# Jav3

A self-hosted personal AI agent with durable file-backed memory, multi-agent
orchestration and a voice interface, running as a single service on a Raspberry
Pi. Its defining constraint is that the agent is treated as untrusted: the entire
reasoning loop executes inside a disposable KVM guest holding no API key, no
database and no secrets, and every capability it has — models, tools, filesystem
writes, network egress — is brokered through a gated host supervisor.

The agent's **entire reasoning loop runs inside a disposable KVM guest**,
reachable only over AF_VSOCK. The host is a thin supervisor: a model gateway (the
only path to DeepSeek) and a tool broker (the only path to tools). When monitored
egress is on, the guest's only route off-box is a host proxy that applies
per-project policy, injects granted secrets on the wire, meters every byte and
can cut a host on an anomaly. See `SECURITY-RESIDUAL-RISK.md` for what that does
and does not buy.

## What's in it

- **Untrusted-agent architecture** — VM-isolated ReAct loop, vsock RPC, a tool
  broker, a write chokepoint with deterministic diff gates, taint tracking on
  web-derived memory, SSRF-guarded fetch, and an egress proxy with an
  allowlist-training approval queue
- **Multi-agent orchestration** — a recursive head → leader → subagent hierarchy
  with context narrowing down and rollups flowing up, plus a purpose-built
  search-and-divide research pipeline (scout → parallel readers → synthesis) and
  a shared token budget on a contextvar that spans every child agent
- **Extension by folder, not by code** — tools, agents and skills are markdown +
  a handler, discovered and hot-reloaded at runtime, so the agent can author its
  own capabilities
- **Voice** — a wake-word → STT → agent → TTS loop tuned to an ~880 ms
  round trip, with a local llama.cpp fast tier for latency-sensitive turns
- **Live GUI** — React SPA over SSE: streaming chat, a draggable project
  workspace, watchable agent run trees, network + security review centers

Roughly 21k lines of Python and 8k of JS across ~80 test modules and 43 tools.

## Layout

- `backend/` — FastAPI app: auth, memory assembly, projects, chat (SSE), agent
  loop, tool broker, egress proxy, git/secrets gates
- `frontend/` — React (Vite) SPA, built to static and served by FastAPI
- `tools/<name>/` — a tool is a folder: `TOOL.md` (frontmatter + schema) +
  `handler.py`; hot-reloaded, no code change needed to add one
- `agents/<slug>/AGENT.md`, `skills/<name>/SKILL.md` — same idea
- `memory/` — soul.md / user.md / env.md / all-projects.md / notes (runtime state)
- `projects/<slug>/` — project.md journal + code/ (a git repo from creation)
- `vm/` — golden-image builder, guest bootstrap, nftables egress rules
- `voicebox/` — the STT/wake-word/TTS sidecar (dockerised, runs off-Pi on a GPU box)
- `docs/SELF.md` — the agent's own technical manual, served by the `self_docs` tool
- `scripts/` — Pi setup, systemd units, backup + image-rebuild timers, E2E smoke

## Install

```
git clone https://github.com/Grindlewalt/Jav3.git ~/jarvis
cd ~/jarvis && bash scripts/install.sh
echo 'JARVIS_DEEPSEEK_API_KEY=sk-...' >> ~/.config/jarvis/env
.venv/bin/python -m backend.cli create-user <name>
systemctl --user restart jarvis     # GUI at http://<host>:8000
```

`scripts/install.sh` is re-runnable, so it is the upgrade path too. It works on
Debian/Ubuntu, Arch and Fedora, on arm64 and x86_64.

The handful of steps that genuinely need root are collected into one phase, so
they are one paste rather than a conversation:

```
bash scripts/install.sh --check                 # what is missing? changes nothing
bash scripts/install.sh --target user@host      # ...on a remote host, over ssh
sudo bash scripts/install.sh --root-phase       # packages, kvm, vsock, linger
bash scripts/install.sh                         # everything else, unprivileged
```

Every failed check prints its own fix command. One failure no script can clear:
if the CPU's virtualization extension is off in firmware, `--check` says so and
stops — the agent loop runs inside a KVM guest and there is no host-side
fallback, so that is a reboot into BIOS by a human.

**State dir.** Everything durable — `memory/`, `projects/`, `skills/`,
`agents/` and `data/` (the SQLite DB, JWT secret, guest images) — lives in one
directory outside the checkout: `~/.local/share/jarvis`, or `JARVIS_STATE_DIR`.
`python -m backend.cli paths` prints where each piece resolves. A box installed
before the state dir keeps running from its checkout (with a warning at start)
until you stop the service and run `python -m backend.cli migrate-state`, which
copies, verifies and only then removes the old copies.

**Backups.** `python -m backend.cli backup` (and the hourly `jarvis-backup.timer`)
runs `rclone sync` of the state to `JARVIS_BACKUP_REMOTE` (any rclone remote,
e.g. `myremote:jav3-backup`; set it up with `rclone config`), with a consistent
DB snapshot. Secrets (env file, `secrets.json`) are included only when
`JARVIS_BACKUP_INCLUDE_SECRETS` is on, and only through an rclone crypt layer
(`JARVIS_BACKUP_CRYPT_PASSWORD`, or your own crypt remote in
`JARVIS_BACKUP_CRYPT_REMOTE`). `python -m backend.cli restore` pulls it back.
The same settings are editable at `/api/backup/config`.

**Moving hosts.** `--from` migrates the durable state that is *not* in git —
`memory/`, `projects/`, `skills/`, `agents/` and the SQLite DB — from either
layout (an old checkout, or a state dir) into this box's state dir:

```
bash scripts/install.sh --from grindlewalt@oldhost:jarvis
```

It snapshots the source DB through SQLite's backup API (a live WAL database
cannot be safely copied as a file), verifies `integrity_check` on arrival, and
refuses to overwrite non-empty local state without `--force`. If the source is
unreachable it fails hard rather than quietly starting a fresh install over the
top of a migration. `data/vm/` is deliberately not copied: the golden image is
built for the source host's architecture and is rebuilt natively instead.

**Behind a reverse proxy.** Every cookie-carrying state change must come from
Jav3's own origin. A TLS-terminating proxy that forwards the public `Host`
header (Caddy does by default; nginx with `proxy_set_header Host $host`) needs
no config: an `https://<public-host>` page reaching Jav3 over plain http is
accepted when host and port match. `X-Forwarded-*` is never trusted. If your
proxy rewrites `Host` (saves fail with "cross-origin request refused"), list
the public name — matched by hostname under any scheme and port:

```
JARVIS_CSRF_ALLOWED_HOSTS='["<public-host>"]'
```

Update loop: `git pull -q && (cd frontend && npm run build) && systemctl --user
restart jarvis` — check for in-flight agent work first, or use
`scripts/deploy_pi.sh`, which does that guarding for you.

## Dev

Backend: `uvicorn backend.main:app --reload` · Frontend: `cd frontend && npm run
dev` (proxies /api to :8000) · Tests: `pytest` (needs `JARVIS_DEEPSEEK_API_KEY` —
there are no model mocks, so full-flow tests hit the real API).

Config via env or `~/.config/jarvis/env`, prefix `JARVIS_` (see
`backend/config.py`). Notable flags: `JARVIS_VM_EGRESS` (monitored egress, off by
default → the guest is netless), `JARVIS_PEAK_WINDOWS` (peak-pricing gate),
`JARVIS_VOICE_ENABLED`.

## Third-party

Backups use [rclone](https://rclone.org) (MIT licence), run as a separate
program — see `NOTICE`.
