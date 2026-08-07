# Jarvis v3

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
- `clients/computeruse/` — the native client the operator runs on a desktop Jarvis
  drives (typed verbs only, never a command line)
- `docs/SELF.md` — the agent's own technical manual, served by the `self_docs` tool
- `scripts/` — Pi setup, systemd units, backup + image-rebuild timers, E2E smoke

## Deploy (test Pi)

```
git clone https://github.com/grindlewalt/jav3.git ~/jarvis
cd ~/jarvis && bash scripts/setup_pi.sh
echo 'JARVIS_DEEPSEEK_API_KEY=sk-...' >> ~/.config/jarvis/env
.venv/bin/python -m backend.cli create-user <name>
systemctl --user restart jarvis     # GUI at http://<pi>:8000
```

Update loop: `git pull -q && (cd frontend && npm run build) && systemctl --user
restart jarvis` — check for in-flight agent work first.

## Dev

Backend: `uvicorn backend.main:app --reload` · Frontend: `cd frontend && npm run
dev` (proxies /api to :8000) · Tests: `pytest` (needs `JARVIS_DEEPSEEK_API_KEY` —
there are no model mocks, so full-flow tests hit the real API).

Config via env or `~/.config/jarvis/env`, prefix `JARVIS_` (see
`backend/config.py`). Notable flags: `JARVIS_VM_EGRESS` (monitored egress, off by
default → the guest is netless), `JARVIS_PEAK_WINDOWS` (peak-pricing gate),
`JARVIS_VOICE_ENABLED`.
