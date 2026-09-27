// Pure logic for the boxes pages (VMs, Persistent, Catalogue, Profiles,
// Network grouping). No React, no fetch: everything here is tested under plain
// node (src/boxes/__tests__/logic.test.mjs).

// ---- sizes and times ---------------------------------------------------------

export function bytes(n) {
  if (n == null || Number.isNaN(Number(n))) return '–'
  const v = Number(n)
  if (v < 1024) return `${v} B`
  if (v < 1024 ** 2) return `${(v / 1024).toFixed(1)} KB`
  if (v < 1024 ** 3) return `${(v / 1024 ** 2).toFixed(1)} MB`
  return `${(v / 1024 ** 3).toFixed(2)} GB`
}

export function mb(n) {
  if (n == null) return '–'
  return n >= 1024 ? `${(n / 1024).toFixed(n % 1024 ? 1 : 0)} GB` : `${n} MB`
}

export function uptime(s) {
  if (s == null) return '–'
  const v = Math.max(0, Math.floor(s))
  if (v < 60) return `${v}s`
  if (v < 3600) return `${Math.floor(v / 60)}m`
  if (v < 86400) return `${Math.floor(v / 3600)}h ${Math.floor((v % 3600) / 60)}m`
  return `${Math.floor(v / 86400)}d ${Math.floor((v % 86400) / 3600)}h`
}

// ---- boxes -------------------------------------------------------------------

export const KIND_ORDER = { shared: 0, project: 1, service: 2, builder: 3 }

export function sortBoxes(boxes) {
  return [...(boxes || [])].sort((a, b) =>
    (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9)
    || String(a.project || '').localeCompare(String(b.project || ''))
    || String(a.id).localeCompare(String(b.id)))
}

// The RAM budget bar: one segment per box that holds a reservation (every
// box counts, running or not — contract section A), in the budget's order.
// `pct` is of the cap; `over` when the reservations exceed it.
export function budgetSegments(boxes, cap) {
  const c = Number(cap) || 0
  const segs = sortBoxes(boxes).map((b) => ({
    id: b.id, kind: b.kind, mb: Number(b.mem_mb) || 0, running: b.state === 'running',
    pct: c ? ((Number(b.mem_mb) || 0) / c) * 100 : 0,
  }))
  const used = segs.reduce((n, s) => n + s.mb, 0)
  return { segs, used, cap: c, free: Math.max(0, c - used), over: c > 0 && used > c }
}

// The nav dot: one word for all the boxes together.
//   'busy'  a box is mid-turn, or an image build runs
//   'on'    at least one box running
//   'off'   none running
//   'warn'  the budget is over its cap, or a box failed
export function navState(boxesResp) {
  const boxes = boxesResp?.boxes || []
  const b = boxesResp?.budget
  if (b && b.ram_mb_cap && b.ram_mb_used > b.ram_mb_cap) return 'warn'
  if (boxes.some((x) => x.state === 'failed')) return 'warn'
  if (boxes.some((x) => x.state === 'running' && x.inflight > 0)) return 'busy'
  if (boxes.some((x) => x.state === 'running')) return 'on'
  return 'off'
}

// The runtimes GET /api/vm/boxes reports (WP8). // Unknown = unavailable: a runtime the server did not vouch for is not offered.
export function normRuntimes(rt) {
  const kvm = rt?.kvm || {}
  const d = rt?.docker || {}
  return {
    kvm: { available: kvm.available !== false, reason: kvm.reason || null },
    docker: {
      available: d.available === true,
      reason: d.reason || (rt ? null : 'this server does not report its runtimes'),
      rootless: !!d.rootless, userns: !!d.userns, gvisor: !!d.gvisor, seccomp: !!d.seccomp,
      weak: !!d.weak, warnings: Array.isArray(d.warnings) ? d.warnings : [],
    },
  }
}

// ---- process trees -------------------------------------------------------------

