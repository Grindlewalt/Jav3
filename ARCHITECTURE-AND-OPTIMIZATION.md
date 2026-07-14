# Jarvis v3 — How It Works, and How to Make It Smarter

A technical walkthrough of every subsystem, followed by concrete LLM-optimization
recommendations. File references are `path:line` against the tree as analyzed.

> **Status:** historical snapshot — file references are against the tree as it
> stood when analyzed. The optimization recommendations in §2-§7 and §8.1 (gate
> verdict feedback) were applied — see the "Token optimization pass" commit —
> EXCEPT §2.4 (pre-filtering the rules self-check pass), which was considered
> and rejected: the operator prefers the model pass to always run rather than
> adding client-side rule-pattern checks. **The entire sandbox/VM/egress layer
> (§8 and every vm/gate/egress reference) has since been REMOVED** in the
> sandbox-prune commit to make room for a new execution architecture.

---

## 1. The big picture

Jarvis is a personal agent with a deliberate trust split:

- **Host (the Pi)** holds everything durable and dangerous: memory files, project
  journals, the DeepSeek API key, the SQLite store, git, and all the guardrails.
- **Guest (QEMU VM)** is where untrusted execution happens: nukeable overlay disk,
  deny-by-default network, every syscall-level and packet-level observation done
  host-side so a compromised guest can't lie about what it did.
- **The model never acts directly.** Every file write goes to a `.staging/` queue,
  every commit is a *request*, every network destination needs operator approval.
  The LLM proposes; the human disposes.

Stack: FastAPI backend (`backend/`), React/Vite SPA (`frontend/`), SQLite (WAL) via
aiosqlite as the only store, one model provider (DeepSeek `deepseek-v4-flash` via the
OpenAI-compatible API, `backend/agent/model.py`), tools as folders under `tools/`.

---

## 2. The agent core (ReAct loop)

### How it works

**`backend/agent/loop.py` — `run_turn()`** is the heart. It's a native OpenAI-style
tool-calling loop:

1. Messages = `[system_prompt, *last-40-history]` (`chat.py:202-207`,
   `recent_message_limit = 40` in `config.py:52`).
2. Call `model.complete()` streaming; if the reply contains `tool_calls`, dispatch
   each through the registry, append the results as `role: "tool"` messages, loop.
3. Iteration cap: 40 for chat, 8 for orchestrator subagents (`config.py:50-51`).
   On the **last allowed iteration tools are stripped** (`loop.py:58`) so the model
   must answer from what it has — a nice guard against "one more tool call" death.
4. Stop conditions: no tool calls → final answer; `BudgetExceeded` → graceful stop;
   loop exhaustion → explicit "(stopped: hit the ReAct iteration limit)".

**Rule enforcement is a two-layer hack around a measured DeepSeek weakness**: tool
schemas pull attention off system-prompt rules (em-dash violations ~0% with no tools,
~65% with tools — measured, documented at `loop.py:41-46`). Mitigations:

- `standing_rules_tail()` re-appends operator rules to the *last user message* when
  tools are attached (halves violations to ~33%), and
- `_enforce_rules()` (`loop.py:110-132`) runs a **second, no-tools, temp-0 model
  call** over every final answer as a "strict copy editor."

**`backend/agent/model.py` — `Model.complete()`**: single choke point for all LLM
traffic. Always streams (with `include_usage` for the final usage chunk),
`max_tokens=4096`, temp 0.7 default. Peak-pricing gate (`config.py:42` windows,
409 + confirmation flow) runs before any network I/O. There's also a regex recovery
path for a DeepSeek serving quirk where tool calls come back as literal
`<｜｜DSML｜｜invoke ...>` text instead of structured fields (`model.py:35-43`).

**`backend/agent/budget.py`**: runs are bounded by **tokens, not loop count**. One
`Budget` object rides a contextvar set at the top of a chat turn or job; asyncio
children inherit it, so the cap is genuinely "across all agents in this operation."
Caps: 5M input / 1M output (`config.py:57-58`). It also tracks DeepSeek's
`prompt_cache_hit_tokens` to confirm the prefix-cache assumption that makes the
generous input cap survivable.

