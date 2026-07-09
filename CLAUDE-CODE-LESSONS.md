# What Claude Code's Source Teaches Us, and How to Apply It to Jarvis

A deep read of the leaked/reconstructed Claude Code source in
`collection-claude-code-source-code/` (the real TypeScript under
`original-source-code/src/`, plus the clean ~16k-line Python port `clawspring/`),
turned into concrete changes for **this** agent. Every recommendation cites the
Claude Code source it comes from *and* the Jarvis file it applies to.

How to read this: Part 1 is how Claude Code actually works. Parts 2–5 are the
four things you asked about — system prompts, architecture, context
optimization, and everything else — each structured as *what they do → what
Jarvis does → what to change*. Part 6 is a prioritized punch list.

A note on where Jarvis already stands: the token-optimization pass we just did
(tool-result cap + eviction, project-context budget, subagent discipline, model
retry) turns out to mirror several of Claude Code's core patterns — so a good
chunk of this doc is "you're on the right track, here's the next 20%," not
"start over."

---

## Part 1 — How Claude Code works (the shape)

**One loop, streaming tool execution.** A turn is: assemble system prompt →
stream the model → if it emitted no tool calls, done → otherwise execute the
tool calls, append results, loop. The real version (`query.ts` +
`services/tools/StreamingToolExecutor.ts`) executes tools *as they stream in*
and runs read-only tools in parallel; the Python port (`clawspring/agent.py`,
~180 lines) does it serially. Jarvis's `backend/agent/loop.py` is the serial
shape.

**Tools are a rich interface, not just name+handler** (`Tool.ts:362-695`). Each
tool declares `isReadOnly`, `isConcurrencySafe`, `isDestructive`,
`checkPermissions`, `validateInput`, a `maxResultSizeChars`, and *two* render
paths — one for the model (`mapToolResultToToolResultBlockParam`) and one for
the human (`renderToolResultMessage`). Defaults are **fail-closed**: a tool is
assumed to write and to be concurrency-unsafe unless it opts in
(`TOOL_DEFAULTS`, `Tool.ts:757`).

**Context is actively managed in three tiers.** (1) Per-tool output caps at the
*source* before results ever enter history. (2) "Microcompaction" — evicting old
read-type tool results and replacing them with a sentinel. (3) Full
"auto-compact" — a model call that summarizes the old conversation into a
structured brief and continues. All three trigger off an *effective* context
window (raw window minus reserved output room minus a safety buffer), not the
raw window.

**Memory is two systems.** (a) `CLAUDE.md` instruction files — human-authored,
always loaded, discovered up the directory tree. (b) "Auto-memory" (`memdir/`) —
model-authored facts, one markdown file per memory with YAML frontmatter, where
**only a `MEMORY.md` index is loaded into context** and full memories are pulled
on demand. A periodic "dream" pass consolidates and prunes them.

**Skills are prompt templates with progressive disclosure.** A skill is a
`SKILL.md` (markdown + frontmatter). Only its *name + description + when_to_use*
sits in context (budgeted to ~1% of the window); the full body loads only when
the skill is invoked. Slash commands are just skills.

**Subagents are the same loop, recursively.** The Agent tool is read-only and
concurrency-safe (so many run in parallel), each gets a filtered tool set and
its own context, and returns *only its final message* plus a `<usage>` trailer
to the parent.

---

## Part 2 — System prompts

### 2a. The single biggest structural idea: a static/dynamic cache boundary

Claude Code builds the system prompt as an **array of titled sections**, and
splits it with a literal marker (`SYSTEM_PROMPT_DYNAMIC_BOUNDARY`,
`prompts.ts:105-115`): everything *before* is invariant instruction text that
can be cached across turns (even across orgs); everything *after* is
session-specific (cwd, git, env, memory). Sections are individually memoized —
`systemPromptSection(name, compute)` caches until `/clear` or `/compact`, and a
cache-*breaking* section must use `DANGEROUS_uncachedSystemPromptSection(name,
compute, reason)` and **supply a written reason** (`systemPromptSections.ts`).

The order is load-bearing (`prompts.ts:560-576`): persona → system mechanics →
task behavior → safety → tool-use → tone → brevity → **[boundary]** → memory →
env → session guidance.

**What Jarvis does:** `backend/memory.py:assemble_system_prompt` rebuilds the
*entire* prompt every turn and joins blocks with `\n\n---\n\n`, in this order:
soul → standing-memory (notes) → user → env → all-projects → agents-index →
active-project → operator-rules. The problem: volatile content
(`all-projects.md` auto-regenerates, `active-project` changes, memory notes
change) is interleaved with static content (`soul.md`) from the top. On DeepSeek,
which caches prompt prefixes automatically, **any change high in the prompt
busts the cache for everything below it.**