// A box may report a nested tree (children[]) or a flat list (pid/ppid): nest
// the flat one. A node whose parent is not in the list is a root. Cycles
// (a lying guest) are broken: a node is placed once.
export function nestProcs(list) {
  const items = list || []
  if (items.some((p) => Array.isArray(p.children) && p.children.length)) return items
  const byPid = new Map()
  for (const p of items) byPid.set(p.pid, { ...p, children: [] })
  const roots = []
  const placed = new Set()
  for (const p of items) {
    const node = byPid.get(p.pid)
    if (placed.has(p.pid)) continue
    placed.add(p.pid)
    const parent = p.ppid !== p.pid ? byPid.get(p.ppid) : null
    if (parent && !isAncestor(node, parent)) parent.children.push(node)
    else roots.push(node)
  }
  return roots
}

function isAncestor(node, maybeChild) {
  const stack = [...(node.children || [])]
  while (stack.length) {
    const n = stack.pop()
    if (n === maybeChild) return true
    stack.push(...(n.children || []))
  }
  return false
}

// Depth-first rows for rendering: [{node, depth, key, hasChildren, open}].
// `collapsed` is a Set of keys the operator folded; a folded node's
// descendants are not emitted.
export function flattenTree(tree, collapsed = new Set(), prefix = '') {
  const out = []
  const walk = (nodes, depth, path) => {
    for (const n of nodes || []) {
      const key = `${path}/${n.pid}`
      const kids = n.children || []
      const open = !collapsed.has(key)
      out.push({ node: n, depth, key, hasChildren: kids.length > 0, open })
      if (open) walk(kids, depth + 1, key)
    }
  }
  walk(tree, 0, prefix)
  return out
}

// Guest-reported vs host-verified bytes on one connection.
//   'verified'   the host saw it and the numbers agree
//   'unverified' the host has no figure for it (not through the proxy/relay)
//   'mismatch'   both present and they disagree by more than the slack
// Slack: 4 KiB or 5 %, whichever is larger — TCP overhead and timing between
// the guest's snapshot and the proxy's meter are never exactly equal.
export function connCheck(c) {
  const pairs = [[c.guest_bytes_out, c.host_bytes_out], [c.guest_bytes_in, c.host_bytes_in]]
  const known = pairs.filter(([, h]) => h != null)
  if (!known.length) return 'unverified'
  for (const [g, h] of known) {
    const gv = Number(g) || 0
    const hv = Number(h) || 0
    const slack = Math.max(4096, 0.05 * Math.max(gv, hv))
    if (Math.abs(gv - hv) > slack) return 'mismatch'
  }
  if (c.verified === false) return 'mismatch'
  return 'verified'
}

// Totals across one box's tree (every node, folded or not).
export function treeTotals(tree) {
  const t = {
    procs: 0, unexpected: 0, service: 0, run_code: 0, conns: 0, in: 0, out: 0,
    guest_out: 0, guest_in: 0, host_out: 0, host_in: 0, mismatches: 0, rss: 0,
  }
  const walk = (nodes) => {
    for (const n of nodes || []) {
      t.procs += 1
      if (n.tag && t[n.tag] !== undefined) t[n.tag] += 1
      t.rss += Number(n.rss) || 0
      for (const c of n.conns || []) {
        t.conns += 1
        if (c.dir === 'in') t.in += 1; else t.out += 1
        t.guest_out += Number(c.guest_bytes_out) || 0
        t.guest_in += Number(c.guest_bytes_in) || 0
        t.host_out += Number(c.host_bytes_out) || 0
        t.host_in += Number(c.host_bytes_in) || 0
        if (connCheck(c) === 'mismatch') t.mismatches += 1
      }
      walk(n.children)
    }
  }
  walk(tree)
  return t
}

// Server totals win where the box sent them (they count every process, even
// when the tree was truncated); the tree fills in what they do not carry
// (service/run_code counts, byte mismatches, in/out split).
export function boxTotals(b) {
  const t = treeTotals(b?.tree)
  const s = b?.totals
  if (!s) return t
  const num = (v, d) => (v == null ? d : Number(v) || 0)
  return {
    ...t,
    procs: num(s.procs, t.procs),
    unexpected: num(s.unexpected, t.unexpected),
    conns: num(s.conns, t.conns),
    guest_out: num(s.guest_bytes_out, t.guest_out),
    guest_in: num(s.guest_bytes_in, t.guest_in),
    host_out: num(s.host_bytes_out, t.host_out),
    host_in: num(s.host_bytes_in, t.host_in),
  }
}

