# Jarvis v3

Personal agent: durable memory + project journals on the host, ReAct loop over
DeepSeek behind a single `Model.complete` choke point, login'd web GUI.
(The pass-2 execution sandbox — QEMU VM + monitored egress — was removed to
make room for a new execution architecture; see git history for the old one.)

## Layout

- `backend/` — FastAPI app: auth, memory assembly, projects, chat (SSE), agent loop
- `frontend/` — React (Vite) SPA, built to static and served by FastAPI
- `memory/` — soul.md / user.md / env.md / all-projects.md (runtime state, not committed)
- `projects/<slug>/` — project.md journal + code/ + notes/ (runtime state)
- `skills/` — SKILL.md files compiled into the tool registry
- `scripts/` — Pi setup + systemd user unit

## Deploy (test Pi)

```
git clone https://github.com/the-shadow-walker/Jarvis-but-its-secure.git ~/jarvis
cd ~/jarvis && bash scripts/setup_pi.sh
echo 'JARVIS_DEEPSEEK_API_KEY=sk-...' >> ~/.config/jarvis/env
.venv/bin/python -m backend.cli create-user <name>
systemctl --user restart jarvis     # GUI at http://<pi>:8000
```

## Dev

Backend: `uvicorn backend.main:app --reload` · Frontend: `cd frontend && npm run dev`
(proxies /api to :8000) · Tests: `pytest`

Config via env or `~/.config/jarvis/env`, prefix `JARVIS_` (see `backend/config.py`;
peak-pricing windows are `JARVIS_PEAK_WINDOWS`).
