# Design: brain-in-the-VM (M4 stretch)

Status: **design, not built.** This is the "move Jarvis's reasoning into the
guest" item. It is the most security-sensitive architectural change in the
backlog, so it gets a design doc before any code.

## Problem

Today the reasoning loop runs **host-side**. `Model.complete`
(`backend/agent/model.py`) holds the DeepSeek API key and talks to the model
provider directly; the sandbox VM only ever runs *code*, never the agent. The
threat model already assumes "the agent may be compromised" — but a compromised
*host-side* loop sits next to the API key, the DB, the secrets vault, the nft
control plane, and every project's canonical files. The blast radius of a prompt
injection that escalates to code execution on the host is the whole system.

Goal: move the loop into the disposable guest so the host becomes a thin
supervisor. A compromise of the reasoning loop then buys the attacker only the
throwaway VM (nuke = recreate), never the key or the host.

## Non-goals

- Changing the ReAct loop's logic (`loop.py:run_turn` stays the same shape).
- Moving *tools* that must stay host-side (nft control, staging approval, the
  secrets vault) into the guest — those are the security boundary and stay out.
- Multi-tenant isolation. Single operator, single guest.

## The invariant to preserve

The API key never enters the sandbox, and the guest can reach **only** the model
provider — through a host proxy, never directly. The guest is already egress-
locked (deny-by-default nft); the proxy is the one allowlisted destination.

## Architecture

```
   ┌─ host (thin supervisor) ──────────────────────────────┐
   │  model-proxy  ── injects key, enforces peak gate,      │
   │       ▲          meters tokens into the Budget,        │
   │       │          streams SSE back to the guest         │
   │  tool-broker ── executes host-only tools (nft, staging │
   │       ▲          approve, secrets, git push) on request│
   │       │          from the guest, with the same gates   │
   └───────┼───────────────────────────────────────────────┘
           │ tap (10.66.0.0/24), the ONLY egress the guest has
   ┌───────┼─ guest (disposable) ──────────────────────────┐
   │  run_turn loop + in-guest tools (read/write project    │
   │  files in the pushed workspace, run code locally)      │
   └───────────────────────────────────────────────────────┘
```

### 1. Model proxy (host)
A small host service listening on the tap gateway (`10.66.0.1:<port>`), the
single destination the guest's nft allowlist permits. It:
- receives the guest's `messages` array + params (no key),
- injects `JARVIS_DEEPSEEK_API_KEY`, calls the provider,
- enforces the **peak-pricing gate** and **Budget** metering here (both already
  live in `Model.complete` — they move to the proxy so the guest can't bypass
  them),
- runs the **DSML tool-call recovery** (`parse_dsml_tool_calls`) so that stays
  host-side and consistent,
- streams SSE events back over the tap.

Effectively `Model.complete` splits: the guest keeps the *shape* (build request,
consume SSE), the host keeps everything that touches the key, the wallet, and
the pricing policy.

### 2. Tool broker (host)
Tools divide into two classes:
- **In-guest** (safe to run in the disposable VM): read/write files in the
  pushed workspace, run code, search. These execute in the guest directly — no
  round-trip. The workspace is pushed in (like `vmexec` does now) and pulled back
  as staged changes on turn completion.
- **Host-only** (the security boundary): `stage_write` approval, nft allowlist
  changes, the secrets vault (`secrets.substitute` must never expose real
  secrets to the guest — it already only substitutes host↔VM), git commit/push,
  spawning sub-VMs. These stay host-side; the guest *requests* them over the tap
  and the host runs them behind the same gates that exist today.

The broker is the seam that keeps a compromised guest from touching the boundary
directly — it can only ask, and every ask hits the existing approval/gate logic.

### 3. What moves, what stays
| Component | Today | After |
|---|---|---|
| `run_turn` ReAct loop | host | **guest** |
| `Model.complete` key/peak/budget/DSML | host | **host (proxy)** |
| read/write project files, run code | host tools | **guest tools** |
| nft allowlist, staging approve, secrets, git push | host | **host (broker)** |
| API key | host env | **host only, never in guest** |

## Security analysis
- **Key exfiltration**: the guest never holds the key; it can only send messages
  to the proxy. A compromised guest can burn budget (bounded by the Budget cap +
  peak gate, both host-enforced) but cannot steal the key.
- **Egress**: unchanged — deny-by-default, proxy is the only allowlisted host.
  All existing gate captures (pcap/DNS/audit) still apply; the guest's own model
  traffic to the proxy is expected and filtered from analysis like SSH is today.
- **Injection → escalation**: a prompt injection that reaches code-exec now lands
  in the disposable guest, not the host. Nuke recovers. The broker's gates mean
  it still can't push code or open the firewall without operator approval.
- **New surface**: the proxy itself. It must (a) never echo the key, (b) treat
  the guest's request body as untrusted (it's just a messages array — no eval),
  (c) rate-limit, (d) pin the guest identity to the tap source. Keep it tiny.

## Migration path (incremental, each step shippable)
1. **Proxy first, loop stays host.** Route `Model.complete` through a local proxy
   process (same host). No behavior change; proves the key-injection + SSE relay
   + budget/peak-at-the-proxy design. Fully testable without touching the VM.
2. **Guest can call the proxy.** Allowlist `10.66.0.1:<port>` in the guest nft;
   prove a process in the VM can complete a model call via the proxy with no key
   present in the guest.
3. **Move read-only tools + the loop into the guest**, host-only tools via the
   broker. Run a real chat turn end-to-end in the guest against a test project.
4. **Cut over**; host keeps only supervisor + proxy + broker.

## Open questions
- Per-turn VM lifecycle: fresh guest per turn (max isolation, boot cost) vs a
  persistent guest reset periodically? Lean persistent-with-periodic-nuke, since
  chat turns are frequent and boot is ~seconds.
- Latency: every model call now hops host→guest→host→provider. The proxy is
  loopback-adjacent over the tap, so the added hop is small, but streaming
  responsiveness needs measuring.
- Compaction / context assembly (`memory.py:assemble_system_prompt`) reads host
  files (memory, project.md). Does it run host-side (and get pushed into the
  guest prompt) or in the guest against pushed copies? Lean host-side assembly,
  push the assembled prompt — keeps memory off the guest.

## Verdict
Feasible and a real security win, but a multi-week change touching the model
choke point, the tool layer, and the VM lifecycle. Do it as the four-step
migration above, each step verified on the Pi, only after the rest of the
security backlog is settled. Not to be rushed at the tail of other work.