// Something on this box wants the operator's eye: an unexpected process, a
// guest/host byte disagreement, host-seen traffic no process owns, or an error.
export function boxIsOdd(b) {
  const t = boxTotals(b)
  return !!(t.unexpected || t.mismatches || (b?.orphan_conns || []).length || b?.error)
}

// Fold one `procs` stream event into the per-box rows (docs/boxes-api-final.md
// section 6). The topic carries ONE box at a time:
//   {type:"box_procs", box:<row>}     replace that row by box_id
//   {type:"box_gone", box_id}         drop it
//   {type:"box_procs_changed", box_id} the row was too big to send: the caller
//                                     refetches ?box= (procsRefetch) — no change here
//   {type:"stream_open"}               no change here (the caller reloads)
// Anything else is ignored. Returns `prev` itself when nothing changed.
export function mergeProcs(prev, ev) {
  if (!ev || typeof ev !== 'object') return prev
  if (ev.type === 'box_procs' && ev.box && typeof ev.box === 'object' && ev.box.box_id) {
    const m = new Map((prev || []).map((b) => [b.box_id, b]))
    m.set(ev.box.box_id, normBoxProcs(ev.box))
    return sortBoxes([...m.values()])
  }
  if (ev.type === 'box_gone' && ev.box_id) {
    if (!(prev || []).some((b) => b.box_id === ev.box_id)) return prev
    return (prev || []).filter((b) => b.box_id !== ev.box_id)
  }
  return prev
}

// What an event asks the caller to fetch: a box id (GET ?box=<id>), '*' for a
// full reload (the stream (re)opened, so events may have been missed), or null.
export function procsRefetch(ev) {
  if (!ev || typeof ev !== 'object') return null
  if (ev.type === 'box_procs_changed' && ev.box_id) return ev.box_id
  if (ev.type === 'stream_open') return '*'
  return null
}

export function normBoxProcs(b) {
  return {
    ...b, id: b.box_id, tree: nestProcs(b.tree || []),
    orphan_conns: Array.isArray(b.orphan_conns) ? b.orphan_conns : [],
  }
}

// ---- catalogue -----------------------------------------------------------------

export const PKG_STATUSES = ['pending', 'approved', 'building', 'built', 'failed',
  'rejected', 'removed']

export function filterCatalogue(rows, { status = '', manager = '', q = '' } = {}) {
  const needle = q.trim().toLowerCase()
  return (rows || []).filter((r) => {
    if (status && r.status !== status) return false
    if (manager && r.manager !== manager) return false
    if (!needle) return true
    return [r.package, r.project_slug, r.reason, r.target_variant, r.canonical_command]
      .some((s) => String(s || '').toLowerCase().includes(needle))
  })
}

// Which projects an approval reaches: everyone on the target variant.
export function variantUsers(images, variant, fallback = []) {
  const v = (images?.variants || []).find((x) => x.name === variant)
  return v ? (v.used_by || []) : fallback
}

// A package row's reach, from the server's own figures when the variant is
// the row's target: {all, direct, via:[[variant, slugs]]}. For another variant
// the operator picked, the images list's used_by is all there is.
export function packageReach(p, variant, images) {
  const target = variant || p?.target_variant
  if (p && target === p.target_variant) {
    const d = p.variant_used_by_detail
    if (d && typeof d === 'object') {
      return {
        all: d.all || p.variant_used_by || [], direct: d.direct || [],
        via: Object.entries(d.via || {}).filter(([, s]) => (s || []).length),
      }
    }
    if (p.variant_used_by) return { all: p.variant_used_by, direct: p.variant_used_by, via: [] }
  }
  const all = variantUsers(images, target, [])
  return { all, direct: all, via: [] }
}

// Rows the operator can take out of an image (the next build drops them).
export const PKG_REMOVABLE = new Set(['approved', 'built', 'failed'])

// ---- image builds ------------------------------------------------------------

export const BUILD_LOG_MAX = 300

