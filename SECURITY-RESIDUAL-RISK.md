# Residual-risk register — monitored-egress containment

What the architecture is, what it actually buys, and — the point of this
document — what it does **not** cover. Written to be read by someone deciding
whether to trust the agent with a new capability. Last updated 2026-07-19,
covering the monitored-egress build (Layers 1–6; deploy separation / Layer 7 is
out of scope), amended 2026-07-20 for the **staging-quarantine removal** (operator decision: writes land live; git is the review/undo surface) and 2026-07-30 for the **computer-use folder-ceiling removal** (operator decision: the GUI grant list is authoritative, see risk #12). **This supersedes the netless posture** — the guest now has a
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

12. **Computer-use folder ceiling removed (2026-07-30, operator decision).**
    The desktop client (`clients/computeruse/`) used to treat its `--allow-root`
    launch flags as a hard ceiling: folder grants made in the GUI could only
    narrow them, so a compromised Jarvis host could address nothing the operator
    had not already pointed that client at on its own command line. That is
    gone. The grant list in the GUI is now authoritative and is pushed to the
    client live.

    Why: the ceiling made the Computer use tab dishonest. A folder granted there
    but absent from the launch flags looked accepted and reached nothing, and
    the only cure was stopping the client and re-running set-up with another
    flag — so folders, the setting that changes most, were the one thing the GUI
    could not change. The operator chose usability here explicitly.

    What still holds: grants are created only by a **logged-in operator in the
    GUI**, never by a tool — there is no verb, and no route, that lets the agent
    widen its own reach. The client still resolves every path and refuses
    anything outside the granted list, refuses non-media extensions, and refuses
    a granted path that is not really a directory on that machine. The no-shell
    property is untouched: the wire still carries a closed table of verbs with
    typed params, never a command line.

    The residual: an attacker who gets **host-level control of Jarvis and an
    operator session** can now name any folder on a paired desktop and read
    media filenames out of it (`list`/`find`) or play a file from it. Before,
    that same attacker was confined to the folders already on the client's
    command line. This is a defence-in-depth loss against a compromised host,
    not a widening of the prompt-injection boundary — a compromised *agent*
    still cannot make a grant.

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
| Egress mis-attribution | Low | **Medium** | Concurrent per-project operations are now normal; policy may consult the wrong project's allowlist in a race. Core cut/secret controls unaffected. |
| Triage reviewer mis-allow | High | Medium | Isolated no-tools/no-fetch judge; guardrails outrank it; fail-closed parse; audited + undoable. Residual = risk #1 without the human click. |
| Desktop reach via computer-use grants | Medium | **Medium** | Ceiling removed 2026-07-30 (operator decision): the GUI grant list is authoritative. Grants still need a logged-in operator — no tool can make one — and the client still refuses paths outside them, non-media files, and no-shell stays absolute. Residual = a host-level attacker with a session can name any folder on a paired desktop. |

**Closing frame:** the netless design made the room have no phone; this design
gives the room a **monitored, policy-gated, cuttable phone with no address book of
its own** — a deliberate trade for autonomy and observability. The work is not to
trust the agent, but to keep every call it makes watched, scoped, and reversible,
and to keep reviewing what it asks to make real.
