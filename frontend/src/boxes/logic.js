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

// Fold a `procs` stream event into the per-box state. The event is either a
// whole snapshot ({boxes:[...]}) or one box ({box: {...}} / a box row itself).
// Returns a new array, sorted like the boxes page.
export function mergeProcs(prev, ev) {
  if (!ev) return prev
  const incoming = Array.isArray(ev.boxes) ? ev.boxes
    : ev.box && typeof ev.box === 'object' ? [ev.box]
      : ev.box_id ? [ev] : null
  if (!incoming) return prev
  if (Array.isArray(ev.boxes) && ev.full !== false && ev.partial !== true) {
    return sortBoxes(incoming.map(normBoxProcs))
  }
  const m = new Map((prev || []).map((b) => [b.box_id, b]))
  for (const b of incoming) m.set(b.box_id, normBoxProcs(b))
  return sortBoxes([...m.values()])
}

export function normBoxProcs(b) {
  return { ...b, id: b.box_id, tree: nestProcs(b.tree || b.procs || []) }
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
    box_runtime: '', service_placement: '',   // explicit: no default (decision 0.1)
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
    e.service_placement = 'pick a service placement — there is no default'
  }
  if (!['kvm', 'docker'].includes(p.box_runtime)) {
    e.box_runtime = 'pick a box runtime — there is no default'
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
    box_runtime: p.box_runtime,
    service_placement: p.service_placement,
    allow_services: !!p.allow_services,
    allow_package_requests: !!p.allow_package_requests,
  }
}

// ---- network grouping ------------------------------------------------------------

export const GENERAL = '__general__'

// The Network page's allow/deny view: by PROJECT first, each carrying its
// profile's name, then the profile baselines. `groups` is GET
// /api/egress/allowlist's list (project rows; a legacy `__general__` group is
// shown as the Default baseline's entries), `denies` maps slug -> [hosts]
// (from each group's `deny`, when the server sends it), `profiles` is GET
// /api/profiles, `projects` is GET /api/projects.
export function groupPolicy({ groups = [], profiles = [], projects = [], filter = '' }) {
  const profOf = new Map()
  for (const p of profiles) for (const s of p.projects || []) profOf.set(s, p)
  const def = profiles.find((p) => p.builtin && /^default$/i.test(p.name)) || null
  const names = Object.fromEntries(projects.map((p) => [p.slug, p.name]))
  const bySlug = new Map()
  const legacyShared = []
  for (const g of groups) {
    if (g.kind === 'profile') continue
    if (!g.project || g.project === GENERAL) { legacyShared.push(...(g.entries || [])); continue }
    bySlug.set(g.project, g)
  }
  const slugs = new Set([...bySlug.keys(), ...projects.map((p) => p.slug)])
  const projectGroups = [...slugs]
    .filter((s) => !filter || s === filter)
    .map((slug) => {
      const g = bySlug.get(slug) || {}
      const prof = (g.profile && typeof g.profile === 'object' ? g.profile : null)
        || profOf.get(slug) || def
      return {
        slug, name: names[slug] || slug,
        profile: prof ? { id: prof.id, name: prof.name } : null,
        allow: g.entries || g.allow || [],
        deny: (g.deny || g.deny_hosts || []).map((d) => (typeof d === 'string' ? { host: d } : d)),
      }
    })
    .filter((g) => filter || g.allow.length || g.deny.length)
    .sort((a, b) => a.name.localeCompare(b.name))
  const wanted = filter ? new Set(projectGroups.map((g) => g.profile?.id)) : null
  const profileGroups = profiles
    .filter((p) => !wanted || wanted.has(p.id))
    .map((p) => ({
      id: p.id, name: p.name, builtin: !!p.builtin, default_verdict: p.default_verdict,
      network_off: !!p.network_off, projects: p.projects || [],
      allow: [...(p.allow_hosts || []),
        ...(p === def ? legacyShared.map((e) => e.host) : [])]
        .filter((h, i, a) => a.indexOf(h) === i),
      deny: p.deny_hosts || [],
    }))
  // no profiles yet (a server before WP2): the old shared list still shows
  if (!def && legacyShared.length && !filter) {
    profileGroups.push({
      id: GENERAL, name: 'Shared (every project without its own list)', builtin: true,
      legacy: true, default_verdict: 'deny', network_off: false, projects: [],
      allow: [...new Set(legacyShared.map((e) => e.host))], deny: [], entries: legacyShared,
    })
  }
  return { projectGroups, profileGroups }
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
  const after = view?.delete_after || view?.persist_delete_after
  if (!after) return null
  const t = Date.parse(String(after).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(after) ? '' : 'Z'))
  if (Number.isNaN(t)) return null
  return Math.max(0, Math.ceil((t - now) / 86400000))
}