// The build panel's state, seeded from GET /api/vm/images `build` and folded
// forward by `vm-images` events {type:"image_build", phase, variant, ...}.
// The host's phases: start, boot, log (with `line`), done ({ok, error,
// version}), resolved ({count, error}: a package dry-run, not a build).
export function buildState(build) {
  const b = build || {}
  return {
    running: !!b.running, variant: b.variant || null, mode: b.mode || null,
    phase: b.phase || null, box: null, log: [...(b.log_tail || [])].slice(-BUILD_LOG_MAX),
    last: null, resolved: null,
  }
}
export function applyBuildEvent(st, ev) {
  if (!ev || ev.type !== 'image_build') return st
  const s = st || buildState(null)
  switch (ev.phase) {
    case 'start':
      return { ...s, running: true, variant: ev.variant || s.variant, phase: 'start', box: null,
        log: [], last: null }
    case 'log': {
      if (ev.line == null) return s
      const log = [...s.log, String(ev.line)]
      return { ...s, running: true, variant: ev.variant || s.variant,
        log: log.length > BUILD_LOG_MAX ? log.slice(-BUILD_LOG_MAX) : log }
    }
    case 'done':
      return { ...s, running: false, phase: 'done',
        last: { variant: ev.variant || s.variant, version: ev.version ?? null,
          ok: ev.ok !== false && !ev.error, error: ev.error || null } }
    case 'resolved':
      return { ...s, resolved: { count: Number(ev.count) || 0, error: ev.error || null } }
    default:
      // boot, and any phase the host adds later: show it, keep the log
      return { ...s, running: true, variant: ev.variant || s.variant, phase: ev.phase || s.phase,
        box: ev.box || s.box }
  }
}
// A REST read of `build` folded into the panel. The host keeps log_tail only
// while a build runs (the last 20 lines), so once it is over the lines and the
// result this tab collected from the stream are kept.
export function mergeBuildRest(cur, build) {
  const fresh = buildState(build)
  if (!cur) return fresh
  if (fresh.running) {
    const same = cur.running && cur.variant === fresh.variant
    return { ...fresh, box: same ? cur.box : null,
      log: same && cur.log.length > fresh.log.length ? cur.log : fresh.log,
      resolved: cur.resolved }
  }
  return { ...cur, running: false, phase: cur.running ? null : cur.phase }
}

export const verLabel = (v) => (v == null ? '?' : typeof v === 'number' || /^\d+$/.test(String(v)) ? `v${v}` : String(v))

// Events after which the variants list itself changed (a version appeared,
// a package row moved): refetch it.
export const buildNeedsReload = (ev) =>
  !!ev && ev.type === 'image_build' && ['start', 'done', 'resolved'].includes(ev.phase)

// The per-manager package name check the host also applies (contract (f)):
// no URLs, paths, git+, flags or shell metacharacters. The host is the
// authority; this only stops an obvious typo before a round trip.
const PKG_RE = {
  apt: /^[a-z0-9][a-z0-9+.-]{0,127}$/,
  pip: /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(\[[A-Za-z0-9,._-]+\])?$/,
  npm: /^(@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]{0,213}$/,
}
export function validPackage(manager, name) {
  const re = PKG_RE[manager]
  return !!re && re.test(String(name || '').trim())
}
export const validVersion = (v) => !v || /^[A-Za-z0-9._+~:-]{1,64}$/.test(v)

// ---- profiles ------------------------------------------------------------------

export const PLACEMENTS = [
  { value: 'per_service', label: 'Per service', hint: 'one box per service — strongest, most RAM (384 MB each)' },
  { value: 'per_project', label: 'Per project', hint: "one box for this project's services" },
  { value: 'shared', label: 'Shared', hint: "one box for every shared-placement project's services — cheapest, weakest" },
]
export const RUNTIMES = [
  { value: 'kvm', label: 'KVM', hint: 'a virtual machine — its own kernel' },
  { value: 'docker', label: 'Docker', hint: 'a container — shares the host kernel (less isolated)' },
]

// The profile form's one "Network" choice, over two stored fields
// (network_off, default_verdict). "ask" is default_verdict=deny: a site on
// neither list is refused and queued for you to approve.
export const NETWORK_MODES = [
  { value: 'off', label: 'Off', hint: 'no network at all, whatever the lists say' },
  { value: 'ask', label: 'Ask me about new sites', hint: 'a site on neither list waits for you' },
  { value: 'allow', label: 'Allow new sites', hint: 'a site on neither list is let through' },
]
export const networkMode = (p) =>
  (p?.network_off ? 'off' : p?.default_verdict === 'allow' ? 'allow' : 'ask')