### Optimization opportunities

1. **Tool results enter context untruncated — the #1 leak.** `loop.py:104` appends
   the *full* tool result to messages; the `result[:10000]` at `loop.py:102` only
   truncates the DB copy. `read_file` can return 100,000 chars
   (`tools/read_file/handler.py:4`), `run_code` returns stdout[-8000:]+stderr[-4000:],
   and every one of those rides *every subsequent iteration* of the loop. This is the
   exact quadratic blow-up `read_and_summarize` was built to avoid — but only web
   pages got the fix. **Fix:** cap what enters `messages` (e.g. 8-12k chars with a
   "truncated, re-read a narrower range" notice), and/or add a summarize-in-tool path
   for big file reads like the web one.

2. **Old tool results are never evicted.** Once iteration N's result is in the
   message list it's re-sent verbatim for iterations N+1..40. A cheap, high-yield
   pattern: after each iteration, replace tool-result messages older than the last
   2-3 with a stub (`"[result of read_file(x.py) — 41KB, superseded; re-call if
   needed]"`). DeepSeek's prefix cache softens the *cost* of re-sending, but it does
   nothing for the *attention* problem — a 4-page grep dump from iteration 2 still
   competes with the actual task at iteration 15. The Logs UI already flags results
   >4000 bytes as "hot" (`frontend/src/pages/Logs.jsx`) — the loop should act on the
   same signal, not just display it.

3. **No conversation compaction — just a cliff at 40 messages.** `chat.py:204` takes
   the last 40 messages and silently drops the head; there's no rolling summary. Long
   working sessions lose early decisions with no trace. **Fix:** when history exceeds
   the window, summarize the evicted half into a single system-adjacent "earlier in
   this conversation" message (one cheap model call, amortized, and it keeps the
   stable prefix property the cache relies on if you re-summarize infrequently).

4. **The self-check pass fires even when nothing needs fixing.** `_enforce_rules`
   re-generates the entire answer at temp 0 on *every* rule-bearing tool turn. Cheap
   pre-filter: run the regex/keyword scan for rule violations client-side first
   (the rules are already extracted line-by-line by `standing_rules_tail`, and the
   em-dash case is literally a `"—" in content` check) and only invoke the editor
   call on a hit. Saves a full round-trip on the ~35-100% of turns that are already
   clean.

5. **No retry/backoff in `Model.complete`.** One transient 5xx or mid-stream drop
   aborts the whole turn and throws away every token spent on it (`model.py:149-158`).
   A single retry with jitter on connect errors/5xx (idempotent — nothing has been
   committed yet) is the highest-value reliability fix in the file.

6. **Rules are sent up to 4× per turn** (standing-memory block, operator-rules tail,
   last-user-message restatement, self-check prompt). The sandwich is empirically
   justified, but the *full* notes block and the *extracted* rules tail overlap —
   consider dropping rule-bearing lines from the standing-memory block since the
   tail already carries them.

---

## 3. Memory & context assembly

### How it works

**`backend/memory.py` — `assemble_system_prompt()`** (`memory.py:263-304`) builds the
system prompt fresh every turn, in this order (the "task sandwich"):

