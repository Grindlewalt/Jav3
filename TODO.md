# Jarvis v3 — TODO

Milestones done: M0-M7 complete.
- M0-M3: auth/GUI, memory+projects, model+peak+chat, sandbox VM
- **M4 — monitored execution**: tap network + nftables deny-by-default egress,
  logged DNS/DHCP, tcpdump capture, in-guest auditd exec log streamed to host,
  and the gate flow (lock → fresh boot → capture → run → analyze → staged
  report → operator approves). Verified live on the test Pi.
- M5 — git gate: git_status / git_diff / git_commit_request tools; host commits
  + pushes only after operator approval (`/api/projects/{slug}/git/*`).
- M6 — upload + crawl_codebase: zip-archive upload + deterministic codebase
  indexer + search_codebase into project notes.
- M7: funnel + live research.

Plus staging, tool layer, agents, schedules, web tools, backup, token
optimization, interactive HTML dashboards, research auto-approve, a Jobs view,
and a real ollama base_url override.

## Remaining M4 stretch (not yet done)
- [ ] move Jarvis's brain into the VM; host becomes a thin supervisor with a
      model proxy so the API key never enters the guest. (The monitored-
      execution slice is done; this is the larger "brain in the box" step.)

## Ideas / later
- [ ] gate flow: a GUI panel to review the pcap/exec-log report + approve in one
      place (report is staged today; approval reuses the staging approve flow).
- [ ] compaction for the generic orchestrator's subagents — currently bounded by
      subagent_max_iterations=8; add per-tool-result compaction only if leaves
      start doing heavy reads.
- [ ] auto-add approved research/pip destinations to the nft allowlist so a
      gated `pip install` can reach PyPI without a blanket open.