export function withNetworkMode(mode) {
  if (mode === 'off') return { network_off: true }
  return { network_off: false, default_verdict: mode === 'allow' ? 'allow' : 'deny' }
}
// The rule for a site on neither list, in the words the pages use.
export const newSitesText = (p) =>
  ({ off: 'network off', ask: 'ask me', allow: 'allowed' })[networkMode(p)]

// The profile form's one "Runs in" choice, over separate_box + box_runtime.
// The shared box leaves box_runtime as it was (it only matters for an own box).
export const RUNS_IN = [
  { value: 'shared', label: 'Shared box', hint: 'the one box every shared project uses' },
  { value: 'vm', label: 'Own VM', hint: 'a virtual machine per project — its own kernel' },
  { value: 'container', label: 'Own container', hint: 'a container per project — shares the host kernel (less isolated)' },
]
export const runsIn = (p) =>
  (!p?.separate_box ? 'shared' : p.box_runtime === 'docker' ? 'container' : 'vm')
export function withRunsIn(value) {
  if (value === 'vm') return { separate_box: true, box_runtime: 'kvm' }
  if (value === 'container') return { separate_box: true, box_runtime: 'docker' }
  return { separate_box: false }
}
export function runsInText(p) {
  const r = runsIn(p)
  if (r === 'shared') return 'shared box'
  const what = `${p.box_image || 'main'}${p.box_mem_mb ? `, ${p.box_mem_mb} MB` : ''}`
  return `${r === 'vm' ? 'own VM' : 'own container'} (${what})`
}