| Block | Source | Cap |
|---|---|---|
| soul | `memory/soul.md` (persona charter) | none |
| standing-memory | `memory/notes/*.md`, prefs sorted first | **2000 tokens** (`memory.py:174`), overflow degrades to a name-only index |
| user | `memory/user.md` | none |
| env | `memory/env.md` | none |
| all-projects | auto-generated rollup (first para of each project's `## Summary`) | none |
| agents-index | `agents/*/AGENT.md` roster | none |
| active-project | **full `project.md` + full text of every operator-ticked context file** | **none** |
| operator-rules | extracted rule lines, always last, never excludable | small |

`standing_rules_tail()` (`memory.py:214-257`) greps notes named `*pref*`/`*rule*` for
behavioral lines (never/always/avoid/must/...), rewrites "pet peeve: X" → "Avoid X",
with one hardcoded em-dash special case.

**Projects** live at `projects/<slug>/` with a `project.md` journal (Summary/Status/
Issues/Journal sections), `code/`, `notes/`, `todo.md`. `journal_update` appends dated
bullets and refreshes the all-projects rollup. `load_project` sets active-project
state; its tool return is capped at 4000 chars but the *context injection* of the same
file is uncapped — inconsistent.

**Staging** (`backend/staging.py`): every agent file write lands in
`projects/<slug>/.staging/<relpath>` (exec bits stripped, `.git`/`.staging` protected).
`effective_read()` overlays staged-over-canonical so the agent sees its own pending
edits. Operator approves/rejects per file in the dashboard; approve copies staged →
canonical. Staged bytes are inert by contract — never executed or rendered.

### Optimization opportunities

1. **The active-project block is the biggest uncapped context surface.**
   `_loaded_context_files()` (`memory.py:307-321`) inlines every ticked file in full,
   fenced, into *every* turn's system prompt — a ticked 100KB file is a permanent
   25k-token tax. The GUI computes per-file token estimates (`workspace.py:313-328`)
   but nothing enforces a ceiling. **Fix:** enforce a total context-files budget at
   assembly time (reuse `MEMORY_CONTEXT_BUDGET` logic — include files up to N tokens,
   degrade the rest to a path+size index the model can `read_file` on demand).

2. **soul/user/env/all-projects have no cap either.** Fine today while they're
   stubs, but `all-projects.md` grows linearly with project count and is re-sent on
   every turn of every conversation, project-relevant or not. Same budget-then-index
   treatment applies.

3. **Retrieval is exact-name-or-nothing.** Overflow memory notes degrade to a name
   index (`memory.py:203-210`) that the model must recall by exact name; codebase
   search is literal substring/regex. There are no embeddings anywhere. You don't
   necessarily need a vector DB on a Pi — two cheaper wins first:
   - give `memory_read` a keyword-match mode over note *contents*, not just names;
   - have `memory_write` maintain a one-line description per note so the overflow
     index is `name — what's in it` instead of bare names.

4. **Stale summaries have no detector.** `all-projects.md` only refreshes on explicit
   mutations; the codebase index refreshes only when the model decides to re-run
   `crawl_codebase`. Cheap fix: `crawl_codebase` records the max mtime it saw; any
   `search_codebase` call compares against the current tree's max mtime and prefixes
   results with "index is stale (N files changed since build) — consider re-crawling."
   That turns an invisible failure into a visible one for the model.

---

## 4. Tools, skills, and the registry

### How it works

A tool is a folder: `tools/<name>/TOOL.md` (YAML frontmatter: name, description,
when_to_use, enabled) + `handler.py` exposing `async def run(**args) -> str`.
`compile_registry()` (`backend/agent/tools/registry.py:85-101`) scans `tools/`,
legacy defs, and `skills/*/SKILL.md` (skills compile into the same registry) into
`data/registry.json` at startup. Handlers hot-reload by mtime; **specs don't** — an
edited TOOL.md isn't seen until restart or a skill save triggers recompile.

`openai_tool_specs()` builds each description as
`description + " Use when: " + when_to_use + "\nNotes: " + body[:500]`
(`registry.py:120-123`). All 23 tools are enabled, so central chat ships all 23 full
specs every turn. Dispatch never crashes the loop — handler exceptions come back as
`error: ...` strings the model can read and react to.

### Optimization opportunities

1. **~11.5KB+ of tool specs on every single turn**, including turns that are plainly
   conversational. Two options, both compatible with prefix caching if done right:
   - *Static trim:* the 500-char body slice is mostly TOOL.md prose written for
     humans; move usage detail out of the spec and into the tool's *first error
     message* (the model reads errors well). Aim for ≤200 chars per description.
   - *Dynamic subsetting:* no project loaded → drop the 8 project-workspace tools;
     no VM configured → drop run_gated/run_command. Keep the subset stable within a
     conversation so the cached prefix survives.

2. **Registry staleness footgun**: mtime-check the TOOL.md files in `load_registry()`
   the same way `_load_dynamic` already does for handlers — one `stat` per tool per
   startup-cache read is free.

---

## 5. Subagents & orchestration — two systems, one wired

### How it works

**(a) `spawn_agent` → `run_agent_headless`** (`backend/agents_run.py:89-114`) — the
live path. Agent defs are `agents/<slug>/AGENT.md` (prompt + exclusion lists for
context/tools/skills). A spawned agent gets `agent prompt + full central context
minus exclusions`, runs the normal loop, and its **entire final message returns raw**
as the parent's tool result. `spawn_agent` is always excluded from agents' own tool
sets — no recursion, no fork bombs. Agents can override model/base_url (ollama).

**(b) The M7 "context funnel" orchestrator** (`backend/orchestrator.py`) — the good
architecture, currently **dead code**: `run_job` (`orchestrator.py:224`) is never
called by anything. Design: head → leaders → subagents (MAX_DEPTH=2, MAX_NODES=24,
MAX_FANOUT=6), a planner LLM call decides decompose-vs-direct, subagents get a
2-line prompt + parent brief instead of full context, are capped at 8 iterations,
and results roll *up* through per-node LLM summarization (`_rollup`, output
truncated to 6000 chars) instead of raw dumps.

**Research** (`backend/research.py`) is the one funnel that shipped, rewritten as a
deterministic pipeline after the ReAct version "burned 5M tokens re-sending a
snowballing context": generate ≤8 queries → parallel SearXNG search → LLM filter
assigns ≤4 URLs to 2-4 parallel readers → each page immediately compressed to 3-5
bullets → one synthesis call → rules pass → doc staged (auto-approved by default,
`config.py:63`). Raw pages never accumulate in any context.

**Schedules** (`backend/schedules.py`): 60s poll loop, daily/interval cadences
(15-min floor), runs due jobs sequentially, headless runs auto-confirm peak pricing.
**Bus** (`backend/bus.py`): in-process asyncio pub/sub for job SSE; drops oldest on
overflow; jobs survive client disconnects.

### Optimization opportunities

1. **The wired subagent path has none of the orchestrator's discipline.** Spawned
   agents get the *full* central context (soul+memory+projects+...) even for narrow
   tasks, run at the full 40-iteration cap, and return uncompacted output that then
   snowballs in the parent's loop. Port three things over from `orchestrator.py`:
   default `subagent_max_iterations=8` for spawned agents, an optional
   `context: minimal` mode in AGENT.md frontmatter (task brief instead of full
   context), and a rollup summarization of any agent report over ~4KB before it
   enters the parent's messages.

2. **Decide the orchestrator's fate.** Either wire `run_job` to a tool/API (the
   TODO already flags subagent compaction as the missing piece) or delete it —
   right now it's 250 lines of the best context-management patterns in the repo,
   testing green and reachable by nothing.

