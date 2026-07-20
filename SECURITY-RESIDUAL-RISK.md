# Residual-risk register — monitored-egress containment

What the architecture is, what it actually buys, and — the point of this
document — what it does **not** cover. Written to be read by someone deciding
whether to trust the agent with a new capability. Last updated 2026-07-19,
covering the monitored-egress build (Layers 1–6; deploy separation / Layer 7 is
out of scope). **This supersedes the netless posture** — the guest now has a
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
still land in the host **staging quarantine** behind deterministic **diff gates**
and touch canonical files only on operator approval; untrusted-derived memory
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
- **Canonical/repo corruption + laundered memory.** Guest edits go to a workspace
  copy → host staging behind diff gates; memory promotions made after untrusted
  content is consumed are taint-stamped and excluded from binding context.
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

5. **Diff gates are heuristic.** The deterministic checks (imports, network
   primitives, entropy blobs, secret values, removed logging/asserts) catch the
   obvious and force human review, but are **evadable** (import via `importlib`/
   `__import__`, a network call via `getattr`/`exec`, a blob under the entropy/
   length floor, a value split across lines). They raise the cost of hiding
   malice and gate the trusted baseline; the operator's review of the staged diff
   is the real control, not the grep.

6. **Malicious approved output.** Everything the guest produces becomes real once
   the operator approves it. `run_code` can write a plausible script with a subtle
   backdoor that passes every gate; staging + gates make it **reviewable**, not
   **safe**.

7. **Egress attribution is coarse.** The proxy sees raw guest requests with no
   op_id and attributes them to the operation currently driving the single guest
   (a process global set by the broker). With overlapping top-level operations the
   attribution can be wrong; the single-guest Pi makes true concurrency rare, and
   the LAN/secret/cut controls do not depend on attribution being exact.

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

## Residual-risk register (Certiv artifact)

| Threat | Impact | Residual | After-controls posture |
|---|---|---|---|
| Exfil via allowed host (HTTP/S) | High | **Medium** | Policy + volume/entropy/cadence anomaly + auto-cut cap rate & shape; low-and-slow within limits is the residual. **The primary new risk.** |
| HTTPS payload exfil / no injection | High | Medium | Host/bytes/cadence still watched + cuttable; payload opaque until MITM lands. Authenticated HTTPS via host tools. |
| DNS covert channel | Medium | Medium | Forced through logged host resolver + entropy alert; tunnelling physically possible. |
| Memory poisoning / laundering | Critical | Low–Med | Persisted taint + static approved:false keep it out of binding context; semantic influence on tainted context remains. |
| Generated-code backdoor | Critical | Medium | Deterministic gates (heuristic) + staging + human review; a gate-passing backdoor is possible. |
| Secret exposure | Critical | Very Low | No secrets in guest; wire injection is grant-scoped per project; key never crosses to a non-DeepSeek endpoint. |
| LAN pivot | High | Very Low | nftables drops all RFC1918 + operator servers; guest reaches only host proxy/DNS. |
| Hypervisor / kernel escape | Critical | Low | No passthrough, minimal devices, monthly patched image; unpatched-CVE window only. |
| Persistence | High | Very Low | Ephemeral guest + idle scrub + versioned rebuild; nukeable at any time. |
| Egress mis-attribution | Low | Low | Single-guest serialisation; core controls don't depend on it. |

**Closing frame:** the netless design made the room have no phone; this design
gives the room a **monitored, policy-gated, cuttable phone with no address book of
its own** — a deliberate trade for autonomy and observability. The work is not to
trust the agent, but to keep every call it makes watched, scoped, and reversible,
and to keep reviewing what it asks to make real.
