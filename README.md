# Jarvis v3

Personal agent: durable memory + project journals on the host, a ReAct loop over
DeepSeek behind a single `Model.complete` choke point, login'd web GUI.

The agent's **entire reasoning loop runs inside a disposable KVM guest** with no
API key, no database and no secrets, reachable only over AF_VSOCK. The host is a
thin supervisor: a model gateway (the only path to DeepSeek) and a tool broker
(the only path to tools). When monitored egress is on, the guest's only route
off-box is a host proxy that applies per-project policy, injects granted secrets
on the wire, meters every byte and can cut a host on an anomaly. See
`SECURITY-RESIDUAL-RISK.md` for what that does and does not buy.

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
- `clients/computeruse/` — the native client the operator runs on a desktop Jarvis
  drives (typed verbs only, never a command line)
- `docs/SELF.md` — the agent's own technical manual, served by the `self_docs` tool
- `scripts/` — Pi setup, systemd units, backup + image-rebuild timers, E2E smoke

## Deploy (test Pi)

```
git clone https://github.com/the-shadow-walker/Jarvis-but-its-secure.git ~/jarvis
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
`backend/config.py`). Notable flags: `JARVIS_USE_GUEST_LOOP`, `JARVIS_VM_EGRESS`,
`JARVIS_PEAK_WINDOWS`.