3. **Reader page-summarization is serial within each reader**
   (`research.py:180-186` loops URLs one at a time; only readers parallelize).
   `asyncio.gather` over the ≤4 pages per reader cuts research wall-clock roughly
   in half for the read phase — the summarize calls are independent.

4. **Schedules run sequentially** (`schedules.py:197-198`); a slow agent job pushes
   later jobs past their slot. Low priority at homelab scale, but `gather` with a
   small semaphore is a five-line change.

---

## 6. Web tools

### How it works

- **`web_search`**: SearXNG JSON → compact text list, ≤8 results, snippets ≤300
  chars, flags already-fetched URLs so the model diversifies sources.
- **`web_read`**: SSRF-guarded (scheme whitelist, resolves host, rejects non-global
  IPs, **re-checks after redirects**) → streams ≤2MB → strips to inert plain text
  (script/style/nav/form removed) → truncates to `web_max_chars = 6000` (reduced
  from 20k specifically to cut token throughput).
- **`read_and_summarize`**: fetches ≤8 URLs concurrently and compresses each to 3-6
  bullets *inside the tool* — full page text costs tokens exactly once and never
  enters the ReAct loop. This is the repo's flagship context-discipline pattern.
- A fetch ledger (`fetched_urls`, atomic claim-before-fetch) prevents duplicate
  reads across parallel agents; ephemeral/incognito mode bypasses it.

### Optimization opportunities

Mostly already good — this is the best-optimized subsystem. Two small ones:

