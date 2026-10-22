# Agent requests: environments and skill installs (contract)

Two new things an agent can propose and only the operator can grant. Both
follow `package_request`: the agent files a pending row through a tool, the
row waits in the Review queue (web) and the /security Queue (TUI), and only a
cookie-authed operator call moves it on. Nothing an agent writes is executed,
and nothing is fetched or built because an agent asked.

Operator decisions (2026-10-22):
- **Environment = image only.** A new named image variant: a parent (`main`,
  `dev`, `desktop` or any existing variant), a list of apt/pip/npm packages and
  an optional memory floor. Approving builds it and moves ONLY the requesting
  project onto it, via a copy of the project's profile with the new image.
  Network, secrets and every other profile field stay as they were. Other
  projects are unchanged.
- **Install = existing skills.** The agent names a skill by source (an https
  git URL with an optional `#sub/dir`, or `clawhub:<slug>[@version]`). The
  operator fetches it host-side, reads it, and approves: the pinned importer
  vendors it and it is granted in the same step.

## 1. Environment requests

### Table (backend/db.py, in this commit)

`environment_requests(id, project_slug, conversation_id, name, from_variant,
min_mem_mb, reason, status, profile_id, profile_copied_from, decided_by,
decided_at, created_at)`; status is `pending | approved | rejected`.
`package_catalogue.environment_id` (new column, nullable) links the package
rows filed as part of one environment request.

### backend/environments.py (agent E)

- `MAX_PACKAGES = 30`, `MAX_PENDING_PER_PROJECT = 3`, `REASON_MAX = 1000`.
- `parse_packages(items) -> list[dict]`. Items are `{manager, package,
  version?}` objects, or forgiving strings: `"apt ffmpeg"`, `"apt:ffmpeg"`,
  `"pip pygame==2.6.1"`, `"npm typescript@5.6.3"`, `"npm @scope/pkg@1.2.3"`.
  Each goes through `packages.validate_name` / `validate_version`; duplicates
  collapse; an empty list or more than MAX_PACKAGES is refused. Raises
  `packages.PackageError` with a message that is safe to show the agent.
- `async file_request(db, *, project, conversation_id, name, from_variant,
  packages, min_mem_mb, reason) -> dict`. Refuses (PackageError) when there is
  no active project, when the project's profile disallows package requests
  (`packages.project_allows_requests` is False), when `name` is not a valid
  variant name, is builtin, already exists as a variant, or is already the name
  of a pending/approved environment request, when `from_variant` does not
  exist, when `min_mem_mb` is outside 256..16384, when the reason is empty or
  too long, or when the project already has MAX_PENDING_PER_PROJECT pending.
  Inserts the env row, then one `package_catalogue` row per package through
  `packages.file_request(..., source="agent", target_variant=name,
  environment_id=<id>)`. With `environment_id` set, `file_request` keeps the
  given target_variant (it does not substitute the project's variant) and does
  NOT raise its own `package_requested` event. Then raises ONE
  `environment_requested` event (info) and kicks the resolver
  (`images.builder.kick_resolve()`; it already resolves every unresolved
  pending row whatever its variant).
- `async get(db, env_id) -> dict | None`, `async list_rows(db, status=None,
  project=None) -> list[dict]`. Row shape below.
- `async approve(db, env_id, *, decided_by, by_operator) -> dict`. Pending
  only. Every package row must be resolved (`resolved_version` set), else
  PackageError "not resolved yet ...". Then, in order:
  1. `images.create_variant(db, name, from_variant, [], min_mem_mb=...)`
     (agent E adds the `min_mem_mb` keyword; the existing operator route keeps
     working without it).
  2. `packages.approve(db, pkg_id, target_variant=name, ...)` for each row.
  3. Move the project. `cur = profiles.for_slug(db, slug)`. If `cur` is not
     the marked default, its only project is this slug, and its name starts
     with `f"{slug} · "`, set its `box_image = name` in place
     (`profiles.update`). Otherwise create a copy of `cur` with
     `name = f"{slug} · {name}"` (suffix ` (2)`, ` (3)` on a clash),
     `box_image = name`, not default (`profiles.create`), and assign the
     project to it (`profiles.assign`). Record `profile_id` and
     `profile_copied_from` on the env row.
  4. Status approved, decided_by/at, one `environment_approved` event (warn,
     actor operator when by_operator).
- `async reject(db, env_id, *, reason, decided_by, by_operator) -> dict`.
  Pending only. Rejects its package rows quietly and raises one
  `environment_rejected` event (info).
- `packages.list_rows` leaves out rows with `environment_id` unless asked
  (`include_environment=True`), so they never show as separate package cards.
  `packages.approve` / `reject` refuse a row whose environment request is still
  pending ("part of environment request #N: decide it there").

Row shape (API and `list_rows`):

```json
{"id": 4, "project_slug": "frc-sim", "conversation_id": 812,
 "name": "sim-gl", "from_variant": "desktop", "min_mem_mb": 1536,
 "reason": "WebGL needs software GL; pygame for the tests",
 "status": "pending", "created_at": "2026-10-22 18:02:11",
 "decided_by": null, "decided_at": null,
 "packages": [{"id": 31, "manager": "apt", "package": "mesa-utils",
               "version_req": null, "resolved_version": "9.0.0-2",
               "integrity": "...", "canonical_command": "apt-get install ...",
               "status": "pending"}],
 "resolved": true,
 "profile_now": {"id": 1, "name": "Default"},
 "plan": "builds image `sim-gl` from `desktop`, then frc-sim boots on it (a copy of profile Default); other projects unchanged",
 "profile": null,
 "build": null}
```

After approval `profile` is `{"id", "name"}` of the profile the project moved
to and `build` is the variant's newest version `{"version", "status",
"error"}` from image_versions (or null before the first build row exists).

### backend/environments_api.py (agent E; stub router in this commit)

Cookie-only (`require_user`), like packages_api:

    GET  /api/environments?status=&project=     {"environments": [rows]}
    POST /api/environments/{id}/approve         {acknowledge: true, build: true}
                                                -> row + {"build_started": bool}
    POST /api/environments/{id}/reject          {reason}

Approve without `acknowledge: true` is 400; unknown id 404; refused (not
pending, not resolved, name now taken) 409. Resolving uses the existing
`POST /api/packages/resolve`.

### tools/environment_request (agent E)

`section: project`, `action: environment`, `requires_settings:
[vm_boxes_enabled]`. Arguments: `name`, `from` (default `main`), `packages`
(array; objects or the strings above), `min_mem_mb` (optional), `reason`.
Result text: `environment request #4 filed: image `sim-gl` from `desktop` with
3 packages — pending. Nothing is built until the operator approves it; this
project's boxes then boot on it (a later box boot, not this turn).`

