# Jarvis v3 — TODO

Milestones done: M0-M7 complete.
- M0-M2: auth/GUI, memory+projects, model+peak+chat
- M3/M4 (sandbox VM + monitored execution + egress control): built, verified
  live on the test Pi, then **removed** in the sandbox-prune commit to clear
  the way for a new execution architecture. Recover from git history if needed.
- M5 — git gate: git_status / git_diff / git_commit_request tools; host commits
  + pushes only after operator approval (`/api/projects/{slug}/git/*`).
- M6 — upload + crawl_codebase: zip-archive upload + deterministic codebase
  indexer + search_codebase into project notes.
- M7: funnel + live research.

Plus staging, tool layer, agents, schedules, web tools, backup, token
optimization, interactive HTML dashboards, research auto-approve, a Jobs view,
and a real ollama base_url override.

## Next
- [ ] new execution architecture (replaces the removed VM sandbox / gate /
      egress layer) — design TBD by the operator.

## Ideas / later
- [ ] GUI panel for the generic funnel (POST /api/runs/funnel is live; the Jobs
      view already lists and streams its heads).
- [ ] compaction for the generic orchestrator's subagents — currently bounded by
      subagent_max_iterations=8; add per-tool-result compaction only if leaves
      start doing heavy reads.