1. `web_read`'s 6000-char hard truncation is position-blind: page text past the cap
   is simply gone, and lots of pages front-load nav junk even after stripping. For
   pages over the cap, keeping head + tail (or letting the model pass a `section`
   hint) beats a pure head slice.
2. `read_and_summarize`'s bullets are lossy with no recovery pointer. Have it stash
   the full extracted text under `notes/web-cache/<hash>.txt` (staged or ephemeral)
   and cite the path in the summary, so the model can drill into one source without
   a re-fetch.

---

## 7. Codebase indexing & search

### How it works

**`crawl_codebase` → `build_index()`** (`backend/codeindex.py`): deterministic, no
LLM. Walks `code/` (skips binaries, >512KB files, dot-dirs), regex-extracts symbols
(Python/JS/TS/Go), writes `notes/codebase/INDEX.md` (one line per file, first 3
symbols, ≤1500 entries) plus one detail file per top-level dir. Written canonically,
not staged (derived data).

**`search_codebase` → `search_code()`**: literal case-insensitive substring or regex
over the live tree. ≤50 hits, each line stripped to 200 chars, first-match
file-sorted order. No ranking, no context lines, no grouping.

### Optimization opportunities

1. **Add 1-2 context lines around matches.** A bare 200-char line routinely forces a
   follow-up `read_file` of the whole file — the most expensive possible next step
   (see §2.1). Grep-with-context usually makes the read unnecessary.
2. **Group hits by file and rank files by hit count** instead of returning the first
   50 in path order. For a common term, path order returns 50 arbitrary lines from
   whatever sorts first alphabetically and tells the model to "narrow the query" —
   hit-count ranking answers "where does this concept live" directly.
3. **Symbol-aware search mode**: the index already has every symbol per file; a
   `kind: symbol` search against the index (instead of the raw tree) would resolve
   "where is function X defined" in one cheap, tiny result.

---

## 8. Execution sandbox & the gate

### How it works

**VM**: golden Debian-13 arm64 cloud image built once (`vm/build_base.sh`), SHA512
verified, base qcow2 chmod 444. Runtime boots a **throwaway overlay** on the RO base
(`vm/run_vm.sh`); "nuke" = delete the overlay + EFI vars and reboot clean. In-guest
auditd logs `execve` and streams it out over a virtio serial console to a host-side
append-mode file that survives nukes. SSH host key is pinned at image-build time; a
key mismatch is treated as possible compromise, never retried.

**Network**: dedicated tap (`jvtap0`), nftables table with **deny-by-default egress**
(allowlist sets empty on boot, re-programmed from the DB at app startup), dnsmasq
with full query logging (guest can *resolve* anything, *connect* nowhere unapproved),
tcpdump per gated run.

**`run_gated`** (`backend/gate.py:353-450`): lock-check → optional fresh-boot nuke →
record log offsets + start pcap → push merged (canonical+staged) tree → run → pull →
stage all changed files → stop pcap → parse DNS/audit/kernel-drop/pcap slices →
`evidence.json` (no verdict — raw facts only) → markdown report staged for review.

**Classification** (`backend/sandbox.py`): deliberately **rule-based, no LLM** — a
compromised agent's output strings can't steer the verdict. Worst-signal wins:
external beacon → crit; sensitive/exec signals; new-blocked-destination; clean.
Approving a destination writes a persistent rule *and* programs the live nft set;
verdicts re-compute against the current allowlist on every read.

**Beacon catcher** (`vm/render/render.js` via `backend/render.py`): agent-built HTML
is rendered in jsdom *inside the VM* with fetch/XHR/WebSocket/sendBeacon shimmed —
every call is logged, fired for real so the nftables tap drops it as ground truth,
and answered with a fake 200 so nothing exfiltrates. Any external beacon forces a
crit verdict.

**Git gate** (`backend/gitgate.py`): the agent can only file `git_commit_request`
rows; the host runs git (argv-only, env-stripped, 30s timeout) after operator
approval, and pushes only if a remote is configured.

### Optimization opportunities

