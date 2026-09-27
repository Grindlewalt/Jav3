# Boxes: the contract every package codes against

Source design: `DESIGN-BOXES.md` (operator's copy, gitignored). Its section 0
(operator decisions) overrides everything, including this file. WP1 owns this
document; a change to anything here is a WP1 contract commit, announced to the
orchestrator before anyone codes against it.

Everything below is behind `settings.vm_boxes_enabled` (default **False**).
With the flag off, the system is today's single shared guest, byte for byte:
no new network, no new ops refused, no new files in the guest package.
The one deliberate exception is the gateway op `taint_note` (section D.3),
which exists with the flag off because it can only ever ADD taint.

Host: a 4 GB Raspberry Pi 4 (about 3 GB available). Size every default for it.

---

## A. Data model (backend/vm/boxes.py)

```python
@dataclass
class Box:
    id: str               # "shared" | "p-<slug>" | "s-<slug>" | "s-<slug>-<service_id>"
                          # | "s-shared" | "b-<variant>-<cid>"
    kind: str             # "shared" | "project" | "service" | "builder"
    project: str | None   # None for shared, builders, and the shared-placement service box
    cid: int              # the box's SLOT; for runtime "kvm" also its vsock CID
    tap: str              # "jvtap0" (shared) | "jvtap<cid>" (kvm) | "jvbr<cid>" (docker bridge)
    host_ip: str          # shared 10.201.0.1 ; others 10.201.<cid>.1
    guest_ip: str         # shared 10.201.0.2 ; others 10.201.<cid>.2
    prefix: int           # shared 24 ; others 30
    mac: str              # shared 52:54:00:12:34:60 ; others 52:54:00:c9:00:<cid hex>
    image: tuple[str, str | None]   # (variant, version or None = the variant's active version)
    mem_mb: int
    cpus: int
    dir: Path             # shared: settings.vm_dir (unchanged) ; others <vm_dir>/boxes/<id>/
    runtime: str = "kvm"  # "kvm" | "docker"
    service_id: int | None = None   # per_service service boxes
    placement: str | None = None    # service boxes: "per_service" | "per_project" | "shared"
    ctl: Any              # runtime controller (lifecycle.GuestVM for kvm; WP8's for docker)
```

CID / slot ranges (settings, inclusive): shared = `vm_guest_cid` (3);
project 10–49; service 50–89; builder 90–127. The slot names the tap and the
/30, so the three identities never disagree.

Box directory (non-shared): `overlay.qcow2`, `efi_vars_run.fd`, `qmp.sock`,
`console.log`; a docker box's sockets live in `<vm_dir>/sock/<cid>/` (section C).

Caps (all reservations count, running or not; the shared box always counts):
`vm_max_boxes` (4, includes shared), `vm_max_project_boxes` (1),
`vm_guest_ram_budget_mb` (2400), which counts each box's real cost: `mem_mb`
plus `vm_kvm_box_overhead_mb` (144, QEMU + firmware, measured) per KVM box
(`budget.ram_mb_overhead_per_kvm_box`). Defaults per kind: shared `vm_memory_mb`
768, project `vm_project_box_mem_mb` 768 (profile `box_mem_mb` overrides),
service 384, builder 1024; variant `desktop` is floored at
`vm_desktop_min_mem_mb` 1280, which the budget then keeps from running beside
another large box. Over a cap: `BoxCapError`, refused, never queued silently.

## B. Python API (import `from backend.vm import boxes`)

| Call | Returns / does |
|---|---|
| `boxes.enabled()` | `settings.vm_boxes_enabled` |
| `boxes.shared()` | the shared Box (always exists) |
| `await boxes.for_project(slug)` | flag off / no slug / profile without `separate_box`: shared. Else the project box (allocated on first use). A profile that asks for a box it cannot get raises `BoxCapError`: never a silent fallback to the shared box |
| `boxes.allocate(kind, *, project, service_id, placement, variant, version, mem_mb, runtime)` | reserve (idempotent per id). `BoxError` / `BoxCapError` |
| `boxes.release(box_id)` | drop a reservation (stop it first). Shared cannot be released |
| `boxes.get(id)`, `by_cid(cid)` (kvm only), `by_host_ip(ip)`, `by_guest_ip(ip)`, `by_ifname(name)`, `all_boxes()` | lookup |
| `boxes.budget()` | `{ram_mb_used, ram_mb_cap, boxes, boxes_cap, project_boxes, project_boxes_cap}` |
| `await boxes.start(box)` / `stop(box)` / `destroy(box, delete_data=False)` | lifecycle; destroy = stop + release + rm dir (+ data deleters) |
| `boxes.controller(box)` | the runtime controller (GuestVM interface below) |
| `boxes.bind_op(op_id, box)` / `op_box(op_id)` | set by guest_turn; the gateway refuses an op arriving from another box |
| `boxes.image_path(box)` | qcow2 the overlay backs on |
| `boxes.status_json(box)` | one `/api/vm/boxes` row |

Extension points (register at import time of your module):

- `boxes.add_hook(async fn(event, box))`: `event` in `box_up` (network ready,
  BEFORE the guest boots; raising fails the start: fail closed) and `box_down`
  (after the guest is gone). **WP2** starts/stops the box's proxy listener here.
- `boxes.register_runtime("docker", factory)`: **WP8**. `factory(box)` returns a
  controller with GuestVM's interface: `running()`, `pid`, `booted_at`,
  `inflight`, `idle_since`, `async acquire()` (boot if needed, wait until the
  run-turn endpoint answers, pin), `release()`, `async boot()`, `async teardown()`.
- `boxes.add_image_resolver(fn(box) -> Path | None)`: **WP5** (variants).
- `boxes.add_data_deleter(async fn(box))`: **WP3** (`/srv` disks).
- `gateway_server.register_package_builder(kind, fn(box) -> bytes)`: **WP3**
  (`service`, from `svc_pkg`), **WP5** (`builder`). Returns a tar.gz;
  the gateway appends `box.json` itself.

Errors: `boxes.BoxError`, `boxes.BoxCapError(BoxError)`.

## C. Transport seam and box.json

`box.transport` is a `Transport`:

- `await t.connect(port) -> socket` host -> guest (non-blocking, connected).
  Ports: 5556 run-turn, 5557 shell, 5558 svcd (`boxes.PORT_*`).
- `t.gateway_endpoint() -> dict` where the guest dials the gateway.
- `t.guest_listen(port) -> dict` where the guest listens.

| runtime | transport | guest -> host | host -> guest | host identity of a caller |
|---|---|---|---|---|
| kvm | `VsockTransport` | AF_VSOCK CID 2 : `vm_vsock_port` (5555, baked in bootstrap) | AF_VSOCK box.cid : port | peer CID from `accept()` -> `boxes.by_cid` |
| docker | `UnixTransport` | AF_UNIX `/run/jav3/gateway.sock` | AF_UNIX `<vm_dir>/sock/<cid>/<port>.sock` | which per-box listener accepted (`gateway.listen_unix(box)`) |

`<vm_dir>/sock/<cid>/` (short: `sun_path` is 108 bytes) is the only host path a docker box ever sees, mounted at
`/run/jav3`. No TCP channel. Never the docker socket.

**box.json** (host-authored, shipped inside the guest package as
`/opt/jarvis/box.json`; only when boxes are enabled):

```json
{"v": 1, "id": "p-alpha", "kind": "project", "project": "alpha", "runtime": "kvm",
 "net": {"guest_ip": "10.201.10.2", "prefix": 30, "gateway": "10.201.10.1",
         "dns": "10.201.10.1", "proxy": "http://10.201.10.1:8443",
         "mac": "52:54:00:c9:00:0a"},
 "gateway": {"transport": "vsock", "cid": 2, "port": 5555},
 "listen": {"runturn": {"transport": "vsock", "port": 5556},
            "shell":   {"transport": "vsock", "port": 5557},
            "svcd":    {"transport": "vsock", "port": 5558}}}
```

Docker: `"gateway": {"transport": "unix", "path": "/run/jav3/gateway.sock"}`,
`"listen": {"runturn": {"transport": "unix", "path": "/run/jav3/5556.sock"}, ...}`.

Guest side: `guest/backend/boxinfo.py` (`load()`, `gateway_connect()`,
`listen(name)`) is the only guest code that knows the transport. Absent
box.json = today's literals (vsock; 10.201.0.2/24 via 10.201.0.1).

## D. Gateway (backend/vm/gateway_server.py)

1. `handle_conn(loop, conn, *, peer_cid=None, box=None)`: the listener passes
   the accepted peer CID (vsock) or the fixed box (unix listener). Flag off:
   no gating. Flag on: the caller's box is `box or boxes.by_cid(peer_cid)`;
   an unknown caller may only `ping`; otherwise the op must be in
   `boxes.GATEWAY_OPS[box.kind]`:

   | kind | allowed ops |
   |---|---|
   | shared, project | ping, get_guest_package, model_call, tool_broker_call, taint_note |
   | service | ping, get_guest_package, svc_report |
   | builder | ping, get_guest_package, build_report |

   Refusal: `{"type":"error","error":"op_not_allowed","message":"<op> is not allowed from a <kind> box"}`.
   An op-bearing request whose `op_id` is bound to a different box:
   `{"type":"error","error":"unknown_op_id",...}` (same shape as a bad token).
2. `get_guest_package` answers with the package for the CALLER's kind
   (shared/project: the turn package; service/builder: the registered builder,
   else `{"type":"error","error":"no_package"}`), plus `box.json`.
3. **`taint_note`** (new): `{"op":"taint_note","op_id","op_token","source":"<short tag>"}`.
   Requires the op token and a registered turn. Calls `broker.mark_tainted`;
   if that newly taints the turn, `persist.on_taint(project)` runs first.
   Reply `{"type":"taint_noted","tainted":true,"newly":bool}`. `source`
   (<= 64 printable chars) is logged, never trusted. **WP5**'s screenshot tool
   sends it from the guest (via `boxinfo.gateway_connect()`).
4. `svc_report` / `build_report` shapes belong to WP3 / WP5; the gateway
   dispatches them to `gateway_server.register_op_handler(op, async fn(loop, conn, req, box))`.

## E. Guest run-turn modes (guest/backend/server.py, port 5556)

Existing: run_turn (no mode), `prime`, `pull`, `persist_mount`, `persist_unmount`.
New: **`{"mode":"ps"}`** -> `{"type":"ps","ok":true,"snapshot":<procwatch.snapshot()>}`
or `{"type":"ps","ok":false,"error":"..."}` when `backend/procwatch.py` is not
in the package. **WP4** owns `procwatch.snapshot()` and its shape; the host
helper is `guest_turn.box_rpc(box, {"mode":"ps"})`.

## F. Bus events

Channel `boxes.BUS_CHAN` = `"vm-boxes"`:
`{"type":"box_up","box":<Box.to_json()>}` and `{"type":"box_down","box":...}`.
`Box.to_json()` = `{id, kind, project, cid, runtime, service_id, placement,
image:{variant,version}, mem_mb, net:{tap,host_ip,guest_ip}}`.

## G. Network (vm/net/)

- Flag off: `net_up.sh up|down` and `jarvis-egress.nft` / `dnsmasq-egress.conf`, unchanged.
- Flag on: `net_up.sh up-boxes|down` loads `jarvis-egress-boxes.nft` (static,
  checked in, equal to `boxnet.render_ruleset()`; golden-file tested) and
  `dnsmasq-egress-boxes.conf` (`interface=jvtap*`, `bind-dynamic`; DNS for
  every box, the one DHCP pin for the shared box).
- Sets in `table inet jarvis_vm`: `guest_taps` (ifname), `tap_addr`
  (ifname . host IP), `tap_src` (ifname . guest IP), `cut_hosts` (unchanged).
- `guest_input` (every ifname in `guest_taps`): IPv6 dropped; a packet whose
  `iifname . ip saddr` is not in `tap_src` or whose `iifname . ip daddr` is not
  in `tap_addr` is dropped BEFORE conntrack's established accept; then only
  DNS 53, the proxy 8443 and ICMP echo reach the host. No DHCP for non-shared
  boxes (they are configured from box.json).
- `forward`: everything from a guest is dropped (boxes cannot reach each other
  or the LAN; the host proxy is the only way out).
- `net_box.sh add|del <jvtapN> <10.201.N.1> <10.201.N.2>` creates/removes the tap
  and its three set elements; `pin|unpin <jvbrN> <10.201.N.1> <10.201.N.2>` only
  the set elements (WP8's bridges). The script refuses any name/address that
  does not derive from the same N (4..254), so a caller cannot pin a LAN address.
- Proxy (WP2): one listener per box on `box.host_ip:8443`, started in the
  `box_up` hook; attribution = the listener's box (`boxes.by_host_ip`), the
  context stack only for the shared box. Record `peer_ip`, `peer_port`,
  `box_id` (and `service_id` for service traffic) on `egress_events`.

## H. Schema (backend/db.py; created by WP1, filled by the owners)

New tables: `security_profiles` (WP2), `services`, `service_port_events` (WP3),
`package_catalogue`, `image_variants`, `image_versions` (WP5). See the column
comments in `backend/db.py` SCHEMA; they are the definitive column list.

- `security_profiles.service_placement` and `.box_runtime` are NOT NULL with
  **no default**: an INSERT that omits either fails (operator decision 0.1 and
  the docker addendum). WP2's migration sets builtins to `per_project` / `kvm`.
- `services.placement` is NOT NULL: the placement actually used, set at filing
  from the profile and changeable in the approval dialog.

New columns: `projects.profile_id` (NULL = builtin `Default`),
`projects.persist_imported_at`, `projects.persist_delete_after`;
`egress_policy.deny_hosts` (`hosts` stays the allow list until WP2's migration,
which owns any rename together with egress.py); `egress_events.peer_ip`,
`.peer_port`, `.box_id`, `.service_id`; `egress_pending.box_id`.

## I. Settings (backend/config.py)

`vm_boxes_enabled`, `vm_max_boxes`, `vm_max_project_boxes`,
`vm_guest_ram_budget_mb`, `vm_project_box_mem_mb`, `vm_service_box_mem_mb`,
`vm_service_box_cpus`, `vm_builder_box_mem_mb`, `vm_desktop_min_mem_mb`,
`vm_builder_timeout_seconds`, `vm_box_cpus`, `vm_cid_{project,service,builder}_{min,max}`,
`vm_box_idle_stop_seconds`, `vm_svcd_port`, `vm_svc_data_max_mb`,
`vm_svc_ping_seconds`, `vm_procwatch_seconds`, `persist_retire_days`,
`services_lan_ip` ("" = no LAN exposure), `docker_enabled`, `docker_bin`,
`docker_image_turn`, `docker_image_svc`, `docker_image_builder`,
`docker_oci_runtime` ("" or "runsc"), `docker_box_mem_mb`, `docker_box_pids`,
`docker_box_cpus`, `docker_tmpfs_mb`.

## J. HTTP API shapes

All control-plane routes are cookie-only (`require_user`), same-origin gated.

### (e) Boxes — WP1

- `GET /api/vm/boxes` -> `{enabled, boxes:[{id, kind, project, cid, runtime,
  service_id, placement, state:"running"|"stopped", image:{variant,version},
  mem_mb, rss_bytes, cpu_pct, uptime_s, inflight,
  disk:{overlay_bytes,data_bytes}, net:{tap,host_ip,guest_ip}}],
  budget:{ram_mb_used, ram_mb_cap, boxes, boxes_cap, project_boxes, project_boxes_cap}}`
- `POST /api/vm/boxes/{id}/start` -> the row. `p-<slug>` may be started
  before it is allocated (operator warm-up). 409 on `BoxCapError`, 404 unknown.
- `POST /api/vm/boxes/{id}/stop` -> the row.
- `POST /api/vm/boxes/{id}/destroy {confirm:true, delete_data:false}` -> `{ok:true}`
  (shared: stop only). 400 without confirm.
- Existing `/api/vm/*` stay as shared-box aliases.

### (e) Images — WP5

- `GET /api/vm/images` -> `{variants:[{name, from, builtin, recipe, recipe_sha256,
  min_mem_mb, used_by:[slugs], versions:[{version, base_version, size_bytes,
  built_at, status, active, in_use_by:[box ids]}]}], build:{running, variant, phase}}`
- `POST /api/vm/images {name, from, packages:[{manager, package, version}]}`
- `POST /api/vm/images/{variant}/build {confirm:true}`; progress on bus `vm-images`.

### (a) Services — WP3

- Tool `service_request` args: `{name, description, command:[argv...], workdir,
  files:[paths/globs], ports:[{port, protocol:"tcp", purpose, expose:"none"|"host"|"lan"}],
  restart:"no"|"on-failure"|"always", egress_hosts:[...], env:{K:V}, reason}`;
  filed, not blocking. Tools `service_status {name?}`, `service_logs {name, lines?}` (untrusted).
- `GET /api/services?project=` -> `{services:[{id, project_slug, name, description,
  command, workdir, files, ports, restart, egress_hosts, env, reason,
  artifact_sha256, placement, expose_ports, status, desired_state, supersedes_id,
  box_id, state:"running"|"stopped"|"failed"|"unreported", last_reported_at,
  created_at, decided_at}]}`
- `GET /api/services/{id}` -> row + `{diff: <unified diff vs supersedes_id's artifact> | null}`
- `POST /api/services/{id}/approve {acknowledge:true, placement, expose_ports:[{port, bind:"loopback"|"lan"}]}`
  (`bind:"lan"` refused while `services_lan_ip` is empty)
- `POST /api/services/{id}/reject {reason}`; `POST /api/services/{id}/start|stop`;
  `POST /api/services/{id}/revoke {confirm:true, delete_data:bool}`;
  `GET /api/services/{id}/logs?lines=`
- `/persist` retirement: `POST /api/projects/{slug}/persist/import {confirm:true}`;
  `PUT /api/projects/{slug}/persist` refuses new approvals.

### (b) Processes — WP4

`GET /api/vm/processes?box=` -> as in the design:
`{boxes:[{box_id, kind, project, reported_at, stale, tree:[{pid, ppid, user, exe,
cmd, unit, service_id, tag:"service"|"unexpected"|"run_code", rss, cpu_pct,
started, conns:[{proto, dir:"out"|"in", laddr, lport, raddr, rport, host, state,
guest_bytes_out, guest_bytes_in, host_bytes_out, host_bytes_in, verified}],
children:[...]}]}]}`. Streams on `/api/events` channel `procs`.

### (c)/(d) Policy and profiles — WP2

- `GET /api/egress/policy/{slug}` -> `{profile:{id,name,default}, project_allow,
  project_deny, effective_allow, effective_deny}`; `PUT {allow:[], deny:[]}`.
- `GET /api/egress/allowlist` groups by project, then profile.
- Decision order in `egress.decide`: cut, project deny, profile deny, project
  allow, profile allow, live auto-allow, profile default.
- `GET /api/profiles` -> `{profiles:[{id, name, builtin, default_verdict,
  network_off, allow_hosts, deny_hosts, secrets, auto_handle, separate_box,
  box_image, box_mem_mb, box_runtime, allow_services, allow_package_requests,
  service_placement, projects:[slugs]}]}`
- `POST /api/profiles` (all fields; `service_placement` and `box_runtime`
  REQUIRED, 422 without), `PUT /api/profiles/{id}`, `DELETE /api/profiles/{id}`
  (builtins 409).
- `PUT /api/projects/{slug}/profile {profile_id}`.

### (f) Packages — WP5

- Tool `package_request` args: `{manager:"apt"|"pip"|"npm", package, version?, install_command, reason}`.
- `GET /api/packages?status=` -> `{packages:[catalogue row + variant_used_by:[slugs]]}`
- `POST /api/packages {manager, package, version?, reason, target_variant}` (operator)
- `POST /api/packages/{id}/approve {acknowledge:true, target_variant}` (the card
  says "installs into `<variant>` — used by: a, b, c");
  `POST /api/packages/{id}/reject {reason}`.

### (g) Screenshot tool — WP5

In-guest tool `screenshot {mode:"url"|"app", url?, command?:[argv], wait_ms<=15000,
width<=1600, height<=1200, full_page?}`; remote content -> gateway `taint_note`.

## K. Security events (security.py kinds)

`box_cap_refused` (info), `profiles_migrated`, `profile_changed`,
`service_requested` (info), `service_approved`, `service_revoked`,
`svc_unreported` (warn), `unexpected_process` (warn; critical in a service box),
`proc_report_mismatch` (warn), `package_requested` (info), `package_approved`,
`package_rejected` (warn), `image_variant_built`. The reviewer never
auto-handles `service_*`, `package_*`, `unexpected_process`, `proc_report_mismatch`.

## L. Docker runtime (WP8) — required hardening

Behind `docker_enabled` and a profile's explicit `box_runtime: "docker"`.

- Rootless Docker, or userns-remap; never a privileged container.
- `--cap-drop ALL`, `--security-opt no-new-privileges`, default seccomp profile
  (or stricter), `--read-only` rootfs, `--tmpfs /tmp:size=<docker_tmpfs_mb>m,noexec,nosuid,nodev`,
  `--pids-limit <docker_box_pids>`, `--memory <mem>m --memory-swap <mem>m`, `--cpus`.
- Mounts: ONLY `<vm_dir>/sock/<cid>` -> `/run/jav3` and the workspace/`/srv` volume.
  Never `/var/run/docker.sock`, never other host paths.
- `--runtime runsc` when `docker_oci_runtime` is set and present.
- Network: one internal network per box (bridge `jvbr<cid>`, subnet
  10.201.<cid>.0/30, `enable_icc=false`, `--internal`), no published ports
  (inbound only through the WP3 relay). The bridge is pinned with
  `net_box.sh pin jvbr<cid> ...`, so the same nft rules apply: the container
  reaches only DNS and its own proxy listener.
- Transport: `UnixTransport` (section C); the gateway's per-box unix listener is
  the caller identity.

## M. Ownership

| WP | Owns |
|---|---|
| 1 | boxes.py, lifecycle.py, gateway_server.py, guest_turn.py, vm_api.py, vm/run_vm.sh, vm/net/*, guest/backend/server.py + boxinfo.py, db.py + config.py (schema/flags) |
| 2 | profiles.py, profiles_api.py, egress.py, egress_api.py, vm/egress_proxy.py, egress_auto.py, reviewer.py, secrets.py, webtools.py, the profiles migration |
| 3 | vm/services.py, services_api.py, vm/svc_pkg.py, vm/portfwd.py, guest/svc/, tools/service_*, vm/broker.py, persist retirement |
| 4 | guest/backend/procwatch.py, vm/procview.py, /api/vm/processes, `procs` |
| 5 | vm/build_base.sh, vm/images/*.recipe, vm/images.py, packages.py + api, tools/package_request, tools/screenshot, vm/guest_pkg.py |
| 6 | frontend |
| 7 | clients/jav3cli/jav3 |
| 8 | the docker runtime driver (registers via `boxes.register_runtime("docker", ...)`) |

## N. WP1 implementation notes (as built)

- The shared box, flag on, is pinned like every other box: it reaches only
  10.201.0.1 (today, flag off, it can reach any host address on 53/8443).
- Per-box taps get no tcpdump ring (RAM); the shared jvtap0 keeps its pcap.
- Non-shared boxes get no DHCP; they configure from box.json. A guest that
  keeps the literal 10.201.0.2 on a box tap is a spoofer and is dropped.
- `/persist` is attached only in the shared box (it is being retired).
- The operator shell (`guest_shell.py`) still reaches the shared box only.
- Sudo: `net_box.sh` runs as `sudo -n bash <repo>/vm/net/net_box.sh <action> ...`.
  A host without blanket NOPASSWD needs, for the app user:
  `<user> ALL=(root) NOPASSWD: /usr/bin/bash <repo>/vm/net/net_box.sh *`
  (plus the existing line for `net_up.sh`, which gains `up-boxes|down-boxes`).
  sudo's env_reset strips JARVIS_*: everything the script needs is in argv.
- A second instance on one host (JARVIS_INSTANCE, or derived from a non-default
  JARVIS_CONFIG_DIR): `net_up.sh <action> <jvtapN> <10.201.N.1> <0|1>
  <jarvis_vm_NAME>` and `net_box.sh ... <jarvis_vm_NAME>` (needs the `*` form
  of both sudoers lines). Its nft table, pid files, `/run` renders, dns log and
  pcaps carry the name; it must move its shared box off jvtap0 / CID 3 (boot
  refuses); `down-boxes` deletes only the taps in its own table's `guest_taps`.
  Its dnsmasq answers on its shared tap only and serves no DHCP; its box taps
  get DNS from the default install's `interface=jvtap*` resolver when one runs.
  The default install passes the bare `net_up.sh <action>` as before.
