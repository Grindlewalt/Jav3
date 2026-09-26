# Residual-risk register — monitored-egress containment

What the architecture is, what it actually buys, and — the point of this
document — what it does **not** cover. Written to be read by someone deciding
whether to trust the agent with a new capability. Last updated 2026-07-19,
covering the monitored-egress build (Layers 1–6; deploy separation / Layer 7 is
out of scope), amended 2026-07-20 for the **staging-quarantine removal** (operator decision: writes land live; git is the review/undo surface), and 2026-09-23 for the **removal of Computer Use and Cloudflare Access** (operator decision: LAN-first; see #12). Amended 2026-09-24 for **computer use rebuilt as `jav3-desk`** (see #15). **This supersedes the netless posture** — the guest now has a
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
  The one sanctioned exception is an operator-approved per-project `/persist`
  disk — residual #14.

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

14. **Approved persistence inside the guest (`/persist`, 2026-09-24).** A
    project the operator approves (GUI only: `PUT /api/projects/{slug}/persist`,
    cookie session, explicit acknowledge, a `persist_approved` security event)
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
    - **Not yet built:** GNOME/KDE portals and `/dev/uinput` (M2), and
      accessibility-tree targeting (M4). macOS relies on Accessibility and
      Screen Recording grants to the python that runs the client.

## Residual-risk register (Certiv artifact)

| Threat | Impact | Residual | After-controls posture |
|---|---|---|---|
| Exfil via allowed host (HTTP/S) | High | **Medium** | Policy + volume/entropy/cadence anomaly + auto-cut cap rate & shape; low-and-slow within limits is the residual. **The primary new risk.** |
| HTTPS payload exfil / no injection | High | Medium | Host/bytes/cadence still watched + cuttable; payload opaque until MITM lands. Authenticated HTTPS via host tools. |
| DNS covert channel | Medium | Medium | Forced through logged host resolver + entropy alert; tunnelling physically possible. |
| Memory poisoning / laundering | Critical | Low–Med | Persisted taint + static approved:false keep it out of binding context; semantic influence on tainted context remains. |
| Generated-code backdoor | Critical | **Medium-High** | Advisory gates + git history only — no pre-landing human review since 2026-07-20; execution stays guest-only and commits stay gated. Release gate (planned) is the compensating control. |
| Secret exposure | Critical | Very Low | No secrets in guest; wire injection is grant-scoped per project; key never crosses to a non-DeepSeek endpoint. |
| LAN pivot | High | Very Low | nftables drops all RFC1918 + operator servers; guest reaches only host proxy/DNS. |
| Hypervisor / kernel escape | Critical | Low | No passthrough, minimal devices, monthly patched image; unpatched-CVE window only. |
| Persistence | High | Very Low | Ephemeral guest + idle scrub + versioned rebuild; nukeable at any time. |
| Approved `/persist` disk (implant survives sessions) | High | **Low–Medium** | Opt-in per project by the operator only (cookie GUI + acknowledge + security event); one project attached at a time, never incognito/nested; size-capped; `noexec,nodev,nosuid`; never read into context; taint re-plugs it read-only at the block layer; a guest that won't release it is torn down; revoke/purge delete it. Residual = interpreters ignore noexec, writes before taint (and `run_code` egress reads) are trusted, and a mount is visible guest-wide. |
| Egress mis-attribution | Low | **Medium** | Concurrent per-project operations are now normal; policy may consult the wrong project's allowlist in a race. Core cut/secret controls unaffected. |
| Triage reviewer mis-allow | High | Medium | Isolated no-tools/no-fetch judge; guardrails outrank it; fail-closed parse; audited + undoable. Residual = risk #1 without the human click. |
| Paste-code device login (unauthenticated redeem route) | High | Low | A logged-in session mints a 256-bit, single-use, 10-minute code (stored hashed; cancellable from Settings); the redeem route is reachable by anything on the LAN. Valid codes always redeem; misses are throttled per peer and globally on the TCP peer (no proxy headers). Tokens are hashed, revocable, expire (90 days / 30 idle) and die with their user; revoking stops the token's running turns. Residual = a code or token captured in transit on plain http. |
| Plain-http LAN transport | High | **Medium** | Code, device token and session cookie travel in cleartext unless TLS is put in front (`cookie_secure`). Warned in Settings and the CLI; token lifetime bounds a capture. |
| CLI / installer delivery (`curl \| sh`) | High | Medium | Served by the server over its own transport, bootstrap from an unpinned branch; truncation-safe scripts, pinned httpx, `npm ci` with a lockfile. Unhashed Python requirements remain. |
| Web origin (CSRF / agent HTML) | Critical | Low | One global scheme+host+port same-origin gate for every cookie state change and WebSocket; agent files served sandboxed/inert. Residual = a missing-Origin non-browser client holding the cookie. |
| Host-side project runner | Critical | Medium | Executes on the host by design; same-origin gated and audited per run. Existence is the operator's decision. |
| Backup contents and destination | High | Medium | DB snapshot (incl. bcrypt hashes) uploaded unencrypted; secrets only through rclone crypt; destination changes raise a security event; rclone binary constrained; git hooks never restored. |
| Computer use (`jav3-desk`) | Critical | **Medium** | Desk-scoped token, server-side per-computer grants under a client ceiling (shell off until allowed at the keyboard), screenshot-before-input, rate limits, audit, taint with shell falling back to asking. Residual = on-screen prompt injection steering granted Input, and Input reaching a terminal. |
| `/local` chats (agent file + shell tools on the `jav3` client's machine) | Critical | **Medium** | Opt-in per chat at the client; the server can only ask over the turn's stream, the client executes. Writes, edits and commands wait for y / a (always, per kind, this session) / n at that keyboard; reads never ask and may reach any path the operator's user can. Only the actor that opened the chat may answer a call; unanswered calls time out (15 min) and die with stop; args carrying a stored secret are refused; results are capped, secret-scrubbed and taint the turn. Residual = a prompt-injected turn reading local files the operator never meant to share (they go to the model provider), and "always" for shell turning every later command in that session into an unattended one. |
| Single-process assumption | Medium | Low | In-memory codes/throttle/turns; `--workers 1` + startup refusal of WEB_CONCURRENCY > 1. |

**Closing frame:** the netless design made the room have no phone; this design
gives the room a **monitored, policy-gated, cuttable phone with no address book of
its own** — a deliberate trade for autonomy and observability. The work is not to
trust the agent, but to keep every call it makes watched, scoped, and reversible,
and to keep reviewing what it asks to make real.
