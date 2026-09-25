# Agents detail view — rebuild brief

The operator's ask (v2.1): the agent detail view "needs to be waaay cleaner".
One agent cleared it (this commit); the next builds it from scratch, simple,
with no clutter of text as design. `Agents.jsx`'s `AgentDefinitions` is now a
working skeleton — roster, name, prompt body, Save / Delete / Run-Stop — and
this file is everything the rebuild needs. Delete this file in the rebuild commit.

## (a) What the old editor showed

Source of truth for the old markup: `git show HEAD~1:frontend/src/pages/Agents.jsx`
(the parent of the strip commit).

### Essential — the rebuild must have these

| Control | Old form | Field |
|---|---|---|
| Name | `Input label="name"` | `name` |
| Prompt (AGENT.md body) | `textarea.md-editor rows=7`, label "system prompt" | `prompt` |
| Works in (project) | `Select label="works in"`, options: `no project — follows whoever starts it`, each project by name, and a stale binding as `<slug> (missing)` | `project` |
| Model override | `Input label="model"`, placeholder `inherit (<active model label>)` from `useModel()` + `modelOption(info, info.active).label` | `model` |
| Run / Stop | not in the old editor at all (lived in Workspace's "Run an agent" panel) — the skeleton adds it | `POST .../run`, `.../runs/{cid}/stop` |
| Delete | ghost danger `Delete` in the pane toolbar, confirm `Move agent "<slug>" to trash?` / `Move to trash` | `DELETE` |
| Save | `SaveButton` (label is the state: Save / Saved) | `PUT` |

### Secondary — keep, but out of the way (one disclosure, not the first screen)

| Control | Old form | Field |
|---|---|---|
| Max rounds | `Input type=number min=0 max=200 label="max rounds"` | `max_iterations` (int, 0 = path default) |
| Own memory | `Toggle label/onText/offText="own memory"` | `own_memory` (bool) |
| Private notes | `<details>` "its private notes", lazy-loaded on open; each note a nested `<details>` with `<code>name</code> — description` and a `<pre>` body; `none yet` / `loading…` | `GET .../memory` |
| Skills exclusions | chip per skill, `aria-pressed` = kept, struck through when taken away | `skills_exclude` (list of skill names, from `GET /api/skills` → `skills[].name`) |
| Base URL | `Input label="base url"` | `base_url` |
| Description | `Input label="description"` (also the seed for the prompt generator) | `description` |
| Outputs link | `outputs` link in the toolbar → `/agents/outputs/<slug>` | — |
| Prompt generator | ghost `Generate` beside the prompt label → quiz → `Write the prompt` / `Cancel` | `POST /api/agents/prompt-quiz` `{description}` → `{questions:[{question, kind: single\|multi\|short, options}]}`; `POST /api/agents/prompt-generate` `{description, answers:[{question, answer}]}` → `{prompt}`. Both endpoints still exist and now have no frontend caller. |
| Secret references | one ghost button per secret inserting `{{secret:NAME}}` at the end of the prompt | `GET /api/secrets` → `secrets[].name`, `.hosts` |

### Clutter — removed; exact copy, in case a phrase is worth reusing

Explanatory prose and hints:
- Roster footer paragraph: "talk to an agent from a project board's chat panel, give it a one-off task in the Run an agent panel, put it on a schedule, or have Jav3 summon one in chat."
- works-in hint: "its runs and threads start in this project unless the one starting it names another"
- max-rounds hint: "tool-calling rounds per run; 0 = the default for how it was started"
- base-url placeholder essay: "default endpoint · ollama: http://<host>:11434/v1"
- secrets line: "API keys — click to reference (the agent uses the key, never sees its value):" … "— new keys are added in Review → Secrets."
- quiz heading: "quick quiz — answers shape the prompt"; quiz input placeholder "short answer…"; busy labels "…" / "writing…"
- skills hint: "every skill — click one to take it away" / "`N` of `M` taken away"; empty: "no skills yet — add one in the Skills tab"
- own-memory hint: "keeps its notes to itself instead of writing to the shared notes — the operator's standing notes still lead its prompt"

Tooltips (`title=`):
- new-agent input: "new agent name — press n to jump here"
- Generate: "answer a short quiz, get a generated prompt"
- secret buttons: "usable in web_read on <hosts>" / "unusable — bind web hosts in Review → Secrets to allow web_read"
- skill chips: "click to take this skill away" / "click to give it back"

Duplicates and noise:
- Own-memory `Toggle` passed the same string three times (`label`, `onText`, `offText`).
- Roster rows carried a sub-line of `Tag`s (project name + raw model id) under every name.
- The "system prompt" label row doubled as a toolbar (label + Generate button).
- Three two-up `field-row`s (name/description, works in/max rounds, model/base url) gave six fields equal weight above the prompt, pushing the one field that matters below the fold.
- The editor was its own scroll container (`.agent-form { overflow-y: auto }`) inside the page.

Kept in the skeleton (plumbing, not clutter): the `n` shortcut to the new-name field, `+` with an empty field asking for a name, the phone scroll-into-view on pick, the Recently deleted fold (restore ↺ / delete forever ×), the empty states.

## (b) API contract

All under `require_user`. Source: `backend/agents_api.py`, `backend/agents_run.py`.

- `GET /api/agents` → `{agents: [{slug, name, description, model, project}]}`
- `GET /api/agents/trash` → same shape, the soft-deleted ones
- `POST /api/agents` `{name}` → `{slug}`; 409 if the slug exists. New agents get the CO-STAR `DEFAULT_PROMPT`.
- `GET /api/agents/{slug}` → `{slug, name, prompt, description, model, base_url, own_memory, context_exclude, tools_exclude, skills_exclude, max_iterations, project}` (404 if missing)
- `PUT /api/agents/{slug}` body `SaveAgent`: `name` (required), `description`, `model`, `base_url`, `own_memory: bool`, `context_exclude: [str]`, `tools_exclude: [str]`, `skills_exclude: [str]`, `max_iterations: int`, `project`, `prompt`. **Every omitted field resets to its default** — always send the whole object you GOT back (the skeleton PUTs `agent` wholesale). 400 `no such project: <slug>` when `project` isn't a live project.
- `DELETE /api/agents/{slug}` → soft delete to `agents/.trash/`
- `POST /api/agents/{slug}/restore` (409 if the slug was re-created), `DELETE /api/agents/{slug}/purge` (trash only)
- `POST /api/agents/{slug}/run` `{task, confirm_peak?: bool, project?: str}` → SSE (`chatStream(body, onEvent, url)`). Events: `start {conversation_id, agent, agent_slug}`, `token {text}`, `tool {name,…}`, `final {content}`, `error {message}`. 409 `peak_confirmation_required` before any event (retry with `confirm_peak: true`). The run is detached: closing the stream does not stop it; completion posts a notice (toasted unless `agentWatch.watchRun(cid)` marks it on-screen).
- `GET /api/agents/runs/{cid}/stream` → re-attach (`idle` event if not running)
- `POST /api/agents/runs/{cid}/stop` → `{stopped: bool}`
- `GET /api/agents/{slug}/memory` → `{slug, notes: [{name, description, body}]}` (empty unless `own_memory`)
- `GET /api/agents/{slug}/outputs` / `GET /api/agents/outputs` — the Outputs tab's data; not the detail view's.
- `GET /api/projects` → `{projects: [{slug, name, …}]}` for the works-in picker
- `GET /api/skills` → `{skills: [{name, …}]}` for skill exclusions; `GET /api/secrets` → `{secrets: [{name, hosts}]}`
- Model label: `useModel()` / `modelOption()` in `frontend/src/modelInfo.js`

## (c) Page state and what the shell expects

`Agents` (the layout route) owns the one `<h1>` and the tab strip (`/agents` Definitions, `/agents/skills`, `/agents/outputs[/:slug]`) and renders `<Page variant="split">` → `<Outlet/>`. The split layout expects the child to render exactly **`<aside>` then `<main>`** as siblings (fragment); `SkillsPanel` does the same. `main.split-idle` hides the empty pane on a phone so the list gets the screen — keep that class when nothing is selected. `routes.jsx` imports the named export `AgentDefinitions` lazily; keep the name.

Skeleton state: `agents`, `trash` (roster + bin), `selected` (slug; not in the URL), `agent` (the full GET object, edited in place via `patch`), `dirty`, `runId` + `running` (Run ⇄ Stop). Refs: `nameRef` (new-agent input, `n` shortcut), `editorRef` (phone scroll-into-view). Shared classes still in use by siblings — don't delete: `agent-list`, `agent-row*` (SkillsPanel), `editor-pane`, `split-idle`, `md-editor`, `deleted-fold`, `trash-list`, `agent-new`.

## (d) Hard design constraints for the rebuild

1. **No explanatory prose in the view.** Labels only; at most one single-line hint per field, and only where the field is unusable without it. No tooltips that restate the label, no paragraphs, no placeholder essays.
2. **Every control on the primitives** (`frontend/src/components/`: Button/SaveButton, Input, Select, Toggle, Toolbar, Tag, EmptyState, Menu, Modal, Card). No bespoke chip/quiz/toggle markup; tokens only, no hardcoded colours.
3. **One screen at 1440, no inner scroll containers.** The prompt is the hero and grows to fill; secondary settings live behind a single disclosure or menu, not stacked above the prompt.
4. **Phone:** the list gives way to the detail — picking an agent shows the detail full-screen with a way back, instead of stacking the editor under the roster.
