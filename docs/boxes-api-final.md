# Boxes: the final HTTP / SSE shapes (as merged on `boxes`)

What the web UI (WP6) and the TUI (WP7) must code against. This supersedes
the sketches in `docs/boxes-contract.md` section J wherever they differ.
Every route below is cookie-only (`require_user`) and same-origin gated.
Every string that came from a guest (process names, command lines, service
logs, build log lines) is untrusted: render it as a text node, never as HTML.

Live updates: `GET /api/events?topics=<comma list>` (SSE). Box topics:
`procs`, `vm-images`, `vm-boxes`, plus the existing `security` and `egress`.

---

## 1. VM boxes (WP1, runtimes from WP8)

`GET /api/vm/boxes`

```
{enabled: bool,                       # settings.vm_boxes_enabled
 boxes: [{id, kind: "shared"|"project"|"service"|"builder", project, cid,
          runtime: "kvm"|"docker", service_id, placement,
          image: {variant, version}, mem_mb,
          net: {tap, host_ip, guest_ip},
          state: "running"|"stopped", rss_bytes, cpu_pct, uptime_s, inflight,
          disk: {overlay_bytes, data_bytes}}],
 budget: {ram_mb_used, ram_mb_cap, boxes, boxes_cap, project_boxes, project_boxes_cap},
 runtimes: {kvm:    {available: bool, reason: str|null},
            docker: {available: bool, reason: str|null, rootless, userns, gvisor,
                     seccomp, weak, warnings: [str]}}}
```

- Flag off: `boxes` is just the shared box.
- Grey out docker in a profile's runtime picker when `runtimes.docker.available`
  is false, and show `reason`. When `weak` is true, show `warnings`: a docker
  box would run without a user namespace.
- `POST /api/vm/boxes/{id}/start` returns the row. `p-<slug>` may be started
  before its first turn; it is allocated with the project's profile (image,
  memory, runtime). 409 means over a cap (the detail is the reason), 404 an
  unknown id, 502 a boot failure.
- `POST /api/vm/boxes/{id}/stop` returns the row.
- `POST /api/vm/boxes/{id}/destroy {confirm: true, delete_data: bool}` returns
  `{ok: true}`. It answers 400 without `confirm`. For the shared box it only stops it.
- SSE topic `vm-boxes`: `{type: "box_up"|"box_down", box: <the static half of a row>}`.
  Refetch `GET /api/vm/boxes` on either.
- The old `/api/vm/status|boot|teardown|nuke|selftest|rebuild` routes are unchanged (shared-box aliases).

## 2. Security profiles (WP2)

`GET /api/profiles` returns `{profiles: [row]}`. A row is:

```
{id, name, builtin, default_verdict: "deny"|"allow", network_off, allow_hosts: [],
 deny_hosts: [], secrets: [NAMES], auto_handle, separate_box, box_image,
 box_mem_mb, box_runtime: "kvm"|"docker",
 allow_services, allow_package_requests,
 service_placement: "per_service"|"per_project"|"shared", projects: [slugs]}
```

- `POST /api/profiles` takes every field. `service_placement` and `box_runtime`
  are REQUIRED (422 without them). It returns the row.
- `PUT /api/profiles/{id}` takes a partial body and returns the row.
- `DELETE /api/profiles/{id}`: a builtin gives 409.
- `PUT /api/projects/{slug}/profile {profile_id}` returns
  `{ok, project, profile: {id, name}}`. It gives 404 for an unknown project and
  409 for `__image_build__`.
- Every change raises the security event `profile_changed`.

## 3. Egress policy and allowlist (WP2)

- `GET /api/egress/policy/{slug}` returns:
  ```
  {slug, profile: {id, name, default, network_off, builtin},
   project_allow, project_deny, effective_allow, effective_deny,
   mode, inherit_general, hosts, effective, source}
  ```
  The last five keys are the read-only view from before profiles.
  `slug = "__image_build__"` returns the fixed builder policy:
  `profile.name "Image build"`, `source "fixed"`, and the registry hosts in `effective_allow`.