1. **Close the feedback loop to the agent — the single biggest "smarter agent" win
   in the repo.** The rich evidence + `classify()` verdict/headline reach only the
   human console; the `run_gated` *tool* returns bare counts (`gate.py:444-450`,
   `tools/run_gated/handler.py:17-28`). The agent literally cannot see "your run
   tried to beacon to evil.com" or "blocked: 3 attempts to 1.2.3.4:443", so it can't
   fix its own code, choose a different mirror, or flag its own output as suspicious.
   Running `classify()` at the end of `run_gated` and returning
   `verdict/rule/headline + blocked destinations (host:port)` is safe — it's
   deterministic, derived from host-side observation, and gives the model exactly
   the structured signal it needs to self-correct in-loop. Approval authority stays
   with the human either way.

2. **Sensitive-read detection is argv-only.** Audit rules log only `execve`
   (`build_base.sh:87`), and `evidence["sensitive"]` is always `[]`
   (`gate.py:346`) — a script that opens `~/.aws/credentials` without naming it on a
   command line is invisible, while the console UI labels the section "auditd
   path-watch." Add `-w <path> -p r` watches for the sensitive globs in
   `config.py:104-108` to make the label true.

3. **Egress lock is advisory** — `egress_locked()` failing produces a report WARNING
   but the run proceeds unmonitored-in-effect (`gate.py:212-213,382`). For a run
   whose whole point is "deny-by-default was on," refuse to start instead.

4. **`run_command` is an unmonitored side door**: same VM, no pcap/DNS/audit slice,
   no report. Fine for `ls`, but nothing steers network-touching commands to the
   gated path. Cheapest fix is prompt-side (TOOL.md: "anything that may touch the
   network must use run_gated"); stronger is a host-side check that diffs the
   drop-log counters across every run_command and warns when a supposedly-local
   command generated egress attempts.

5. **The TODO's "brain in the box" item** (move the loop into the VM behind a model
   proxy) would also fix the subtler issue that today's host-side loop handles
   attacker-controlled *text* (tool results from the guest) in the same process that
   holds the API key and the approval endpoints.

---

## 9. Observability

`usage_log` records per-turn input/output tokens and DeepSeek cache hit/miss; the
Logs UI is explicitly a token-blowup debugger — per-conversation tiles (tokens, tool
calls, result bytes, cache-hit %), "heavy" flags at 500k tokens / 30 calls, and a
per-result "hot" flag at 4000 bytes with the exact right explanation ("a result this
big is re-sent every ReAct iteration"). The diagnosis layer is ahead of the
enforcement layer: almost every threshold the UI *displays* is one the loop could
*act on* (§2.1-2.2).

---

## 10. Priority-ordered summary

If you do only five things:

1. **Cap and evict tool results in the loop** (§2.1, §2.2). Truncate what enters
   `messages`, stub out stale results after 2-3 iterations. This attacks the only
   quadratic cost in the system and helps attention as much as cost.
2. **Return the gate verdict to the agent** (§8.1). One function call moved earlier
   in the pipeline turns the sandbox from a pure human console into a feedback
   signal the agent can learn from within a single task.
3. **Budget the active-project context block** (§3.1). It's the only unbounded
   every-turn context surface an operator can accidentally make huge.
4. **Discipline `spawn_agent`** (§5.1): 8-iteration default, optional minimal
   context, rollup-summarize big reports. The patterns already exist in
   `orchestrator.py` — port or wire them.
5. **Add retry-with-backoff to `Model.complete`** (§2.5). Cheapest reliability win;
   everything else in the system assumes the model call succeeds.

Honorable mentions: pre-filter before the `_enforce_rules` second pass (§2.4), trim
tool specs / subset by context (§4.1), grep-with-context + ranking in
`search_codebase` (§7), parallel page summaries in research readers (§5.3),
staleness detection for the codebase index (§3.4).

The repo's own history shows the playbook works: research was rewritten from a
snowballing ReAct loop into a compress-at-the-edge pipeline after a 5M-token burn,
and `read_and_summarize`/`web_max_chars` exist for the same reason. The remaining
optimizations are mostly *applying that same pattern* — spend big context once at
the edge, let only compact summaries ride the loop — to file reads, subagent
reports, and gate evidence.