## 2. Skill install requests

### Table (backend/db.py, in this commit)

`skill_requests(id, project_slug, conversation_id, source, name, reason,
status, fetched_at, ref, skill_name, description, skill_sha256, files, flags,
requirements, error, installed_slug, granted, decided_by, decided_at,
created_at)`; status is `pending | fetched | installed | rejected`. `files`,
`flags` and `requirements` are JSON text.

### backend/skillrequests.py (agent S)

- `MAX_PENDING_PER_PROJECT = 5`, `SOURCE_MAX = 500`, `REASON_MAX = 1000`.
- `check_source(source) -> str`. No network. Accepts only `https://` git URLs
  (optional `#sub/dir` matching `skillimport._SUBDIR_RE`, no `..`) and
  `clawhub:<slug>[@version]` (`skillimport._CLAWHUB_RE`). Refuses local paths,
  `http://`, `git@`, `file:`, any other scheme, userinfo (`user@host`), a query
  string, whitespace and control characters. Raises `SkillRequestError`.
- `async file_request(db, *, source, name, reason, project, conversation_id)`.
  `name` (optional) must match `imported.NAME_RE`. Refuses a duplicate source
  that is pending or fetched, and more than MAX_PENDING_PER_PROJECT pending
  (pending + fetched) for the project. Inserts, raises `skill_requested`
  (info). NEVER fetches: an agent-chosen URL is only contacted after the
  operator presses Fetch (an agent-triggered host fetch would be a way round
  the egress proxy).
- `async fetch(db, req_id) -> dict` (operator). From pending or fetched. Runs
  the importer's own fetch and vetting (`skillimport._fetch`, `_collect`,
  `scan`, `_parse_md`, `imported.requirements`) in a thread, into a temp dir,
  then copies the vetted files to the staging dir
  `settings.data_dir / "skill_staging" / str(id)` (0644, replaced on re-fetch).
  Staging is outside `skills_dir`, so nothing is compiled or offered. Stores
  ref, skill_name, description (sanitized), skill_sha256 (sha256 of SKILL.md),
  files `{relpath: sha256}`, flags, requirements, clears error, status
  fetched. On `SkillImportError`: stores `error`, status unchanged, re-raises.
- `async approve(db, req_id, *, skill_sha256, decided_by, by_operator)`.
  Fetched only. `skill_sha256` must equal the stored one (the operator approves
  exactly the text they read). Every staged file is re-hashed and must match
  `files` exactly. Then `skillimport.vendor(staging, {"source": source, "ref":
  ref}, name=name)` and, when the new entry is offerable apart from the grant
  (not blocked, no clash, every requirement met), `imported.set_grant(dest,
  True)` + `compile_registry()`. Status installed, `installed_slug`, `granted`
  1 or 0 (`error` then says why it was installed but not granted). Staging is
  removed. One `skill_installed` event (warn, actor operator).