- `PUT /api/egress/policy/{slug} {allow?: [], deny?: []}` replaces the project's
  own lists. The legacy body `{mode, inherit_general, hosts}` is still accepted.
  It is refused for `__general__` (edit the Default profile) and for `__image_build__`.
- `POST /api/egress/policy/{slug}/promote {host, profile_id?: null, list: "allow"|"deny"}`
  copies a host into a profile. `null` means the project's own profile.
- `GET /api/egress/allowlist` returns `{groups: [...]}`, with project groups first and then profile groups:
  ```
  {project: <slug> | "__general__" | "profile:<id>", kind: "project"|"profile",
   profile: {id, name, default},
   entries: [{host, source: "operator"|"reviewer"|"auto"|"seed", id?, rule?, reason?,
              created_at?, expires_at?}],
   deny: [hosts], projects?: [slugs] (profile groups)}
  ```
- `POST /api/egress/allowlist/revoke {project, host, id?, list: "allow"|"deny"}`
- `POST /api/egress/allow {project, host}` returns `{ok, host, added_to}`. It
  returns `needs_project: true` when there is no project.
- `GET /api/egress/pending?project=` is unchanged. `POST /api/egress/pending/{id}/approve|reject` and `/pending/bulk` are unchanged.
- Egress events and the `egress` SSE topic now carry `box_id` and `service_id`.
  Builder traffic shows as project `__image_build__`. It is never queued, so it
  never appears in pending.

## 4. Services (WP3)

`GET /api/services?project=` returns:

```
{services: [row], services_lan_ip: "" | "<ip>", services_lan_ip_configured: str,
 lan_error: str|null, relays: [...]}
```

A row is:

```
{id, project_slug, name, description, command: [argv], workdir, files, ports:
 [{port, protocol, purpose, expose: "none"|"host"|"lan"}], restart, egress_hosts,
 env, reason, artifact_sha256, definition_sha256,
 placement: "per_service"|"per_project"|"shared", expose_ports: [{port, bind}],
 status: "pending"|"approved"|"rejected"|"revoked"|"superseded",
 desired_state: "running"|"stopped", supersedes_id, last_reported_at,
 created_at, decided_at, decided_by, decision_note, requested_by, conversation_id,
 box_id, state: "running"|"stopped"|"failed"|"unreported", error}
```

- `GET /api/services/{id}` returns the row plus `{diff: str|null, relays}`.
- `POST /api/services/{id}/approve {acknowledge: true, placement, expose_ports: [{port, bind}]}`
  - `placement` is REQUIRED (422 without it). `expose_ports` is REQUIRED and may be `[]`.
  - `bind` is `"loopback"|"lan"`. The aliases `"host"` (loopback) and `"none"` (dropped) are accepted.
  - `lan` gives 409 unless `services_lan_ip` is non-empty.
- `POST /api/services/{id}/reject {reason}`
- `POST /api/services/{id}/start` and `POST /api/services/{id}/stop` take no body.
- `POST /api/services/{id}/revoke {confirm: true, delete_data: bool}` returns
  the row plus `data_deleted`.
- `GET /api/services/{id}/logs?lines=` returns `{service_id, untrusted: true, text}`.
- A stopped service's `egress_hosts` are closed. Egress is allowed only while
  its `desired_state` is `running`.

`/persist` retirement:

- `GET /api/projects/{slug}/persist` returns
  `{..., retired: true, imported_at, delete_after, import: {state: "pending"|"done"|"failed", ...} | null}`.
- `POST /api/projects/{slug}/persist/import {confirm: true}`
- `PUT /api/projects/{slug}/persist {approved: false, delete_disk: true}` works.
  `{approved: true}` gives 409, because approvals are frozen.

## 5. Packages and images (WP5)

