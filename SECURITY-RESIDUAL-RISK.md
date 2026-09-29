# Residual-risk register — monitored-egress containment

What the architecture is, what it actually buys, and — the point of this
document — what it does **not** cover. Written to be read by someone deciding
whether to trust the agent with a new capability. Last updated 2026-07-19,
covering the monitored-egress build (Layers 1–6; deploy separation / Layer 7 is
out of scope), amended 2026-07-20 for the **staging-quarantine removal** (operator decision: writes land live; git is the review/undo surface), and 2026-09-23 for the **removal of Computer Use and Cloudflare Access** (operator decision: LAN-first; see #12). Amended 2026-09-24 for **computer use rebuilt as `jav3-desk`** (see #15) and 2026-09-29 for **navigation** (element ids, description-based clicking, grounding; see #15, #18-#21). **This supersedes the netless posture** — the guest now has a
real, monitored internet path, a deliberate trade of maximal containment for
watchability and genuine developer autonomy.

## The model in one paragraph

The agent's entire reasoning loop runs inside a disposable KVM guest that has
**no API key, no database, and no secrets**. When `vm_egress` is on, the guest
gets a tap NIC, but its **only route off-box is a host egress proxy**: nftables
drops the whole LAN (RFC1918 + the operator's own servers), forces DNS through a
logged host resolver, redirects 80/443 to the proxy, and drops everything else —
so a process that ignores the proxy fails closed. The proxy applies a per-project
allow/deny policy, **injects `{{secret:X}}` the project is granted** (so the
guest still never holds a key), meters every byte, streams a live feed, and runs
anomaly detection that **auto-cuts** a host on an exfil-shaped signal. File edits
now land **directly on the canonical project files** (the per-file staging
quarantine was removed 2026-07-20): every write still crosses one host chokepoint
(`backend/writes.py`) that refuses path escapes and **refuses any write containing
a real secret value**, and runs the deterministic **diff gates** as an ADVISORY
tripwire — flagged writes land but raise deduped security events. Git is the
review/undo surface (projects are repos from creation with a baseline commit;
commits/pushes remain operator-approved). Untrusted-derived memory
carries a **persisted taint** that keeps it out of binding context. A prompt
injection that reaches code execution lands in a box with no secrets, no LAN, and
a watched, policy-gated, cuttable pipe to the internet.

## What the monitored-egress design genuinely covers

- **Secret theft.** The guest holds no key/DB/secrets (verified: no `*KEY*` env,
  no key file). Authenticated calls get the secret injected at the proxy, on the
  wire, and only for secrets the project is explicitly granted — a compromised
  project cannot enumerate or reach the operator's other keys.
- **LAN pivot.** nftables drops all RFC1918 + link-local; the guest cannot reach
  main/git/test or anything else on the network. Its only host-reachable ports
  are the proxy and the logged DNS resolver.
- **Unwatched egress.** Every outbound connection crosses the proxy (host, bytes,
  timing, verdict) or the DNS log; non-HTTP/S is dropped by nftables. There is no
  silent path out — the "watch what it's doing" property.
- **Fast exfil / beaconing.** High-entropy hosts, volume spikes and beacon
  cadence auto-cut the destination (nftables drop on both guest-forward and
  host-output) and raise a critical alert.
- **Laundered memory.** Memory promotions made after untrusted content is
  consumed are taint-stamped and excluded from binding context. (Canonical-file
  protection CHANGED 2026-07-20: guest edits now apply to the real files at turn
  end — see residual #5/#6.)
- **base_url key-exfil seam.** The model gateway now refuses any guest-supplied
  base_url off the allowlist and sends the real key only to the DeepSeek endpoint.
- **Persistence.** Idle-scrub reboots the single guest from the read-only golden
  image; a monthly timer rebuilds a patched image (highest version auto-activates).
  The sanctioned exception is now operator-approved services in service boxes
  (root reset every boot, only a capped `/srv` persists), residual #14; the
  old `/persist` disk is retired.

## What it does NOT cover — the residual risks

1. **Monitored ≠ prevented: exfil through an allowed host.** This is the central
   new residual and the honest cost of giving the guest a network. Once a host is
   on the allowlist, the guest can POST data to it. Anomaly detection (volume,
   entropy, cadence) + auto-cut cap the **rate and shape** of a leak, not its
   possibility — a **low-and-slow** exfil that stays under the thresholds, to a
   legitimately-allowed host, will succeed. Volume-baseline tuning and the live
   feed are the backstop, plus the operator reviewing what a project may reach.

2. **HTTPS payload is opaque (TLS not intercepted).** The proxy tunnels HTTPS:
   it sees the host, byte volume and timing (enough for policy, cut, and
   beacon/volume anomalies) but **not the payload**. Consequences: (a) content
   inspection and entropy-on-body are unavailable for HTTPS, so an allowed HTTPS
   host is a capped-but-real channel; (b) **secret injection into HTTPS is
   deferred** (needs the baked MITM CA) — authenticated HTTPS the agent's own code
   needs must go through the host tools that substitute host-side. HTTP requests
   are fully intercepted (payload seen, secrets injected).

3. **DNS is a logged side channel.** The guest resolves through the host dnsmasq,
   every query logged and entropy-alertable, but DNS tunnelling to the resolver's
   upstream is still physically possible; logging + high-entropy alerting cap it,
   they don't eliminate it.

4. **Proxy-bypass depends on the nftables floor.** A process that ignores
   `HTTP(S)_PROXY` is dropped by nftables (fails closed) — but that guarantee is
   only as good as the ruleset staying loaded and correct. An nft/kernel misconfig
   or a rule-ordering regression could open a direct path. The ruleset is
   self-replacing and gated on `vm_egress`; treat changes to it as security-
   critical.

5. **Diff gates are heuristic AND now advisory.** The deterministic checks
   (imports, network primitives, entropy blobs, removed logging/asserts) are
   **evadable** (import via `importlib`/`__import__`, a network call via
   `getattr`/`exec`, a blob under the entropy/length floor), and since the
   quarantine removal they no longer block anything except secret-value writes —
   a flagged write lands and alerts. They are a tripwire, not a wall. The
   planned **release gate** (deterministic malware/obfuscation scan + diff
   review before commit) is the intended replacement control and is NOT built.

6. **Malicious output is live immediately.** Everything the agent writes becomes
   a real project file the moment it writes it — the human-review step between
   "agent wrote it" and "it's on disk" is gone by operator decision. What keeps
   this from being remote-execution: canonical files are not executed by the
   host (execution stays guest-only), dashboards render in a sandboxed iframe,
   and commits/pushes still require operator approval. The real exposure is the
   operator themselves running a project file that an injected agent poisoned —
   review `git diff` before running project code you didn't write.

7. **Egress attribution is coarse — and concurrency is now common.** The proxy
   sees raw guest requests with no op_id and attributes them to the operation
   that most recently registered a turn (a process global set by the broker).
   Per-project chats/agent panels (added 2026-07-20) make overlapping top-level
   operations a normal pattern, so cross-project mis-attribution is likelier
   than under the old single-active-project flow. The LAN/secret/cut controls
   do not depend on attribution being exact, but per-project egress POLICY does
   pick the wrong project's allowlist in a race — treat allowlists as
   operator-wide rather than strictly per-project until this is fixed.
   **Update 2026-09-26 (boxes, WP2): fixed for every box of its own, unchanged
   for the shared box.** With `vm_boxes_enabled`, the proxy runs one listener
   per box on that box's host address and attributes by the listener (plus a
   peer-address check behind the nft `iif . saddr/daddr` pinning); project,
   service and builder boxes never consult the context stack. A project whose
   profile sets `separate_box` is therefore policed under exactly its own
   lists. Turns in the shared box still share the stack, and are read once
   per connection now rather than per step. Unattributed shared-box traffic is
   judged by the Default profile only (never a project list, never auto mode)
   and its queue rows need the operator to name the project on approval.
   Approvals now train the PROJECT's own list only, so a mis-attributed
   approval can no longer widen every default project at once.

8. **Bigger host-side surface / confused deputy.** The proxy and model gateway now
   process guest-controlled bytes on the host. A parsing bug in the proxy
   (e.g. host-header vs. absolute-form disagreement) that authorised one host but
   connected to another would be critical; this path is adversarially tested but
   remains attack surface.

9. **Host kernel / QEMU escape.** Unchanged and ultimate: a hypervisor 0-day
   defeats everything above. Mitigation is the minimal device profile (vsock +
   one virtio NIC), no passthrough, and the monthly patched golden image — not
   something the app can guarantee.

10. **Resource / availability.** rlimits + timeouts + the single shared guest mean
    a deliberately heavy `run_code` degrades concurrent turns. An availability
    risk, not a confidentiality one.

11. **LLM triage of the approval queues (added 2026-07-28).** The triage
    reviewer (`backend/reviewer.py`) auto-approves egress hosts and auto-acks
    routine alerts — an operator-directed relaxation of "a human clicks every
    approval." Its inputs (hostnames, alert summaries) are attacker-influenced,
    so a crafted benign-looking host could be steered past it; its verdict is a
    model output. Containment: the reviewer is isolated (no tools, no fetch
    capability, no main-agent context; its only egress is the model API call
    every feature makes), deterministic guardrails outrank it (critical /
    anomaly / secret-leak alerts and cut, anomaly-named or high-entropy hosts
    are never auto-handled), it fails closed (unparseable output acts on
    nothing), and every auto-action is audit-logged and one-click undoable.
    The residual: a plausible-looking exfil host auto-allowed is exactly risk
    #1 with the human click removed — the anomaly detectors and the undo log
    are the backstop.
    **Since 2026-09-26 it is per security profile** (`auto_handle`): the
    reviewer and egress auto mode skip every item of a project whose profile
    has it off; the global switch stays the master kill. The four builtins
    were migrated with it ON so nothing changed on day one — turning it off
    for a sensitive profile is now the operator's lever. "Auto handle all"
    never means the hard-coded never-list: anomaly / cut / secret-leak,
    `service_*`, `svc_*`, `package_*`, `unexpected_process`,
    `proc_report_mismatch`, `profile_changed` and `profiles_migrated` are
    always left for the operator, and unattributed hosts are never
    auto-approved (an approval must name a project).

12. **Computer Use and Cloudflare Access removed (2026-09-23, operator decision).**
    The desktop client, its folder grants and the `computer_*` tools are gone,
    so the host can no longer reach into a paired desktop at all — the
    "desktop reach via grants" residual this entry used to carry no longer
    exists. Jav3 is LAN-first: there is no Cloudflare service token to leak,
    rotate or push, and the music server is reached host-to-host on the LAN.
    What that shifts: the unauthenticated device-login route is now
    reachable by anything that can reach Jav3 on the network, with no Access
    policy in front — see entry 13 and the paste-code row below.

13. **Paste-code login, the CLI, the installers and the web origin (2026-09-23
    adversarial review, five reviewers; fixes by one fixer).** Closed: agent-
    written HTML/SVG served as a same-origin page (`/raw` now serves inert:
    `CSP: sandbox`, nosniff, active types as downloads); CSRF on ~80 cookie
    routes and both WebSockets (one global same-origin gate comparing scheme,
    host and port); valid login codes locked out by other people's misses (a
    valid code now always redeems; only misses are throttled); uvicorn
    trusting X-Forwarded-For from loopback (`--no-proxy-headers`); device
    tokens that never expired, outlived their user, or kept a running turn
    alive after revocation; git Basic auth as an unmetered password oracle;
    an unvalidated `rclone` setting that named any program to execute; and
    restored `.git/hooks` running on the host. What deliberately remains:
    - **Plain-http LAN transport.** Without TLS in front (`cookie_secure`),
      the login code, the device token it buys and every CLI request cross
      the LAN in cleartext; the session cookie is not `Secure` and, like all
      cookies, ignores the port. A passive LAN observer can take a device
      token and use it until it expires (90 days / 30 idle) or is revoked.
      Settings and `jav3 login` both warn. TLS is a deployment choice.
    - **The CLI and its installer are served by the server itself** over the
      same transport (`curl <server>/cli/install.sh | sh`), and the server
      bootstrap is `curl | sh` from a git host with an unpinned default
      branch. Whoever controls the path controls what runs. Python
      requirements are unhashed and loosely pinned (npm uses `npm ci` once a
      lockfile exists).
    - **Single process is a requirement, not an option.** Login codes, the
      miss throttle, live turns and the event bus are in memory; the unit
      pins `--workers 1` and the app refuses `WEB_CONCURRENCY` > 1.
    - **Device tokens are operator-equivalent on the chat surface.** A token
      can list, read and delete every conversation and drive every chat
      tool; the control plane (secrets, VM, egress, backups, devices) stays
      cookie-only. Conversations a device opens carry its `device_id`.
    - **Backups carry the user table.** The DB snapshot uploaded to the
      rclone remote includes the bcrypt password hashes in the clear; the
      secrets toggle encrypts only env / secrets.json / the JWT secret. A
      remote is only as private as its rclone backend. Changing where
      backups go raises a `backup_config_changed` event.
    - **The host-side project runner stays.** `POST /api/projects/{slug}/run`
      executes Python on the HOST, outside the guest. It is now behind the
      global same-origin gate and every run is a `host_run` security event,
      but whether it should exist at all is the operator's call.
    - **Small leftovers.** Device ids are sequential (a device learns how
      many were ever enrolled; the id grants nothing). An already-open
      stream a device attached to a turn it did NOT start keeps receiving
      until that turn ends. `/cli/install.sh` reflects any well-formed Host
      (a rebinding page cannot read it cross-origin and gets no cookie).
      The JWT signing secret on any box that ran the SPA-traversal build
      should be treated as possibly exposed; rotating it (logging everyone
      out) is the operator's call.

14. **Approved services in service boxes (replaces `/persist`, 2026-09-26).**
    The one sanctioned way for anything to outlive a turn. The agent files a
    `service_request` (argv, env, ports, egress hosts, files); nothing starts
    until the operator approves it over the cookie-only API
    (`POST /api/services/{id}/approve` with `acknowledge`, an explicit
    `placement` per_service|per_project|shared and an explicit `expose_ports`;
    `service_approved` event; the reviewer never decides one). What is approved
    is immutable: the HOST snapshots the files from the canonical project tree
    (never from a guest) into a deterministic, sha256-named tar, refuses any
    file, argv or env value carrying a secret value or a `{{secret:`
    placeholder, and re-hashes the tar before every boot (a mismatch is
    refused); a code change is a new request, shown as a diff. It runs in a
    *service box*: its own CID/tap/subnet/proxy listener, no workspace, no op
    tokens, no model or broker access (gateway-gated by CID). The root disk is
    a fresh overlay every boot (the host deletes any stale overlay first) and
    the host re-applies the definitions each boot, so only `/srv` persists: a
    capped qcow2 mounted `nodev,nosuid,noexec`, bound in as each unit's
    StateDirectory. `svcd` runs each service as a transient unit with
    DynamicUser, ProtectSystem=strict, NoNewPrivileges, no capabilities,
    INET/UNIX sockets only (no AF_VSOCK, so a service cannot reach svcd or the
    gateway), a memory cap and read-only code. Egress is deny-by-default
    whatever the profile says: only the running services' approved
    `egress_hosts` minus the profile/project deny lists, with no auto mode and no
    queue training. Revoke = destroy the box (killing QEMU is the authoritative
    stop). A box that stops answering raises `svc_unreported` and is restarted
    with backoff. What remains:
    - **Approved code can be malicious.** The agent wrote it; the operator's
      review of the snapshot is the control. `/srv` state is writable and an
      interpreter ignores `noexec`, so a compromised service keeps what it
      stores in its own StateDirectory across reboots.
    - **Low-and-slow exfil to an approved host** (#1), now from a process that
      runs unattended for days.
    - **Placement is the blast radius.** `per_project` puts a project's
      services in one box (shared kernel, shared egress allow list); `shared`
      puts every shared-placement service of every project together, the
      co-residence the design otherwise avoids. The operator chooses it per
      service; it is stored and shown with the service.
    - **Unit hardening is guest-enforced.** A kernel or systemd escape inside
      the box gets box root: still no secrets, no model, no other box, and the
      root resets on the next boot, but `/srv` and the box's egress are its.
    - **svcd's reports are claims.** State, logs and process data come from a
      box that may be compromised; host truth is the proxy log, the relay
      counters and whether it answers. Logs are untrusted (`service_logs`
      taints the turn) and secret-scrubbed.
    - **Integration pending (2026-09-26):** the box runtime (WP1) and the
      proxy's per-box listener and `services.egress_decide` hook (WP2) must land
      before any of this runs; until then `vm_boxes_enabled` is off and
      services cannot be filed.

14b. **Inbound service ports (2026-09-26).** Nothing reaches a service box by
    default. An exposure is a separate operator choice at approval, only for a
    port the agent asked to expose and no wider than it asked. It is a host-side
    relay (`backend/vm/portfwd.py`) that reaches the service through svcd's
    tunnel over the box transport: no network path into the box, and svcd
    tunnels only to ports the host exposed. Each connection is metered into
    `service_port_events`, with at most 32 concurrent connections and an idle
    timeout. `loopback` binds 127.0.0.1. `lan` binds ONLY `services_lan_ip`, a
    dedicated second address added by `vm/net/services_lan_ip.sh` (label
    `<iface>:jsvc` or macvlan `jsvc0`; the script also drops Jav3's UI port on
    it). Empty = no LAN exposure. At approval and at every bind, the relay
    refuses an address that is loopback, wildcard, public, inside 10.201/16,
    any other host address, the default-route source, or an IP listed as
    Jav3's. Cookies ignore ports (#13), so a service on Jav3's own origin would
    receive the session cookie. What remains: the operator's browser renders
    attacker-authored pages from the services' origin (phishing-shaped but not
    cookie-bearing); anything on the LAN can reach an exposed port, so the
    service must do its own auth; and the relay is new host code handling
    untrusted bytes (it only copies them).

    **Legacy `/persist` (retired 2026-09-26; notes kept until every project has
    imported).** New approvals are refused (409). Projects approved before
    keep attaching until the operator imports or revokes. Import is one click
    (`POST /api/projects/{slug}/persist/import`): approval off, the old disk
    plugged READ-ONLY into a fresh service box to be copied, the host keeps
    the tar (never extracts it), services see the result read-only at
    `/persist`, and the disk is deleted `persist_retire_days` (30) later. How
    the legacy disk worked: a project the operator approved
    gets a sparse qcow2 at `<vm_dir>/persist/<slug>.qcow2`, hot-plugged into the
    guest over QMP and mounted at `/persist` only while one of that project's
    top-level, non-incognito turns is live. It is a deliberate hole in "nothing
    survives the guest": **a place for an implant to wait for the next
    session.** Controls: per-project scope (one disk per slug, one project
    attached at a time, never for incognito or nested turns); a hard virtual-
    size cap (`vm_persist_max_mb`, oversize or backing-file images refused);
    `noexec,nodev,nosuid`; the host never reads the disk into any prompt or
    executes anything from it; the moment a turn is tainted (web read, peer
    message) the disk is unplugged and re-plugged **read-only at the QEMU block
    layer** before the tainted result reaches the guest, so nothing written after
    reading attacker-authorable text lands; a guest that will not release the
    device is torn down (`persist_unplug_failed` event); revoke stops the next
    attach and can delete the disk, and a project purge deletes it. What remains:
    - **`noexec` is a speed bump.** It stops `./implant`, not
      `python3 /persist/implant.py`, and guest root can remount. The real
      boundary is that nothing auto-runs from `/persist` — an implant needs a
      later turn to choose to run it, which is the same prompt-injection
      problem as residual #5/#6, now with memory across sessions.
    - **Writes before taint are trusted.** A turn that has not yet read the web
      writes freely; a malicious *project file* (pushed into the guest every
      turn, attacker-reachable via git or a prior tainted write that landed)
      is not taint, so content derived from it can persist. With
      `JARVIS_VM_EGRESS` on, in-guest network reads by `run_code` go through
      the egress proxy, not the broker, and **do not taint** — `/persist`
      stays writable after them.
    - **Guest-global while mounted.** A mount is visible to every process in
      the guest. A concurrent incognito turn, or another project's turn
      running code in the same guest, can read (and, before taint, write) the
      holder's `/persist` — the same property the pushed workspaces already
      have. Scoping is by attach time, not by process.
    - **The guest kernel parses a filesystem the guest wrote.** A crafted ext4
      image could attack the guest's ext4 driver on the next mount — but the
      writer was already guest root. The host only ever handles the qcow2
      container (QEMU's parser, as for the overlay) and never mounts the disk.
    - **Taint is shared.** The read-only switch covers the hold, so one tainted
      turn makes `/persist` read-only for every concurrent turn of that
      project until the last one ends (fail-safe direction).

15. **Computer use, rebuilt as `jav3-desk` (2026-09-24).** A client on the
    operator's computer (`clients/jav3-desk`) holds one outbound WebSocket to
    `/api/desk/ws` with a `desk`-scoped device token; the agent drives it with
    host-side `desk_*` tools, so every gate is host-side in `backend/desk.py`
    and the guest never learns the token. What is closed: a desk token opens
    only the desk socket (chat refuses it) and a CLI token is refused there —
    so a leaked CLI token cannot pose as a desk and feed forged screenshots,
    and a desk cannot start turns. Grants (screen / input / shell) are per
    computer, server-side, and set only from cookie routes; the client's own
    ceiling can narrow them and never be widened — shell stays off until
    `jav3-desk allow-shell` is run at that computer. Input is refused without
    a screenshot of that computer from this turn under 60 s old, with
    coordinates bounded by it; 10 input/s, 2 screenshots/s, one shell at a
    time; every action is audited (typed text as length + digest); typing a
    stored secret's value is refused; every desk result taints the turn, and
    a tainted turn loses "trusted" shell (off-allowlist commands go back to an
    in-page ask with a 60 s timeout). Stop turns every grant off and kills the
    session; revoking the token drops it. What deliberately remains:
    - **Screens are an injection channel into a turn that can act.** A page,
      a chat window or a document on screen can carry instructions. Taint and
      the shell downgrade contain the shell; clicks and keystrokes inside the
      granted Input are not individually approved. Input on is trust in the
      model's judgement against whatever it reads on that screen.
    - **Input is shell by other means.** With Input granted the agent can open
      a terminal and type into it, which is a shell with none of the shell
      gates. The client refuses typed text naming its own controls (so input
      cannot type its way to `allow-shell`) and offers no terminal in its
      default app list, but a terminal reached by clicking is reachable. Grant
      Input only while watching, or for a machine whose shell you would grant.
    - **The allowlist runs argv, not a sandbox.** An allowlisted `git *` can
      still run `git -c core.pager=… log`; patterns are per argument and the
      command runs with shell=False, but a permissive pattern is permissive.
    - **Trust in the server for approvals.** The client enforces its ceiling
      but cannot tell an operator-approved shell command from one a
      compromised server claims was approved; `allow-shell` is the line a
      compromised server cannot cross, `deny-shell` / `panic` the way back.
    - **Wayland trust model.** The rootless backends (grim, wtype, the
      wlroots virtual pointer) work because the compositor trusts every local
      Wayland client; anything else running as the operator can do the same.
      The client adds no new privilege, but it is a long-lived process holding
      a credential that drives the desktop.
    - **Not yet built:** GNOME/KDE portals and `/dev/uinput` (M2). Wayland
      backends and Windows send no element list (clicks there are by
      coordinates). macOS relies on Accessibility and Screen Recording grants
      to the python that runs the client.
    - **Navigation (2026-09-29).** Every screenshot now carries a numbered
      element list from the platform accessibility tree, and `desk_click`
      takes an element id, a description (`target=`, #20) or coordinates;
      `desk_wait` and `desk_drag` are new verbs (screen and input grants).
      An id resolves only against the latest frame of this turn and is
      refused if absent; the point it produces then goes through the same
      bounds check, rate limit, taint and audit as any coordinate. Labels are
      other people's words: control and bidi characters are stripped, they are
      length-capped and JSON-quoted so a label cannot pose as another list
      line, and the result taints the turn. The window group headers and the
      role are not quoted (whitespace is collapsed, so they cannot start a
      new line). A locked screen or sleeping display is refused with one
      sentence rather than photographed. Input is unchanged in kind: still
      the operator's trust in the model against whatever is on screen, now
      with faster, more reliable clicks. `desk_open` offers every installed
      app (#21); a screenshot can leave the host for grounding (#19).

16. **Persistent packages and image variants (boxes, WP5).** Behind
    `vm_boxes_enabled`. An agent may ask (`package_request`) for an apt, pip
    or npm package to persist; the operator approves it into the image
    variant the project's profile uses, and a builder box produces a new
    frozen layer (`layer-<variant>-vN.qcow2`) that every project on that
    variant then boots. What is closed: the name and version are validated
    per manager and the command is host-built, so a request cannot smuggle an
    index, a URL, a VCS ref, a local file or shell syntax; the version and
    integrity are pinned by a dry-run before approval and re-checked at
    build; the builder reaches only package registries through its own
    proxy listener, every fetch logged under `__image_build__`; nothing is
    auto-handled; a running image is never changed. What remains is the
    supply chain itself: an approved package's code (and its dependencies,
    which the registry resolves) runs in every box on the variant. Keep
    approvals for the variant a project actually uses; `main` reaches every
    project on the default profile.
17. **In-box screenshots (WP5).** The `screenshot` tool renders pages and
    GUI apps inside the box and hands the pixels to the model. Remote pages
    taint the turn (gateway `taint_note`) before the image is returned.
    Screens are an injection channel exactly like `web_read` text, and are
    treated the same way.

18. **Browser use, `jav3-browser` (2026-09-26).** A Chromium extension
    (`clients/jav3-browser`) holds one outbound WebSocket to `/api/browser/ws`
    with a `browser`-scoped device token sent in the first frame (a browser
    WebSocket cannot set a header; a URL token gets logged). The route is
    exempt from the cookie same-origin gate because it never reads the cookie
    the extension's socket carries. Host-side `browser_*` tools route through
    `backend/browser.py`. What is closed: the token opens only that socket
    (chat, the desk socket and every Settings route refuse it; desk and CLI
    tokens are refused there). Grants are per browser AND per project (Read;
    Act = click/type), cookie-only, none by default; Stop removes them all.
    The extension acts only in tabs it opened, in its own unfocused window,
    never the operator's; the first action on each new site waits for the
    operator's Allow (60 s, silence = no); every action raises a notification
    with Cancel, which aborts it and pauses access; Pause and Disconnect sit in
    the popup. Verbs are a closed list validated on both sides; click/type need
    a read of that tab from this turn; one action at a time, 5/s; URLs and
    typed text carrying a stored secret's value are refused, as are the Jav3
    server's own hosts, non-http(s) URLs, `user:pass@` URLs, and password/file
    fields. Every result taints the turn like `web_read`; every action is
    audited (typed text as length + digest). What deliberately remains:
    - **It is the operator's browser, outside the egress proxy.** On an
      allowed site the agent acts with the operator's cookies and sessions,
      and a navigation is an unmetered request from the operator's network.
      A tainted turn can still navigate to an allowed site with data in the
      URL; only stored secret values are caught. Per-site consent is the
      boundary: allow sites where acting as you is acceptable.
    - **Pages are an injection channel into a turn that can act.** Act on a
      project is trust in the model against whatever those pages say, as with
      desk Input. Clicks are not individually approved (the notification is
      notice, not a gate; it can be turned off).
    - **Consent is by hostname, not by page.** A site allowed once stays
      allowed until forgotten in the popup; `*.example.com` covers every
      subdomain. A redirect to a new host is asked about on the next action,
      not before the load.
    - **Trust in the extension's code.** Loaded unpacked in developer mode,
      it is not signed and updates only when re-loaded; `<all_urls>` host
      access is needed to read any allowed site. A tampered folder is a
      browser-wide compromise — install it from your own server's zip.
    - **Synthetic input.** Clicks and typing are DOM events (`isTrusted`
      false), not OS input: some sites ignore them, and no debugger
      permission is taken to do better.
    - **Navigation additions (2026-09-29).** `browser_click` also takes x, y
      (pixels of a `browser_screenshot_tab` from this turn under 120 s old,
      refused once the page scrolled or navigated since) and `browser_type`
      may omit the element to type into whatever has focus (it still needs a
      read of the tab from this turn). Both run in the extension under the
      same checks as an id: per-site consent on the tab's current site, the
      Jav3-host refusal, the password/file refusal, a click that lands in an
      iframe is refused (go by id), focus inside a cross-origin frame needs
      that frame's domain allowed; the server still refuses text carrying a
      stored secret's value. Not covered: a page that moves its own layout
      after the screenshot (a late banner, an overlay) receives the click at
      the old coordinates. New verbs: `select`, `hover` and `key` (one key or
      combo) need Act and a fresh read; `back` and `forward` navigate under
      Read like `navigate`. `read_page` prints the page text, then the
      element list (labels JSON-quoted, whitespace collapsed), then, when a
      page has little button markup, a "candidates" block of likely-clickable
      text. The page text is free text, so a page can write lines that look
      like element-list entries into it; the real ids are only what the
      extension reports and an id that is not on the page is refused as
      stale. The turn is tainted before any of it is returned.

19. **Screenshots leave the host for a grounding model (2026-09-29).**
    `desk_click(target=…)` that no element label settles sends the latest
    desk frame (the whole downscaled screenshot, at most 1280 px on its long
    edge, plus the description) through the model gateway to the vision model
    chosen in Settings → Grounding: `JARVIS_GROUNDING_MODEL` if set, else the
    operator's pin, else the winner of the last model-finder run; with none of
    those nothing is sent and the click is refused. The gateway routes it to
    that model's provider with the host-side key and budgets and meters it like
    any model call; the guest sees no image and no key. What is closed: the
    request is made only after the grant, the ceiling, the locked-screen and
    the fresh-frame checks pass, so a refused click sends nothing; the model
    finder itself uses synthetic screens drawn by the server, not the
    operator's; the reply is parsed to a point and nothing else of it reaches
    the agent; the agent supplies only the description (300 characters used)
    and no tool argument reaches the model choice, the provider or the extra
    request fields. What deliberately remains:
    - **Whatever is on screen goes to a third party.** The image is not
      redacted or masked: an open password manager, a mail, a banking page or
      a terminal is sent as pixels. The provider is one of the image-capable
      models the operator switched on, ranked by measured hit rate, then
      price, then latency, so "automatic" can land on a provider the operator
      enabled for chat and never thought of as seeing the screen. The desk
      audit row records the target, model id and confidence of a grounded
      click; there is no separate record of each image sent. Pin a model you
      accept seeing your screen, or leave grounding unset and click by id.
    - **The choice is not re-checked at call time.** The ranking file is read
      as is, without confirming the model is still one of the enabled
      candidates.

20. **Description-based clicking is a confused deputy (2026-09-29).**
    `desk_click(target="the Save button")` is answered first from the element
    list (a unique exact label, or a unique label that contains the text), else
    by the grounding model. Either way the agent trusts something that reads
    attacker-authorable pixels and text to pick the element. What mitigates
    it: the click still passes every gate a coordinate does (grant, fresh
    frame from this turn, bounds, rate limit, taint, audit); the result line
    says how the point was chosen (label, or grounded with model and
    confidence) and the screenshot after it shows what happened; a miss is
    "could not find", not a guess; two label matches fall through to the
    picture. What is not mitigated:
    - **The confidence is reported, not enforced.** Nothing compares it to a
      threshold; a low-confidence point is clicked.
    - **On-screen text can steer the model.** A dialog or page that draws a
      second, look-alike "Save" (or text saying where the real one is)
      competes with the real element for the grounding model's answer, and
      nothing checks the answer against the accessibility tree. The reply
      parser accepts the first JSON object it finds, so a model that quotes
      on-screen JSON before answering can be steered by it.
    - **Label matching is by substring.** A description that is a fragment of
      exactly one label ("OK" in "Book now", "Delete" in "Delete account")
      resolves to that element without a look at the picture, including when
      the control the agent meant is missing from the tree.
    - **The screen may have moved.** A point is resolved on a frame up to 60 s
      old (plus grounding's seconds) and clicked on the live screen; the
      after-shot says what changed, not what was intended.
    Input on is still trust in the model against whatever it reads; targets
    make that reading easier to exploit, not a new grant.

21. **`desk_open` offers every installed app (2026-09-29).** On macOS every
    `.app` in the usual folders is offered, on Linux the browsers/editors on
    PATH plus visible `.desktop` entries. The client drops a fixed set of
    terminals and script runners by name (Terminal, iTerm, Warp, Alacritty,
    kitty, WezTerm, Ghostty, Hyper, Tabby, Script Editor, Automator, Shortcuts,
    the common Linux terminal emulators), any name containing "terminal", and
    `.desktop` entries marked `Terminal=true` or in the TerminalEmulator
    category. The server accepts only names the client sent in its hello,
    matching a fixed name pattern; a URL must be http(s). What deliberately
    remains:
    - **The exclusion is by name, and is friction, not a wall.** Anything
      else that runs commands is offered: editors and IDEs with an integrated
      terminal (VS Code, Xcode; `code` is in the Linux default list), IDLE,
      Emacs, System Settings, Keychain Access. And with Input granted the
      agent can reach a terminal without `desk_open` at all, by keystroke
      (Spotlight, a launcher): the client's key deny list is a few
      compositor-exit combos. This is the "Input is shell by other means"
      residual of #15, not a new one; the wider list makes it quicker.
    - **`desk-apps.json` overrides the list as is.** When that file exists in
      the client's config directory (mode 0700) its entries are taken
      unfiltered, so an entry named "Terminal" would be offered. Only someone
      with write access to that directory can add one; the file is read when
      the client starts.
    - **Opening an app is not gated per app.** The grant is Input; there is
      no per-app approval, and the after-screenshot is the only report of what
      launched.

## Residual-risk register (Certiv artifact)

| Threat | Impact | Residual | After-controls posture |
|---|---|---|---|
| Exfil via allowed host (HTTP/S) | High | **Medium** | Policy + volume/entropy/cadence anomaly + auto-cut cap rate & shape; low-and-slow within limits is the residual. **The primary new risk.** |
| HTTPS payload exfil / no injection | High | Medium | Host/bytes/cadence still watched + cuttable; payload opaque until MITM lands. Authenticated HTTPS via host tools. |
| DNS covert channel | Medium | Medium | Forced through logged host resolver + entropy alert; tunnelling physically possible. |
| Memory poisoning / laundering | Critical | Low–Med | Persisted taint + static approved:false keep it out of binding context; semantic influence on tainted context remains. |
| Generated-code backdoor | Critical | **Medium-High** | Advisory gates + git history only — no pre-landing human review since 2026-07-20; execution stays guest-only and commits stay gated. Release gate (planned) is the compensating control. |
| Secret exposure | Critical | Very Low | No secrets in guest; wire injection AND web_read substitution are grant-scoped per project (profile `secrets` + project grants, a project revoke wins; the web path ignored grants until 2026-09-26); service boxes never get injection; key never crosses to a non-DeepSeek endpoint. |
| LAN pivot | High | Very Low | nftables drops all RFC1918 + operator servers; guest reaches only host proxy/DNS. Holds for every project without LAN access (the default); the next row covers one that has it on. |
| Per-project LAN access (box -> LAN through the proxy, `backend/lanaccess.py`) | High | **Medium** when on / Very Low off | Off by default, per project, operator-only (cookie API; every enable, disable or list change is a `lan_access_changed` security event). Only listed RFC1918 IPv4 targets (CIDR, address or name, optionally :port). The guest still has no L3 route to the LAN (nft unchanged); the proxy dials from the host. The name is resolved once host-side, every answer must be listed, and the proxy connects to the address it judged (no rebinding between check and dial). Refused at save AND at connect, even inside a listed CIDR: the Jav3 host (all its interface addresses and names, the services' LAN alias, host-internal bridge networks), 127/8, 0/8, 169.254/16 incl. cloud metadata, multicast/broadcast, the box network 10.201/16 (gateway, proxy), public addresses and IPv6. Cut, network-off and deny lists still win; service and builder boxes never get it; secret injection to a LAN target needs a key explicitly host-bound to it (an unbound grant is never sent). Every connection is an `egress_events` row with a `LAN:` reason and passes the anomaly check. Residual = whatever the listed devices expose: a prompt-injected agent can drive their APIs (Home Assistant can unlock doors), exploit unpatched LAN services, and read data to exfiltrate through the project's allowed hosts; HTTPS to a LAN device is a tunnel, so its payload is opaque. Prefer single host:port entries to whole ranges. |
| Hypervisor / kernel escape | Critical | Low | No passthrough, minimal devices, monthly patched image; unpatched-CVE window only. |
| Persistence | High | Very Low (turn guests) | Ephemeral guest + idle scrub + versioned rebuild; nukeable at any time. What persists now depends on approved services (next row). |
| Approved services (service boxes; replaces `/persist`) | High | **Medium** | Operator-only approval (cookie, acknowledge, explicit placement + exposure, never auto-handled) of a host-snapshotted, content-hashed artifact; no secrets; own box with no model/broker/workspace; root reset every boot, only a capped `nodev,nosuid,noexec` `/srv` persists; hardened DynamicUser units; deny-by-default egress on approved hosts only; revoke = kill the box; unreported box alerts. `/persist` frozen, imported, deleted 30 days after import. Residual = approved agent-written code can be malicious, `/srv` state survives, low-and-slow exfil to approved hosts, placement sets co-residence. |
| Inbound service ports | Medium | **Low–Medium** | Off by default; per-port operator choice no wider than requested; relay through svcd's tunnel (no network path into the box), metered; loopback, or LAN only on a dedicated `services_lan_ip` that is refused if it is any address Jav3 answers on. Residual = the operator's browser renders attacker pages from the services' origin; LAN clients can reach an exposed port. |
| Persistent package catalogue / image variants | Critical | **Medium-High** | Structured `package_request` only (per-manager name/version grammar; URLs, paths, `git+`, index/registry flags and shell syntax refused); the host builds the canonical argv and never runs the agent's string; a dry-run pins version + integrity (apt SHA256, pip sha256, npm dist.integrity) before the card can be approved, and the build re-resolves and refuses a changed integrity; never auto-handled; builds only in a builder box whose egress is the registry-only `__image_build__` profile; a new frozen layer version per change, never a mutated image; the card names every project on the variant (and on variants built from it). Residual = an approved malicious or typosquatted package (or its install script, run as root in the builder) lands in every box on that variant, and pip/npm dependencies are resolved by the registry, not pinned. Same class as the generated-code backdoor. |
| In-box screenshots | Medium | Low | In-guest only (`desktop` variant), chromium through the box's egress proxy, app mode on a private Xvfb with no TCP listener; a non-loopback URL, or an app that opened any proxy connection during the run (TIME_WAIT included), taints the turn through the gateway `taint_note` op before the image is returned, and the image is withheld if the note fails. Residual = untrusted pixels in context (prompt injection by image), an app that reaches remote content only via a process started before the capture window, and whatever a response echoes back on screen. |
| Egress mis-attribution | Low | **Medium** (shared box) / Very Low (own box) | Boxes of their own are attributed by their per-box proxy listener (nft-pinned + peer check), never the turn stack. The shared box still races (#7); approvals write only the project's own list, and unattributed traffic gets the Default profile only. Core cut/secret controls unaffected. |
| Triage reviewer mis-allow | High | Medium | Isolated no-tools/no-fetch judge; guardrails outrank it; fail-closed parse; audited + undoable; per-profile `auto_handle` (off = never touched). Residual = risk #1 without the human click. |
| Security profiles and per-profile auto-handle | High | Low–Medium | A profile change is one high-impact click (allow-by-default, network on, secrets for every project under it). Cookie-only API; every create/edit/delete/assign is a `profile_changed` event with a field diff (critical when it widens the default or turns the network on); builtins cannot be deleted or renamed; placement and runtime must be chosen explicitly. The hard never-list cannot be overridden by `auto_handle`. The migration from per-project modes was one transaction with identical verdicts (`profiles_migrated`). Residual = operator error. |
| Paste-code device login (unauthenticated redeem route) | High | Low | A logged-in session mints a 256-bit, single-use, 10-minute code (stored hashed; cancellable from Settings); the redeem route is reachable by anything on the LAN. Valid codes always redeem; misses are throttled per peer and globally on the TCP peer (no proxy headers). Tokens are hashed, revocable, expire (90 days / 30 idle) and die with their user; revoking stops the token's running turns. Residual = a code or token captured in transit on plain http. |
| Plain-http LAN transport | High | **Medium** | Code, device token and session cookie travel in cleartext unless TLS is put in front (`cookie_secure`). Warned in Settings and the CLI; token lifetime bounds a capture. |
| CLI / installer delivery (`curl \| sh`) | High | Medium | Served by the server over its own transport, bootstrap from an unpinned branch; truncation-safe scripts, pinned httpx, `npm ci` with a lockfile. Unhashed Python requirements remain. |
| Web origin (CSRF / agent HTML) | Critical | Low | One global scheme+host+port same-origin gate for every cookie state change and WebSocket; agent files served sandboxed/inert. Residual = a missing-Origin non-browser client holding the cookie. |
| Host-side project runner | Critical | Medium | Executes on the host by design; same-origin gated and audited per run. Existence is the operator's decision. |
| Backup contents and destination | High | Medium | DB snapshot (incl. bcrypt hashes) uploaded unencrypted; secrets only through rclone crypt; destination changes raise a security event; rclone binary constrained; git hooks never restored. |
| Computer use (`jav3-desk`) | Critical | **Medium** | Desk-scoped token, server-side per-computer grants under a client ceiling (shell off until allowed at the keyboard), screenshot-before-input, rate limits, audit, taint with shell falling back to asking; since 2026-09-29 also element ids resolved only against this turn's frame, clicks by description or coordinates through the same gates (#20), locked or asleep screens refused. Residual = on-screen prompt injection steering granted Input, and Input reaching a terminal., a wider `desk_open` app list whose terminal exclusion is by name (#21), and the live screen having moved since the frame a point was resolved on. |
| Browser use (`jav3-browser`) | Critical | **Medium** | Browser-scoped token (first-frame, socket only), per-browser per-project Read/Act grants (none by default), own unfocused window and own tabs only, per-site consent in the extension, action notification with Cancel, Pause, closed verb list checked both sides, read-before-act, secret-value and Jav3-host refusals (also for clicks by x, y and typing into the focused element, under the same per-site and cross-origin-frame consent), taint like web_read, audit. Residual = acting with the operator's sessions on allowed sites outside the egress proxy, page prompt injection steering granted Act, host-level (not page-level) consent., a page moving its layout after the screenshot a coordinate click uses, and page text that imitates element-list lines in `read_page`. |
| Screenshots sent to a grounding model provider | High | **Medium** | Only when `desk_click(target=…)` is not settled by an element label, only to a model the operator enabled and pinned or ranked in Settings → Grounding, only after grant / ceiling / fresh-frame / unlocked checks; via the model gateway (host-side key, budgeted, metered); the model finder uses synthetic screens; the agent cannot choose the model, provider or request fields; the desk audit row keeps target, model and confidence. Residual = the whole screenshot, unredacted, reaches a third-party provider (automatic mode picks by hit rate, price and latency, not by trust), no per-image audit row, no re-check that the ranked model is still one the operator wants. |
| Description-based clicking (`desk_click target=`) | High | **Medium** | Same gates as any coordinate click; label match first, unique only; "could not find" instead of a guess; the result says how the point was chosen and returns the after-screenshot; taint. Residual = on-screen text can steer the grounding model to a look-alike element; confidence is shown, never a threshold; label matching is by unique substring; the grounded point is not cross-checked against the accessibility tree; the screen may change between frame and click. |
| Wider `desk_open` app list | Medium | Low–Medium | Server accepts only client-reported names and http(s) URLs; client drops terminals and script runners by name and terminal-flagged `.desktop` entries; shell has its own grants. Residual = the exclusion is by name (IDEs with terminals, Emacs, IDLE, System Settings are offered), a keystroke reaches a terminal anyway with Input, and `desk-apps.json` entries are taken unfiltered. |
| `/local` chats (agent file + shell tools on the `jav3` client's machine) | Critical | **Medium** | Opt-in per chat at the client; the server can only ask over the turn's stream, the client executes. Writes, edits and commands wait for y / a (always, per kind, this session) / n at that keyboard; reads never ask and may reach any path the operator's user can. Only the actor that opened the chat may answer a call; unanswered calls time out (15 min) and die with stop; args carrying a stored secret are refused; results are capped, secret-scrubbed and taint the turn. Residual = a prompt-injected turn reading local files the operator never meant to share (they go to the model provider), and "always" for shell turning every later command in that session into an unattended one. |
| Permission modes (`yolo` / `auto` / `ask` per chat, permissions.py) and `ask_user` (operator_ask.py) | Low | **Low** | An operator control, not a boundary: the VM, the write chokepoint, diff gates and git/egress approvals hold in every mode, and `yolo` (the default) is today's behaviour. `auto` sends only the tool name and args to a one-shot judge (fixed prompt, args JSON-quoted between per-call random markers, 5 output tokens, only an exact SAFE runs); args that address the reviewer (verdict words, injection phrasing, markers) skip it and ask; any judge error asks. Always-rules are exact tool + normalised prefix per project: a run_code rule never matches a command with shell chaining/substitution, and none is offered for git remotes/commits or broad heads (rm, curl, sh, python…); revocable in Settings and /security. In-guest writes are gated by the guest asking the host first, so a compromised guest can skip the ask (it could already write its own disposable copy; staging still crosses the host gates). Residual = a judge talked into SAFE by args that avoid the filter, and an operator clicking "always" too broadly. |
| Process telemetry is guest-reported (Security > Persistent, boxes flag) | Medium | **Medium** | The process tree, socket owners and per-socket byte counters come from inside the guest (`procwatch`), so the guest can lie. The host treats every field as untrusted. Replies are capped at 8 MiB with a 3 s timeout. It keeps at most 2048 processes, 4096 sockets and 64 connections per process. Strings are clipped, and control and bidi characters are replaced. ppid cycles cannot hang the tree builder. A box raises at most 10 alerts an hour, and each (exe, unit) alerts once per boot. Host truth comes from the host's own kernel: an established connection from the guest IP that no reported process owns for two polls, or byte counts more than 5% (+64 KiB) apart, raise `proc_report_mismatch`. Proxy `egress_events` (peer_port) give host names, and relay counters give inbound bytes. "Not OS" means not in the image's baseline (exe, unit) set; when no baseline is recorded, a small built-in set is used instead. Residual: a root implant with no network traffic, or one reusing a baseline (exe, unit), can hide from the tree. Short connections that open and close between polls are named but never flagged. Without iproute2 on the host (or on a non-Linux dev machine), the host-truth checks are skipped and connections show as unverified. |
| Host Gitea (`backend/gitea.py`, docs/gitea.md) | High | **Low–Medium** | A new HTTP service on the LAN (default :3000, plain http like Jav3 itself; SSH off, registration off, sign-in required to view, offline mode, pinned + sha256-checked static binary, no package manager). Two API tokens 0600 in the config dir, read host-side only: the operator's (admin; merges, account admin, pushes main) and the `jav3-agent` bot's (`write:repository`). Neither enters a box, argv, `.git/config`, the DB or a tool result (per-command `http.extraheader`; scrubbed errors). The bot is a write collaborator, so Gitea itself would let it push any non-protected branch: the `agent/*`-only rule is enforced host-side (the host builds the name, the model never passes a ref), and `main` is protected with the operator as the only push/merge whitelist entry. Boxes cannot reach Gitea at all (the egress proxy refuses its name and host:port before any policy; the host's own IPs are refused by LAN access). Residual: anyone on the LAN can reach the login page; a stolen admin token is full Gitea admin; the Gitea data dir sits in the state dir unencrypted. |
| Single-process assumption | Medium | Low | In-memory codes/throttle/turns; `--workers 1` + startup refusal of WEB_CONCURRENCY > 1. |

**Closing frame:** the netless design made the room have no phone; this design
gives the room a **monitored, policy-gated, cuttable phone with no address book of
its own** — a deliberate trade for autonomy and observability. The work is not to
trust the agent, but to keep every call it makes watched, scoped, and reversible,
and to keep reviewing what it asks to make real.