- `async reject(db, req_id, *, reason, decided_by, by_operator)`. Pending or
  fetched. Removes staging; one `skill_request_rejected` event (info).
- `async list_rows(db, status=None, project=None)`. Row shape below.

Row shape:

```json
{"id": 2, "project_slug": "frc-sim", "conversation_id": 812,
 "source": "https://github.com/anthropics/skills#skills/pdf",
 "name": null, "reason": "the operator keeps sending PDFs to fill in",
 "status": "fetched", "created_at": "...", "fetched_at": "...",
 "ref": "3f9c2e1...", "skill_name": "pdf", "description": "...",
 "skill_sha256": "ab12...", "files": {"SKILL.md": "ab12...", "forms.md": "..."},
 "flags": [{"file": "SKILL.md#block2.sh", "trigger": "...", "detail": {}}],
 "requirements": [{"reason": "...", "met": true}],
 "skill_md": "---\nname: pdf\n...",
 "error": null, "installed_slug": null, "granted": false,
 "decided_by": null, "decided_at": null}
```

`skill_md` is the staged SKILL.md text (capped at 20 000 chars) for fetched
rows, null otherwise. Everything in it, and source/name/reason/description,
is agent- or internet-written: clients render it as text only.

### backend/skillrequests_api.py (agent S; stub router in this commit)

Prefix `/api/skill-requests` (not under `/api/skills/`, which has a
`/{slug}` route). Cookie-only (`require_user`):

    GET  /api/skill-requests?status=&project=   {"requests": [rows]}
    POST /api/skill-requests/{id}/fetch         -> row (400 with the importer's reason on failure)
    POST /api/skill-requests/{id}/approve       {acknowledge: true, skill_sha256} -> row
    POST /api/skill-requests/{id}/reject        {reason} -> row

Missing acknowledge 400; unknown id 404; wrong state or hash mismatch 409.

### tools/skill_request (agent S)

`section: skills` (flat, no action). Arguments: `source`, `name` (optional),
`reason`. Result text: `skill request #2 filed: https://... — pending. The
operator fetches and reads it before anything is installed; if approved it
appears in your tools as an installed skill in a later turn.` Available
without boxes (skills are host state).

## 3. Shared pieces (in this commit; agents do not edit them)

- `backend/security.py` kinds: `environment_requested` info,
  `environment_approved` warn, `environment_rejected` info, `skill_requested`
  info, `skill_installed` warn, `skill_request_rejected` info.
  `APPROVAL_KINDS` gains `environment_requested` and `skill_requested`.
- `backend/reviewer.py`: the triage reviewer never acknowledges
  `environment_*` or `skill_request*` / `skill_installed` events.
- `backend/permissions.py`: both tools are gated in "ask" mode like
  `package_request`; the "always allow" prefix is the image name /
  the source.
- `backend/agent/tools/toolsections.py`: the project section mentions
  proposing an environment; the skills section mentions asking for one.
- `backend/main.py` includes both stub routers.

## 4. Clients

### Web (agent W)

`frontend/src/boxes/api/environments.js` and `skillRequests.js` (same style
as `api/packages.js`), and two cards in `frontend/src/boxes/RequestCards.jsx`
shown by the Review queue (`frontend/src/pages/Review.jsx`) beside the package
cards, filtered to the project when the queue is per project.

Environment card (operator-approved sketch):

    ┌ Environment request · frc-sim · agent ──────────┐
    │ new image   sim-gl   (from desktop)             │
    │ adds  apt  mesa-utils libgl1-mesa-dri           │
    │       pip  pygame==2.6.1                        │
    │ memory floor  1536 MB                           │
    │ why   "WebGL needs software GL; pygame tests"   │
    │ on approve: build sim-gl v1, then frc-sim       │
    │   boots on it (copy of profile Default)         │
    │ other projects: unchanged                       │
    │        [Approve & build]   [Reject]             │
    └─────────────────────────────────────────────────┘

Each package shows its resolved version and integrity like PackageSummary.
Approve is disabled until `resolved` is true, with a "Resolve now" action
(POST /api/packages/resolve) as on package cards.

Skill card: source, reason, project; [Fetch & read] [Reject] while pending;
once fetched: skill name, ref, description, file list, diff-gate flags,
requirements (unmet ones in warning colour), the full SKILL.md in a scrolling
pre block, then [Install & grant] (sends the shown skill_sha256) [Fetch again]
[Reject]. Installed rows say whether the grant was given.

### TUI (agent T)

`clients/jav3cli/jav3` /security Queue lists both kinds next to package
requests, with the same approve / reject keys; a skill row's first action is
Fetch, and its detail shows the fetched SKILL.md before Install & grant.