**Change for Jarvis:** reorder `assemble_system_prompt` into a stable prefix and
a volatile suffix.

- *Stable prefix* (rarely changes → stays cached): `soul.md`, a new behavior/
  tone/tool-use section (see 2b), and the operator rules text.
- *Volatile suffix* (changes turn to turn): standing-memory notes, `user.md`/
  `env.md` (edited occasionally), `all-projects.md`, `agents-index`,
  `active-project`.

You don't need Anthropic's cache-scope machinery — just *ordering* the array so
mutable blocks come last gets you most of the win on DeepSeek's prefix cache.
Keep the operator-rules "task sandwich" tail (that's a deliberate recency trick
and it's correct), but move the *bulk* static instruction text to the very top
so the cached prefix is as long as possible.

### 2b. Prompt-engineering techniques worth lifting into `soul.md`

Jarvis's `soul.md` is a thin persona stub. Claude Code's behavioral bank is
where most of its "feel" comes from. The most transferable pieces, quoted so you
can adapt them:

**Rules as concrete negatives + a memorable maxim** beats abstract positives
(`prompts.ts:201`):
> "Don't add features, refactor code, or make 'improvements' beyond what was
> asked. A bug fix doesn't need surrounding code cleaned up… Three similar lines
> of code is better than a premature abstraction."

**Reversibility / blast-radius doctrine** — Jarvis has a whole staging+gate
system for this but says nothing to the *model* about it (`prompts.ts:258`):
> "Carefully consider the reversibility and blast radius of actions… A user
> approving an action (like a git push) once does NOT mean that they approve it
> in all contexts… Authorization stands for the scope specified, not beyond."
> …followed by an explicit risky-action taxonomy (destructive / hard-to-reverse
> / visible-to-others / third-party-upload) and the maxim "measure twice, cut
> once."

**Faithful reporting, both directions** (`prompts.ts:240`) — directly relevant
since Jarvis runs headless agents whose reports you can't watch:
> "Never claim 'all tests pass' when output shows failures… Equally, when a
> check did pass… state it plainly — do not hedge confirmed results with
> unnecessary disclaimers. The goal is an accurate report, not a defensive one."

**Diagnose before retry** (`prompts.ts:233`): "If an approach fails, diagnose
why before switching tactics… Don't retry the identical action blindly, but
don't abandon a viable approach after a single failure either."

**Brevity reframed as reader comprehension, not terseness** (`prompts.ts:405`) —
this is a much better instruction than "be concise":
> "What's most important is the reader understanding your output without mental
> overhead or follow-ups, not how terse you are. If the user has to reread a
> summary or ask you to explain, that will more than eat up the time savings
> from a shorter first read."

**Numeric length anchors** beat qualitative ones (`prompts.ts:531`, code comment
claims ~1.2% output-token reduction): "keep text between tool calls to ≤25
words. Keep final responses to ≤100 words unless the task requires more detail."

**Small high-value micro-rules** (`prompts.ts:430`): reference code as
`file_path:line_number`; only use emojis if asked; **"Do not use a colon before
tool calls"** — because "Let me read the file:" followed by a suppressed tool
call reads as a broken sentence.

**Marker discipline:** reserve `IMPORTANT:` / `NEVER` / `CRITICAL` (caps) for
true absolutes. If everything is IMPORTANT, nothing is.

**Change for Jarvis:** expand `soul.md` (or add a `# Behavior` block in the
stable prefix) with adapted versions of the above. Note Jarvis's operator memory
already carries an em-dash rule — this is the same idea (negative examples beat
bare prohibitions), so the machinery is there; it just needs the broader
behavioral bank.

### 2c. Dynamic context: keep it small and cap it

The env block is a tight XML `<env>` snippet (cwd, is-git, platform, OS, model,
knowledge cutoff) — `prompts.ts:606`. Git status, when injected, is **capped at
2000 chars** with a "run it yourself for more" note (`context.ts:20`). Jarvis's
env injection is fine; the lesson is the *cap-and-point* pattern, which you now
apply to project context files (good) and should apply anywhere you inline
host state.

---

## Part 3 — Context optimization (the highest-value section)

This is where Claude Code is most sophisticated and where Jarvis has the most
headroom. Three layers, cheapest first.

### 3a. Cap tool output at the SOURCE, and throw rather than truncate

Claude Code caps each tool's output *before it enters history*: file read ≈25k
tokens / 256KB, Bash 30k chars (150k hard), Grep 250 lines, Glob 100 files
(`FileReadTool/limits.ts`, `outputLimits.ts`, `GrepTool.ts:108`,
`GlobTool.ts:157`). Crucially, file-read **throws a ~100-byte error** when a file
is too big instead of returning 25k tokens of truncated content — the comment
(`limits.ts:9-13`) says they tested truncation and reverted, because "the throw
path yields a ~100-byte error tool-result while truncation yields ~25K tokens of
content at the cap." A tiny error also nudges the model to narrow its next call.

**What Jarvis does:** `tools/read_file/handler.py` caps at **100,000 chars** and
returns the truncated content (~25k tokens!). Our recent loop-level cap
(`tool_result_max_chars=12000`) catches this downstream, but that's a backstop,
not the fix.

**Change for Jarvis:**
1. Give `read_file` `offset` and `limit` parameters (like Claude Code's Read),
   and lower its hard cap. On overflow, return a short error — "file is N lines;
   read a range with offset/limit" — not truncated text.
2. Do the same audit on `run_command`/`run_code` (already tail-capped — good)
   and `search_codebase` (already 50 hits — good, and we just added grouping).
3. Consider adopting the "persist big results to disk, hand the model a preview +
   path" pattern (`Tool.ts:456`) for genuinely large outputs — Jarvis already
   has `notes/` and staging to stash them in.

### 3b. Two-tier compaction — you have tier 1, you're missing tier 2

Claude Code runs **microcompaction** (evict old read-type tool results, replace
with the sentinel `[Old tool result content cleared]`, always keep the last N,
never evict edits/writes — `microCompact.ts:36,41-50,458`) *before* it runs
**full auto-compact** (a model call that summarizes the old conversation and
continues). It triggers off an *effective* window: `rawWindow − 20k reserved
output − 13k buffer` (`autoCompact.ts:30-91`), not the raw window.

**What Jarvis does:** we just added tier-1 eviction in `loop.py`
(`_evict_stale_results` stubs out >4k-char results older than 2 rounds — this is
exactly microcompaction). But Jarvis has **no tier 2**: conversation history is a
flat `recent_message_limit = 40` cliff (`chat.py:204`) that silently drops the
oldest messages with no summary.

**Changes for Jarvis, in order:**

1. **Tell the model about the eviction you already do.** Claude Code pairs its
   clearing with two system-prompt lines, and this is a near-free win that makes
   our eviction change strictly better:
   > `# Function Result Clearing` — "Old tool results will be automatically
   > cleared from context to free up space. The N most recent results are always
   > kept." (`prompts.ts:836`)
   > "When working with tool results, write down any important information you
   > might need later in your response, as the original tool result may be
   > cleared later." (`prompts.ts:841`)

   Add these two lines to Jarvis's system prompt (the stable prefix). Right now
   we evict silently, so the model can be surprised when a result vanishes;
   telling it to note what it needs first fixes that.

2. **Add real conversation compaction (tier 2).** When the history estimate
   exceeds a threshold, summarize the older portion with one text-only model
   call and keep the recent tail verbatim. The clawspring version is the minimal
   template (`compaction.py:110-165`): find a split keeping ~30% of tokens as
   "recent," summarize the rest, return `[summary_msg, ack_msg, *recent]`. The
   summary *prompt* is what matters — Claude Code forces a fixed structure
   (`compact.ts` / `prompt.ts`): primary request & intent, key files + code
   snippets, errors & fixes, **all user messages**, pending tasks, current work,
   and a **verbatim** next-step quote to prevent drift. Two details that matter:
   - Make the summarizer **text-only** and say so ("Tool calls will be REJECTED
     and waste your only turn," `prompt.ts:19`), or a stray tool call wastes it.
   - Re-enter with "resume directly — do not acknowledge the summary, do not
     recap" (`prompt.ts:355`) so the model doesn't waste a turn re-orienting.

   This is the single biggest context upgrade available to Jarvis. It plugs into
   `chat.py` right where the 40-message window is applied.

3. **Trigger on an effective window.** Jarvis's op budget is a flat 5M/1M token
   cap. For compaction, compute `effective = context_window − reserved_output −
   buffer` and compact against that (`autoCompact.ts`), rather than a raw
   message count. `estimate_tokens` (chars/4, already in `memory.py`) is the
   right cheap estimator — Claude Code uses the same, with chars/2 for dense
   JSON.

4. **Circuit-break doomed compaction** (`autoCompact.ts:70`): stop after ~3
   consecutive failures so an irrecoverably-over-limit session doesn't hammer the
   API.

### 3c. Pin edits, evict reads

The rule threaded through all of Claude Code's compaction: **read-type results
are disposable, mutating results are load-bearing.** Only reads/searches/shell go
in `COMPACTABLE_TOOLS`; FileEdit/FileWrite/NotebookEdit are excluded from
clearing (`apiMicrocompact.ts:28`). Jarvis's eviction currently keys off *size +
age* only. Refine `_evict_stale_results` to never evict results from
`write_file`/`edit_file`/`journal_update`/`memory_write` — those are the
model's record of what it changed.

---

## Part 4 — Architecture, tools, and subagents

### 4a. Give tools read-only / concurrency metadata, and run reads in parallel

Claude Code's whole concurrency story is two booleans: `isReadOnly` and
`isConcurrencySafe` (which usually just returns `isReadOnly`). The executor runs
any number of concurrency-safe tools together and forces an unsafe tool to run
alone (`StreamingToolExecutor.ts:129`). Defaults fail closed.

**What Jarvis does:** `loop.py` executes tool calls **strictly serially** (`for
tc in final["tool_calls"]`), and TOOL.md has no read-only/concurrency metadata.
When the model asks for three `read_file`s or a `search_codebase` + two reads in
one turn, Jarvis runs them one after another.

**Change for Jarvis:**
1. Add `read_only: true` / `concurrency_safe: true` frontmatter to the TOOL.md
   files that qualify (read_file, list_files, search_codebase, web_search,
   web_read, git_status, git_diff, memory_read). We already added a
   `requires_project` flag the same way, so the plumbing exists.
2. In `loop.py`, when a turn's tool calls are all concurrency-safe, dispatch them
   with `asyncio.gather` instead of the serial loop; fall back to serial when any
   call is unsafe. Preserve result ordering. This is a real latency win on
   read-heavy turns and it's a contained change to the one dispatch block.

### 4b. Subagents: usage trailer, depth, and briefing guidance

Claude Code returns a subagent's *final message* plus a `<usage>total_tokens /
tool_uses / duration_ms</usage>` trailer, and substitutes "(Subagent completed
but returned no output.)" for empty results (`AgentTool.tsx:1340`). It allows
nesting to a depth guard rather than forbidding it outright, and its Agent-tool
prompt is the best "how to brief a delegate" text anywhere:
> "Brief the agent like a smart colleague who just walked into the room — it
> hasn't seen this conversation… **Never delegate understanding.** Don't write
> 'based on your findings, fix the bug'… include file paths, line numbers, what
> specifically to change." (`AgentTool/prompt.ts`)

**What Jarvis does:** we just gave `spawn_agent` the subagent iteration cap and
report compaction. It returns `[agent reports]\n{final}`, forbids nesting
entirely (`spawn_agent` always excluded), and has a thin briefing note.

**Changes for Jarvis:**
1. Append a usage trailer to the spawn_agent result — Jarvis already tracks the
   shared `Budget`, so `(N tokens / M tool calls)` is free and helps the parent
   judge the report.
2. Handle empty subagent output with an explicit marker instead of returning a
   bare header.
3. Adopt the "smart colleague / never delegate understanding" guidance in
   `spawn_agent/TOOL.md`.
4. Optional: allow one level of nesting behind a depth guard (like clawspring's
   `max_depth`), rather than a blanket ban — the orchestrator you just wired
   already has the depth machinery.

### 4c. The model-facing vs human-facing split

Every Claude Code tool renders results twice: a compact block for the model and
a rich node for the human, with an invariant that the searchable text equals the
rendered text. Jarvis has a version of this asymmetry in the gate (evidence.json
for the human, counts for the model — which we just improved by returning the
verdict). The general lesson: **the model rarely needs the same verbosity the
human console does.** Audit any tool that dumps a human-formatted blob into the
model's context and give it a leaner model-facing form.

---

## Part 5 — Memory and skills

### 5a. The MEMORY.md index pattern (the big one)

Claude Code's auto-memory stores **one markdown file per memory** with YAML
frontmatter (`name`, `description`, `type`), and **loads only a `MEMORY.md`
index into context** — each entry is a one-line `- [Title](file.md) — hook`
(under ~150 chars). Full memories are pulled on demand, either by the model
grepping the dir or by a fast model call that reads just the frontmatter
manifest and picks ≤5 relevant files (`findRelevantMemories.ts`). No embeddings.
The index is capped (200 lines / 25KB) and truncated with a warning when it
overflows (`memdir.ts:35`).

**What Jarvis does:** `memory_block()` loads notes **in full** until a 2000-token
budget, then overflow degrades to **bare names** (`memory.py:203`) — no
descriptions, so the model has to already know a note is relevant to recall it by
exact name.

**Change for Jarvis:** adopt the index-with-descriptions pattern.
- Require each note to carry a one-line `description` in frontmatter (you can
  backfill existing ones cheaply).
- Make the always-loaded standing-memory block a list of `name — description`
  lines for *all* notes (this is tiny), plus the full text of only the highest-
  priority few within budget.
- Keep `memory_read` for pulling a full note on demand. The model now recalls by
  *relevance* (it can read the descriptions) instead of by *exact name*.

This scales to hundreds of notes without bloating context, and it's a modest
change to `memory_block()` and the `memory_write` handler.

### 5b. Memory hygiene: taxonomy, anti-patterns, structure, freshness

Claude Code's memory prompt does a lot of work Jarvis's "memory habit" line
doesn't:
- **Closed type taxonomy** — `user | feedback | project | reference`
  (`memoryTypes.ts`) — and an explicit **what-NOT-to-save** list: code patterns,
  architecture, git history, anything derivable with grep or already in
  CLAUDE.md. This exists because "save this PR list" produced noise.
- **Body structure** for feedback/project: the rule, then `**Why:**`, then
  `**How to apply:**` — so future-you can judge edge cases, not blindly obey.
- **Convert relative dates to absolute at write time** ("Thursday" → the date).
- **Freshness caveat on recall** (`memoryTypes.ts:240`, and the code comment
  notes the header wording measurably changed behavior): "A memory that names a
  specific function, file, or flag is a claim that it existed when the memory was
  written. It may have been renamed, removed, or never merged. 'The memory says X
  exists' is not the same as 'X exists now.'"

**Change for Jarvis:** fold these into `soul.md`'s memory section and the
`memory_write`/`memory_read` TOOL.md. The freshness caveat is especially worth
adding — Jarvis loads memory as ground truth today.

### 5c. Consolidation ("dreaming") — a natural fit for Jarvis's scheduler

Claude Code runs a periodic **forked, read-only pass** that merges duplicate
memories, prunes contradicted facts, converts dates, and rebuilds the index
(`autoDream.ts` + `consolidationPrompt.ts`), gated cheapest-first (time since
last run ≥ 24h, ≥ 5 new sessions, a lock). Its 4-phase prompt: orient → gather
recent signal → consolidate (merge, don't duplicate) → prune + reindex.

**Change for Jarvis:** you already have a scheduler (`backend/schedules.py`) and
headless agents. Add a nightly "consolidate memory" schedule that runs an agent
with read + memory-write tools over `memory/notes/`, using the 4-phase prompt.
This keeps the index lean and the notes non-duplicative automatically — exactly
the "dreaming" pattern, and it costs you almost no new infrastructure.

### 5d. Skills: progressive disclosure (Jarvis currently over-ships)

Claude Code puts only *name + description + when_to_use* of each skill in
context (budgeted to ~1% of the window, per-entry desc capped at 250 chars) and
loads the **full `SKILL.md` body only when the skill is invoked**
(`SkillTool/prompt.ts:25`) — "the listing is for discovery only… verbose
whenToUse strings waste turn-1 cache_creation tokens without improving match
rate."

**What Jarvis does:** it compiles skills into the tool registry *the same as
tools*, shipping `body[:300]` of every skill on every turn (`registry.py`). That
is the anti-pattern — the body rides context whether or not the skill is used.

**Change for Jarvis:** for skill entries specifically, put only
`description + when_to_use` in the tool spec, and load the full `SKILL.md` body
only when the skill is actually invoked (inject it as a message at call time —
mirrors how `load_project` refreshes on the next turn). This is the biggest
per-turn token win available on the skills side.

---

## Part 5.5 — Web tools (search and fetch)

Claude Code's web layer is the *architectural inverse* of Jarvis's, and that's
worth stating first because it changes which lessons transfer. Claude Code has
two tools — `WebSearch` and `WebFetch` — and **neither runs a search engine**.

**WebSearch delegates to the provider** (`WebSearchTool.ts`). When the model
calls it, the tool spins up a *nested* model query with a one-line system prompt
("You are an assistant for performing a web search tool use") and attaches
Anthropic's server-side `web_search_20250305` tool with `max_uses: 8` hardcoded.
The API does the searching; results stream back as `web_search_tool_result`
blocks (`{title, url}`) interleaved with the model's text. So one tool call can
run up to 8 searches server-side. It's read-only, concurrency-safe, and behind a
flag runs on Haiku with forced `tool_choice` to make it cheap.

**WebFetch fetches then summarizes with a prompt** (`WebFetchTool.ts` +
`utils.ts`). It takes a URL *and a prompt*, fetches, converts HTML→markdown
(turndown), truncates to 100k chars, then runs **Haiku over the markdown with
that prompt** and returns the model's answer — never the raw page
(`applyPromptToMarkdown`, `utils.ts:484`). This is the summarize-at-the-edge
pattern, but prompt-driven per fetch, so the model gets exactly what it asked to
extract.

**What Jarvis does** (the better base for a self-hosted secure agent): `web_search`
runs your own **SearXNG** and returns structured results directly — private, no
provider dependency. `web_read`/`read_and_summarize` do SSRF-guarded fetch → strip
to plain text → (for read_and_summarize) summarize to 3-6 bullets. You re-check
SSRF after redirects and reject non-global IPs. **Keep this architecture** — the
provider-delegation model only works because Anthropic runs search server-side,
and SearXNG fits the homelab threat model better.

The engineering worth lifting from Claude Code's implementation:

1. **A short-TTL content cache on `web_read`.** WebFetch keeps a 15-minute,
   URL-keyed, 50MB self-cleaning LRU cache (`utils.ts:61-68`), so refetching a
   URL skips both the download *and* the summarize model call. Jarvis has a fetch
   *ledger* (claim-before-fetch dedup within a session) but no content cache, so
   refetching a URL across turns re-downloads and re-summarizes. Highest-value
   web item.

2. **Make `web_read` prompt-driven.** WebFetch's `prompt` param + secondary-model
   summarize returns exactly what was asked for. Jarvis's `web_read` returns 6000
   chars of raw text; `read_and_summarize` returns fixed bullets. Add an optional
   `extract:` prompt to `web_read` that summarizes toward the model's stated need
   via the small model — the model gets signal, not noise, and the two tools
   converge.

3. **Two cheap prompt fixes.** (a) Claude Code's search prompt forces the current
   year into queries — "The current month is ${month}. You MUST use this year…
   NOT last year" (`WebSearchTool/prompt.ts`) — a real fix for models defaulting
   to stale years. (b) It appends a **mandatory source-citation reminder** to the
   result ("You MUST include the sources above… using markdown hyperlinks"). Add
   both to Jarvis's `web_search` TOOL.md / result formatting so answers cite links.

4. **Cross-host redirect posture.** WebFetch does **not** auto-follow cross-host
   redirects — same-host follows (max 10 hops), cross-host returns the redirect
   URL to the model to decide, explicitly to defeat open-redirect SSRF chains
   (`utils.ts:246-310`). Jarvis re-validates SSRF then follows; the return-to-
   caller pattern is a stronger defense and fits the gate-adjacent threat model
   you already care about. Consider it for `web_read`.

Also noted but lower priority: WebFetch's http→https upgrade, 10MB content cap,
60s timeout, and a preapproved code-doc domain allowlist (with a loud warning
that the *sandbox* must NOT inherit it — upload-capable domains would enable
exfiltration). Jarvis's web path is already inert and host-side, so the allowlist
matters less; the cap/timeout hardening is worth a glance against `webtools.py`.

---

## Part 5.6 — Corrective feedback: teach the model, don't just fail it

This is the pattern you asked about: when the model does something wrong,
incomplete, or out of order, Claude Code almost never throws a bare stack trace
back at it. It returns a short, **instructional** `is_error` tool result — a
sentence that names the problem *and the fix* — and lets the loop continue so the
model self-corrects on the next step. The mechanism is: a failed tool call
produces a `tool_result` with `is_error: true`, wrapped in `<tool_use_error>…`,
whose text is written to be *read by the model as a next-step instruction*, not
logged for a human.

Three things make this work, and all three are the actual lesson:

1. **The message states the fix, not just the fault.** "String not found" would
   be a fault; Claude Code says what to do instead.
2. **The loop keeps going.** An `is_error` result is just another turn input —
   the model reads it and retries correctly. It does not abort the turn.
3. **Guidance/state that isn't an error rides in `<system-reminder>` tags** the
   model is pre-told to trust (`constants/prompts.ts:132`: "…contain useful
   information and reminders. They are automatically added by the system…").

### The canonical examples (verbatim, with when they fire)

**Read-before-edit** — the exact case you described. Editing or writing a file
you haven't read returns (`FileEditTool.ts:280`, `FileWriteTool.ts:202`):
> "File has not been read yet. Read it first before writing to it."

**File changed under you** — if the file's mtime is newer than your last read
(`FileEditTool.ts:305`):
> "File has been modified since read, either by the user or by a linter. Read it
> again before attempting to write it."

**Edit target not unique** — `old_string` matches more than once
(`FileEditTool.ts:336`):
> "Found N matches of the string to replace, but replace_all is false. To replace
> all occurrences, set replace_all to true. To replace only one occurrence,
> please provide more context to uniquely identify the instance."

**Edit target missing** (`FileEditTool.ts:321`): "String to replace not found in
file." · **No-op edit** (`:153`): "No changes to make: old_string and new_string
are exactly the same." · **Wrong tool for the file** (`:270`): "File is a Jupyter
Notebook. Use the NotebookEdit to edit this file."

**File too big to read whole** — note it hands over the *exact* mechanism to
succeed (`FileReadTool.ts:181`):
> "File content (N tokens) exceeds maximum allowed tokens (M). Use offset and
> limit parameters to read specific portions of the file, or search for specific
> content instead of reading the whole file."

**Re-reading an unchanged file** — instead of re-sending the bytes
(`FileReadTool/prompt.ts:7`):
> "File unchanged since last read. The content from the earlier Read tool_result
> in this conversation is still current — refer to that instead of re-reading."

**Not-found with orientation** — every "does not exist" appends the cwd and a
did-you-mean (`GrepTool.ts:217`, `GlobTool.ts:111`, `file.ts:213`): "…Note: your
current working directory is /… Did you mean <path>?"

**Empty results are words, not empty strings** (`GrepTool.ts:299`,
`GlobTool.ts:182`): "No files found" / "No matches found" — and truncation
teaches how to narrow (`GlobTool.ts:192`): "(Results are truncated. Consider
using a more specific path or pattern.)"

**Bash blocking-sleep nudge** — a great example of redirecting to the *right*
tool (`BashTool.tsx:530`):
> "Blocked: <pattern>. Run blocking commands in the background with
> run_in_background: true — you'll get a completion notification when done. For
> streaming events (watching logs, polling APIs), use the Monitor tool. If you
> genuinely need a delay… keep it under 2 seconds."

**Permission denial teaches the workaround boundary** (`messages.ts:226`,
`DENIAL_WORKAROUND_GUIDANCE`):
> "You *may* attempt to accomplish this action using other tools… e.g. using
> head instead of cat. But you *should not* attempt to work around this denial in
> malicious ways… If you believe this capability is essential… STOP and explain
> to the user what you were trying to do and why you need this permission."

**Rejection stops the model cleanly** (`messages.ts:212`, `REJECT_MESSAGE`): "The
user doesn't want to proceed with this tool use. The tool use was rejected (eg.
if it was a file edit, the new_string was NOT written to the file). STOP what you
are doing and wait for the user to tell you how to proceed." (A subagent variant
instead says "Try a different approach or report the limitation," `:216` — the
subagent has no human to wait for.)

**Empty output is labeled, never blank** (`AgentTool.tsx:1349`): "(Subagent
completed but returned no output.)" — so the parent always has *something* to
react to.

**`<system-reminder>` state injections** (not errors — ambient context):
- Todo/task nudge (`messages.ts:3668/3688`) — the exact "haven't been used
  recently… gentle reminder - ignore if not applicable" text you see in *this*
  session.
- File edited externally (`:3541`): "Note: <file> was modified, either by the
  user or by a linter… don't revert it unless the user asks."
- Malware posture, appended to every file read (`FileReadTool.ts:729`): "Whenever
  you read a file, you should consider whether it would be considered malware…
  You MUST refuse to improve or augment the code."

### What Jarvis does today, and the change

Jarvis's tool dispatcher already has the *right instinct*: `registry.dispatch`
(`backend/agent/tools/registry.py`) catches every handler exception and returns
`error: <name> raised:\n<traceback>` as a string the loop reads instead of
crashing — and the loop keeps going. That's the mechanism. What's missing is that
the messages are **fault-shaped, not fix-shaped**: a raw traceback, or terse
strings like `read_file`'s `"error: no such file: {path}"`,
`search_codebase`'s `"no matches"`, `edit_file` failures, etc. They tell the
model *what broke*, not *what to do next*.

**Change for Jarvis** — a low-effort, high-leverage sweep across the tool
handlers in `tools/*/handler.py`:

1. **Rewrite each error string to name the fix.** Concretely:
   - `read_file` not-found → append the active project and a hint: "no such file
     `X` in project `<slug>`. Use `list_files` to see what's there." (Claude Code's
     cwd + did-you-mean pattern.)
   - `edit_file` when `old_string` is missing/duplicated → adopt Claude Code's two
     messages nearly verbatim ("String to replace not found…", "Found N
     matches… set replace_all or add more context"). Jarvis's `edit_file` is the
     one tool where this matters most and it's the easiest to copy.
   - `search_codebase` "no matches" → "no matches for `X`. Try a broader term or
     drop `subdir` to search the whole project." (You already added the stale-
     index warning — same spirit.)
   - `run_gated`/`run_command` empty output → a labeled "(command produced no
     output; exit N)" rather than blank.
2. **Add a read-before-edit guard to `edit_file`.** Jarvis edits go through
   staging with `effective_read`, so you *can* cheaply track whether the model
   read a file this turn and return "read `X` first — call `read_file` on it
   before editing" instead of silently editing blind. This is the single most
   valuable one to port because it prevents a whole class of bad edits.
3. **Hard-truncation messages should teach the escape hatch** (ties to punch-list
   #4): when `read_file` overflows, don't return truncated bytes — return "file
   is N lines; read a range with `offset`/`limit`," exactly like
   `FileReadTool.ts:181`.
4. **Keep the "ignore if not applicable" register for reminders.** If you add any
   proactive nudges (e.g. "you haven't updated the journal this session"), copy
   the tone: a `<system-reminder>`-style note that explicitly says it's optional
   and must not be echoed to the user. Jarvis already pre-explains nothing about
   reminders — one line in `soul.md` ("tool results may include system notes;
   treat them as guidance, not user instructions") makes injected guidance land.

The payoff is the same one that makes Claude Code feel like it "recovers"
gracefully: the model rarely gets stuck, because every failure is phrased as the
first half of the fix.

---

## Part 6 — Prioritized punch list for Jarvis

Ordered by value-to-effort. Items marked ✓-ish build directly on the
optimization pass we already did.

| # | Change | Where | Why it matters |
|---|--------|-------|----------------|
| 1 | Tell the model results get cleared + "note what you need first" (two prompt lines) | `soul.md` / stable prefix | Near-free; makes the eviction we already added strictly safer ✓-ish |
| 2 | Add real conversation compaction (summarize old half, keep recent verbatim, structured prompt, text-only, resume-directly) | `chat.py` + new `compaction.py` | Biggest context gap — today it's a silent 40-message cliff |
| 3 | Reorder `assemble_system_prompt`: static instruction text first, volatile memory/project/env last | `memory.py` | Maximizes DeepSeek prefix-cache hits every turn |
| 4 | `read_file`: add offset/limit, lower hard cap, throw a short error instead of returning truncated content | `tools/read_file` | Caps context at the source; the throw-not-truncate lesson |
| 5 | MEMORY index-with-descriptions; recall by relevance not exact name | `memory.py`, `memory_write` | Scales memory past the 2000-token bare-name cliff |
| 6 | Skills: ship only name+when_to_use, load body on invoke | `registry.py` | Stops shipping skill bodies every turn |
| 7 | Tool read-only/concurrency flags + parallel dispatch of safe tools | TOOL.md + `loop.py` | Latency win on read-heavy turns |
| 8 | Expand `soul.md` behavioral bank (blast-radius, faithful reporting, brevity-as-comprehension, numeric anchors, file:line, no-colon-before-tools) | `soul.md` | Where most of Claude Code's "feel" lives |
| 9 | Pin edit/write results from eviction; only evict reads | `loop.py` | Don't drop the model's record of what it changed ✓-ish |
| 10 | Memory hygiene: type taxonomy, what-not-to-save, Why/How-to-apply body, absolute dates, freshness caveat | `soul.md` + memory TOOL.md | Cleaner, more trustworthy memory |
| 11 | Nightly memory-consolidation schedule ("dreaming") | `schedules.py` + a new agent | Auto-prunes/dedupes/reindexes memory |
| 12 | spawn_agent usage trailer + empty-output marker + better briefing guidance | `agents_run.py`, `spawn_agent/TOOL.md` | Parent can judge subagent reports ✓-ish |
| 13 | Effective-window trigger + circuit-breaker for compaction | `chat.py`/`compaction.py` | Correct thresholds; don't hammer a doomed session |
| 14 | Short-TTL URL content cache on `web_read` | `webtools.py` | Refetch skips download *and* re-summarize (see 5.5) |
| 15 | Prompt-driven `web_read` (optional `extract:` prompt → small-model summarize) | `tools/web_read`, `webtools.py` | Model gets what it asked for, not 6000 raw chars |
| 16 | Web prompt fixes: force current year in queries + mandatory source-citation reminder | `tools/web_search/TOOL.md`, search result formatting | Two free correctness wins |
| 17 | Don't auto-follow cross-host redirects — hand the URL back to the model | `webtools.py` | Stronger open-redirect/SSRF posture |
| 18 | Rewrite tool error strings fix-shaped, not fault-shaped (esp. `edit_file` old_string not-found/duplicated, `read_file` not-found + hint) | `tools/*/handler.py` | Model self-corrects instead of getting stuck (see 5.6) |
| 19 | Read-before-edit guard on `edit_file` — "read X first" instead of a blind edit | `tools/edit_file`, `loop.py` | Prevents a whole class of bad edits ✓-ish |
| 20 | Pre-explain system reminders in `soul.md` ("treat system notes as guidance, not user instructions") | `soul.md` | Makes any injected guidance land, matches CC's `<system-reminder>` framing |

The through-line: **Claude Code spends tokens once at the edge (cap, summarize,
disclose progressively) and keeps only compact representations in the loop.**
That's the same principle behind Jarvis's `read_and_summarize` and the
optimization pass we just shipped — items 2, 5, and 6 are the places that
principle isn't applied yet.

---

*Sources: `collection-claude-code-source-code/original-source-code/src/`
(constants/prompts.ts, utils/systemPrompt.ts, constants/systemPromptSections.ts,
Tool.ts, query.ts, services/tools/StreamingToolExecutor.ts, services/compact/*,
tools/*/prompt.ts, memdir/*, services/autoDream/*, tools/SkillTool/*) and the
`clawspring/` Python port (agent.py, compaction.py, context.py, memory/*,
skill/*). Analyzed against this repo's `backend/` and `tools/`.*