export function parseHosts(text) {
  const seen = new Set()
  const out = []
  for (const raw of String(text || '').split(/[\s,]+/)) {
    const h = raw.trim().toLowerCase()
    if (h && !seen.has(h)) { seen.add(h); out.push(h) }
  }
  return out
}
const HOST_RE = /^(\*\.)?[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*(:\d{1,5})?$/
export const validHost = (h) => HOST_RE.test(h)

export function blankProfile() {
  return {
    name: '', builtin: false, default_verdict: 'deny', network_off: false,
    allow_hosts: [], deny_hosts: [], secrets: [], auto_handle: false,
    separate_box: false, box_image: 'main', box_mem_mb: 768,
    box_runtime: 'kvm', service_placement: 'per_project',   // shared box; per-project services
    allow_services: false, allow_package_requests: false, projects: [],
  }
}

// Field errors for the profile form. Empty object = savable.
export function validateProfile(p, { names = [], minMem = 256 } = {}) {
  const e = {}
  const name = String(p.name || '').trim()
  if (!name) e.name = 'a name is required'
  else if (names.some((n) => n.toLowerCase() === name.toLowerCase())) e.name = 'that name is taken'
  if (!['per_service', 'per_project', 'shared'].includes(p.service_placement)) {
    e.service_placement = 'pick where its services run'
  }
  if (!['kvm', 'docker'].includes(p.box_runtime)) {
    e.box_runtime = 'pick where it runs'
  }
  if (!['allow', 'deny'].includes(p.default_verdict)) e.default_verdict = 'allow or deny'
  const bad = [...(p.allow_hosts || []), ...(p.deny_hosts || [])].filter((h) => !validHost(h))
  if (bad.length) e.hosts = `not a host: ${bad.slice(0, 3).join(', ')}`
  const both = (p.allow_hosts || []).filter((h) => (p.deny_hosts || []).includes(h))
  if (both.length) e.hosts = `on both lists: ${both.slice(0, 3).join(', ')}`
  if (p.separate_box) {
    const m = Number(p.box_mem_mb)
    if (!Number.isFinite(m) || m < minMem) e.box_mem_mb = `at least ${minMem} MB`
    if (!p.box_image) e.box_image = 'pick an image variant'
  }
  return e
}

// Projects a profile can be assigned to: never the reserved keys
// (`__image_build__` is the builders' fixed policy — the server answers 409 —
// and `__general__` is the Default profile's list, not a project).
export const assignableProjects = (projects) =>
  (projects || []).filter((p) => p && p.slug && !String(p.slug).startsWith('__'))

// What a profile save sends (every field; the server wants them all).
export function profilePayload(p) {
  return {
    name: String(p.name || '').trim(),
    default_verdict: p.default_verdict,
    network_off: !!p.network_off,
    allow_hosts: p.allow_hosts || [],
    deny_hosts: p.deny_hosts || [],
    secrets: p.secrets || [],
    auto_handle: !!p.auto_handle,
    separate_box: !!p.separate_box,
    box_image: p.box_image || 'main',
    box_mem_mb: p.box_mem_mb == null || p.box_mem_mb === '' ? null : Number(p.box_mem_mb),
    box_runtime: p.box_runtime || 'kvm',
    service_placement: p.service_placement || 'per_project',
    allow_services: !!p.allow_services,
    allow_package_requests: !!p.allow_package_requests,
  }
}

// ---- network grouping ------------------------------------------------------------

export const GENERAL = '__general__'
export const IMAGE_BUILD = '__image_build__'

// The Network page's per-project view, from GET /api/egress/allowlist
// (`groups`) and GET /api/profiles (`profiles`): one row per project with its
// profile, the profile's new-sites rule, and the EFFECTIVE always-allow and
// always-block lists — the profile's and the project's merged, each entry
// marked with where it lives:
//   project  the project's own list (edit it in the project's editor)
//   auto     a live auto-mode allow on the project (Keep / Revoke)
//   profile  the profile's list (edit the profile)
//   general  the Default profile's list (the old shared list, key __general__)
// An allow entry that is also on a block list carries `blocked: true` (block
// wins). Every project has a profile: one with none recorded is on Default.
export function projectPolicy({ groups = [], profiles = [], projects = [], filter = '' }) {
  const names = Object.fromEntries(projects.map((p) => [p.slug, p.name]))
  const isDefaultProf = (p) => !!p && p.builtin && /^default$/i.test(p.name || '')
  const def = profiles.find(isDefaultProf)
  const hostOf = (x) => (typeof x === 'string' ? x : x?.host)
  const byProject = new Map()
  const byProfile = new Map()          // profile id -> its group
  for (const g of groups) {
    if (!g) continue
    if (g.kind === 'project' && g.project && !String(g.project).startsWith('__')) byProject.set(g.project, g)
    if (g.kind === 'profile' && g.profile) byProfile.set(g.profile.id, g)
  }
  const slugs = new Set(projects.map((p) => p.slug).filter((x) => x && !String(x).startsWith('__')))
  for (const k of byProject.keys()) slugs.add(k)
  if (filter) { slugs.clear(); if (!String(filter).startsWith('__')) slugs.add(filter) }
  const rows = [...slugs].map((slug) => {
    const own = byProject.get(slug)
    const listed = profiles.find((p) => (p.projects || []).includes(slug))
    const pid = own?.profile?.id ?? listed?.id ?? def?.id
    const full = profiles.find((p) => p.id === pid) || listed || def || {}
    const pg = byProfile.get(pid)
    const general = pg?.project === GENERAL || isDefaultProf(full)
    const profSrc = general ? 'general' : 'profile'
    const profile = {
      id: pid ?? null,
      name: full.name || own?.profile?.name || pg?.profile?.name || 'Default',
      isDefault: general,
      network_off: !!full.network_off,
      default_verdict: full.default_verdict || own?.profile?.default || pg?.profile?.default || 'deny',
      key: pg?.project ?? (general ? GENERAL : pid != null ? `profile:${pid}` : GENERAL),
    }
    const block = []
    const seenB = new Set()
    for (const h of (own?.deny || []).map(hostOf).filter(Boolean)) {
      if (!seenB.has(h)) { seenB.add(h); block.push({ host: h, from: 'project' }) }
    }
    for (const h of (pg?.deny || full.deny_hosts || []).map(hostOf).filter(Boolean)) {
      if (!seenB.has(h)) { seenB.add(h); block.push({ host: h, from: profSrc }) }
    }
    const allow = []
    const seenA = new Set()
    const add = (e, from) => {
      const k = `${from}|${e.host}`
      if (!e.host || seenA.has(k)) return
      seenA.add(k)
      allow.push({ ...e, from, blocked: seenB.has(e.host) })
    }
    for (const e of own?.entries || []) add(e, e.source === 'auto' ? 'auto' : 'project')
    for (const e of pg?.entries || (full.allow_hosts || []).map((h) => ({ host: h, source: 'operator' }))) {
      add(e, profSrc)
    }
    return { slug, name: names[slug] || slug, profile, allow, block }
  })
  rows.sort((x, y) => x.name.localeCompare(y.name))
  return rows
}

// Plain words for where an effective entry lives.
export const POLICY_FROM = {
  project: { text: 'project', title: "this project's own list" },
  auto: { text: 'auto', title: 'a live auto-mode allow: it expires unless you keep it' },
  profile: { text: 'profile', title: "the profile's list: every project on the profile shares it" },
  general: { text: 'general', title: "the Default profile's list: every project on Default shares it" },
}

// The label for an egress row's project: the builders' traffic and the
// Default profile's list have names of their own.
export function projectLabel(slug, names = {}) {
  if (!slug) return 'unattributed'
  if (slug === GENERAL) return 'Default profile'
  if (slug === IMAGE_BUILD) return 'image build'
  return names[slug] || slug
}
// A waiting row / decision with no project must be given one before it can
// be approved or allowed (the host answers 409 / needs_project otherwise).
export const needsProject = (row) => !(row?.project_slug || row?.project)
  || (row.project_slug || row.project) === GENERAL

// ---- services ------------------------------------------------------------------

// Where the operator may expose one requested port. The host binds no wider
// than the agent asked (services.check_exposure): a port asked "none" cannot
// be exposed at all, "host" only on loopback, "lan" on loopback or — only
// while services_lan_ip is valid — the LAN. The values are the canonical
// binds the approve route takes; "none" is the UI's "leave it closed".
export const EXPOSE_LABEL = {
  none: 'not exposed',
  loopback: 'this host only (loopback)',
  lan: 'LAN (services address)',
}
export function exposeChoices(asked, lanIp) {
  if (asked === 'lan') return lanIp ? ['none', 'loopback', 'lan'] : ['none', 'loopback']
  if (asked === 'host') return ['none', 'loopback']
  return ['none']
}
// The starting choice: what the agent asked for, narrowed to what is possible.
export function exposeDefault(asked, lanIp) {
  const c = exposeChoices(asked, lanIp)
  if (asked === 'lan' && c.includes('lan')) return 'lan'
  return c.includes('loopback') ? 'loopback' : 'none'
}
// {port: choice} -> the approve body's expose_ports (REQUIRED, may be []).
// Closed ports are left out; any stale choice outside the allowed set is too.
export function exposePayload(choices, ports = null, lanIp = '') {
  const asked = ports ? Object.fromEntries(ports.map((p) => [String(p.port), p.expose])) : null
  return Object.entries(choices || {})
    .filter(([port, bind]) => (bind === 'loopback' || bind === 'lan')
      && (!asked || exposeChoices(asked[port], lanIp).includes(bind)))
    .map(([port, bind]) => ({ port: Number(port), bind }))
    .sort((a, b) => a.port - b.port)
}

export const SERVICE_STATE_TONE = {
  running: 'done', stopped: undefined, failed: 'error', unreported: 'pending',
}

// ---- diffs -------------------------------------------------------------------

// A unified diff, line by line, classed for colour. Rendered as text nodes.
export function diffLines(diff) {
  if (!diff) return []
  return String(diff).split('\n').map((text) => ({
    text,
    cls: text.startsWith('+++') || text.startsWith('---') ? 'file'
      : text.startsWith('@@') ? 'hunk'
        : text.startsWith('+') ? 'add'
          : text.startsWith('-') ? 'del' : 'ctx',
  }))
}

// ---- persist retirement ------------------------------------------------------

// Days left before an imported /persist disk is deleted (null = not imported).
export function persistDaysLeft(view, now = Date.now()) {
  const after = view?.delete_after
  if (!after) return null
  const t = Date.parse(String(after).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(after) ? '' : 'Z'))
  if (Number.isNaN(t)) return null
  return Math.max(0, Math.ceil((t - now) / 86400000))
}