- `GET /api/packages?status=` returns `{packages: [row]}`. A row is:
  ```
  {id, project_slug, source: "agent"|"operator", manager: "apt"|"pip"|"npm", package,
   version_req, resolved_version, integrity, requested_command, canonical_command,
   reason, conversation_id, status: "pending"|"approved"|"rejected"|"building"|
   "built"|"failed"|"removed", target_variant, decided_by, decided_at,
   built_version, created_at,
   variant_used_by: [slugs], variant_used_by_detail: {all, direct, via: {variant: [slugs]}},
   card: "installs into `<variant>` — used by: a, b, c"}
  ```
- `POST /api/packages` adds a package as the operator:
  `{manager?, package?, version?, packages?: [{manager, package, version}], reason, target_variant?, new_variant?, from?}`.
  It returns `{packages: [rows], skipped: [{package, error}], target_variant}`.
- `POST /api/packages/resolve` runs a dry-run for every pending row (in a builder box).
- `POST /api/packages/{id}/approve {acknowledge: true, target_variant?, build?: true}`
  returns the row plus `{variant_used_by, build_started}`.
- `POST /api/packages/{id}/reject {reason}` and `POST /api/packages/{id}/remove`.
- `GET /api/vm/images` returns:
  ```
  {variants: [{name, from, builtin, recipe, recipe_sha256, min_mem_mb, layer_packages,
               needs_build, used_by: [slugs], versions: [{version, base_version,
               size_bytes, built_at, status, active, recipe_sha256, in_use_by: [box ids]}]}],
   build: {running, variant, mode, phase, log_tail: [str]}}
  ```
- `POST /api/vm/images {name, from, packages: [{manager, package, version}]}` returns
  `{name, from, recipe_sha256, ...}`.
- `POST /api/vm/images/{variant}/build {confirm: true}` returns `{started: true, variant}`.
- `GET /api/vm/images/{variant}/dockerfile` returns `{variant, recipe_sha256, dockerfile}`.
- SSE topic `vm-images` sends `{type: "image_build", phase: "boot"|"run"|"log"|"poweroff"|..., variant, box?, line?}`.

## 6. Processes (WP4)

- `GET /api/vm/processes[?box=<id>]` returns `{enabled, boxes: [row]}`. With the
  flag off it returns `{enabled: false, boxes: []}`. An unknown `?box=` gives 404.
- A row is:
  ```
  {box_id, kind, project, reported_at, stale, error: str|null,
   baseline: "image"|"builtin", truncated: bool,
   totals: {procs, unexpected, conns, guest_bytes_out, guest_bytes_in,
            host_bytes_out, host_bytes_in} | null,
   orphan_conns: [conn],        # host-seen traffic no reported process owns: show RED
   tree: [{pid, ppid, user, exe, cmd, unit, service_id,
           tag: "service"|"unexpected"|"run_code", rss, cpu_pct, started,
           conns: [{proto, dir: "out"|"in", laddr, lport, raddr, rport, host, state,
                    guest_bytes_out, guest_bytes_in, host_bytes_out, host_bytes_in,
                    verified}],
           children: [...]}]}
  ```
- SSE topic `procs` sends updates for ONE BOX at a time:
  - `{type: "stream_open"}`
  - `{type: "box_procs", box: <row>}`: replace the row by `box_id`, about every 5 s.
  - `{type: "box_procs_changed", box_id}`: the row was over 256 KiB, so refetch `?box=<id>`.
  - `{type: "box_gone", box_id}`: drop the row.
- Service boxes are covered too: svcd answers the same `ps` request.
- The baseline comes from the image build (`<image>.baseline.json`, converted
  from its enabled units). Before any image baseline exists it is the built-in set (`baseline: "builtin"`).

## 7. Security events the UIs should know

These kinds are never auto-handled by the reviewer, whatever a profile's `auto_handle` says:

- `service_*` and `svc_unreported`
- `package_*` and `image_variant_built`
- `unexpected_process` and `proc_report_mismatch`
- `profile_changed` and `profiles_migrated`
- `docker_weak_isolation`, `docker_hardening_refused` and `docker_socket_refused`
- `persist_imported` and `persist_disk_deleted`

Plus the existing `egress_anomaly`, `host_cut` and `secret_leak`.

`box_cap_refused` is informational.
