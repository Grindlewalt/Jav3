// node frontend/src/boxes/__tests__/logic.test.mjs
import assert from 'node:assert/strict'
import {
  budgetSegments, connCheck, diffLines, filterCatalogue, flattenTree, projectPolicy,
  mergeProcs, navState, nestProcs, parseHosts, persistDaysLeft, profilePayload,
  sortBoxes, treeTotals, uptime, validPackage, validateProfile, variantUsers, blankProfile,
  exposeChoices, exposeDefault, exposePayload, procsRefetch, boxTotals, boxIsOdd, normBoxProcs,
  normRuntimes, assignableProjects, packageReach, buildState, applyBuildEvent, mergeBuildRest,
  buildNeedsReload, verLabel, projectLabel, needsProject,
  networkMode, withNetworkMode, newSitesText, runsIn, withRunsIn, runsInText, deleteBlock,
} from '../logic.js'

let n = 0
const t = (name, fn) => { fn(); n += 1; console.log('ok', name) }

t('nestProcs builds a tree from pid/ppid and keeps orphans as roots', () => {
  const tree = nestProcs([
    { pid: 10, ppid: 1 }, { pid: 11, ppid: 10 }, { pid: 12, ppid: 11 },
    { pid: 20, ppid: 999 },
  ])
  assert.deepEqual(tree.map((x) => x.pid), [10, 20])
  assert.equal(tree[0].children[0].pid, 11)
  assert.equal(tree[0].children[0].children[0].pid, 12)
})

t('nestProcs survives a lying cycle and self-parent', () => {
  const tree = nestProcs([{ pid: 1, ppid: 2 }, { pid: 2, ppid: 1 }, { pid: 3, ppid: 3 }])
  const flat = flattenTree(tree)
  assert.equal(flat.length, 3)
})

t('nestProcs leaves an already-nested tree alone', () => {
  const nested = [{ pid: 1, children: [{ pid: 2, children: [] }] }]
  assert.equal(nestProcs(nested), nested)
})

t('flattenTree indents and honours collapsed keys', () => {
  const tree = [{ pid: 1, children: [{ pid: 2, children: [{ pid: 3 }] }] }, { pid: 4 }]
  let rows = flattenTree(tree)
  assert.deepEqual(rows.map((r) => [r.node.pid, r.depth]), [[1, 0], [2, 1], [3, 2], [4, 0]])
  assert.equal(rows[0].hasChildren, true)
  rows = flattenTree(tree, new Set(['/1/2']))
  assert.deepEqual(rows.map((r) => r.node.pid), [1, 2, 4])
  assert.equal(rows[1].open, false)
})

t('connCheck: verified, unverified, mismatch', () => {
  assert.equal(connCheck({ guest_bytes_out: 100, host_bytes_out: null }), 'unverified')
  assert.equal(connCheck({ guest_bytes_out: 100000, host_bytes_out: 101000,
    guest_bytes_in: 5, host_bytes_in: 5 }), 'verified')
  assert.equal(connCheck({ guest_bytes_out: 100, host_bytes_out: 900000 }), 'mismatch')
  assert.equal(connCheck({ guest_bytes_out: 0, host_bytes_out: 0, verified: false }), 'mismatch')
})

t('treeTotals counts every node and conn', () => {
  const tree = [{
    pid: 1, tag: 'service', rss: 10,
    conns: [{ dir: 'out', guest_bytes_out: 10, host_bytes_out: 999999 }],
    children: [{ pid: 2, tag: 'unexpected', rss: 5, conns: [{ dir: 'in' }] }],
  }]
  const s = treeTotals(tree)
  assert.equal(s.procs, 2); assert.equal(s.service, 1); assert.equal(s.unexpected, 1)
  assert.equal(s.conns, 2); assert.equal(s.in, 1); assert.equal(s.out, 1)
  assert.equal(s.mismatches, 1); assert.equal(s.rss, 15)
})

t('mergeProcs: box_procs replaces by box_id, box_gone drops, the rest is ignored', () => {
  const box = (id, kind, tree = []) => ({ type: 'box_procs', box: { box_id: id, kind, tree } })
  let st = mergeProcs([], box('p-a', 'project'))
  st = mergeProcs(st, box('shared', 'shared'))
  assert.deepEqual(st.map((b) => b.box_id), ['shared', 'p-a'])
  st = mergeProcs(st, box('s-a', 'service', [{ pid: 1, ppid: 0 }]))
  assert.deepEqual(st.map((b) => b.box_id), ['shared', 'p-a', 's-a'])
  assert.equal(st[2].tree[0].pid, 1)
  assert.deepEqual(st[2].orphan_conns, [])
  // replace, not append
  st = mergeProcs(st, box('s-a', 'service', [{ pid: 2, ppid: 0 }, { pid: 3, ppid: 2 }]))
  assert.equal(st.length, 3)
  assert.equal(st[2].tree[0].children[0].pid, 3)
  // box_gone drops; an unknown id is a no-op returning the same array
  st = mergeProcs(st, { type: 'box_gone', box_id: 'p-a' })
  assert.deepEqual(st.map((b) => b.box_id), ['shared', 's-a'])
  assert.equal(mergeProcs(st, { type: 'box_gone', box_id: 'nope' }), st)
  // box_procs_changed / stream_open / noise change nothing here; a bare
  // {box_id} (box_gone's shape) is never mistaken for a row
  assert.equal(mergeProcs(st, { type: 'box_procs_changed', box_id: 's-a' }), st)
  assert.equal(mergeProcs(st, { type: 'stream_open' }), st)
  assert.equal(mergeProcs(st, { type: 'noise', box_id: 'x' }), st)
  assert.equal(mergeProcs(st, { boxes: [] }), st)
})

t('procsRefetch: changed -> that box, stream_open -> everything', () => {
  assert.equal(procsRefetch({ type: 'box_procs_changed', box_id: 's-a' }), 's-a')
  assert.equal(procsRefetch({ type: 'stream_open' }), '*')
  assert.equal(procsRefetch({ type: 'box_procs', box: {} }), null)
  assert.equal(procsRefetch(null), null)
})

t('boxTotals prefers the server totals; boxIsOdd sees orphans and errors', () => {
  const tree = [{ pid: 1, tag: 'service', conns: [{ dir: 'out', guest_bytes_out: 10, host_bytes_out: 10 }] }]
  const b = normBoxProcs({ box_id: 'x', tree, truncated: true,
    totals: { procs: 900, unexpected: 2, conns: 40, guest_bytes_out: 5, guest_bytes_in: 6,
      host_bytes_out: 7, host_bytes_in: 8 } })
  const t2 = boxTotals(b)
  assert.equal(t2.procs, 900); assert.equal(t2.unexpected, 2); assert.equal(t2.conns, 40)
  assert.equal(t2.host_in, 8); assert.equal(t2.service, 1)
  assert.equal(boxTotals(normBoxProcs({ box_id: 'y', tree, totals: null })).procs, 1)
  assert.equal(boxIsOdd(normBoxProcs({ box_id: 'y', tree })), false)
  assert.equal(boxIsOdd(normBoxProcs({ box_id: 'y', tree, orphan_conns: [{ lport: 1 }] })), true)
  assert.equal(boxIsOdd(normBoxProcs({ box_id: 'y', tree, error: 'ps timed out' })), true)
  assert.equal(boxIsOdd(b), true)
})

t('budgetSegments sums reservations and flags over-cap', () => {
  const b = budgetSegments([{ id: 'shared', kind: 'shared', mem_mb: 768, state: 'running' },
    { id: 'p-a', kind: 'project', mem_mb: 1280 }], 2250)
  assert.equal(b.used, 2048); assert.equal(b.free, 202); assert.equal(b.over, false)
  assert.equal(b.segs[0].id, 'shared')
  assert.equal(budgetSegments([{ id: 'x', mem_mb: 3000 }], 2250).over, true)
})

t('navState', () => {
  assert.equal(navState({ boxes: [] }), 'off')
  assert.equal(navState({ boxes: [{ state: 'running', inflight: 0 }] }), 'on')
  assert.equal(navState({ boxes: [{ state: 'running', inflight: 2 }] }), 'busy')
  assert.equal(navState({ boxes: [], budget: { ram_mb_used: 3000, ram_mb_cap: 2250 } }), 'warn')
})

t('sortBoxes: shared, project, service, builder', () => {
  const s = sortBoxes([{ id: 'b', kind: 'builder' }, { id: 's', kind: 'service' },
    { id: 'shared', kind: 'shared' }, { id: 'p', kind: 'project' }])
  assert.deepEqual(s.map((x) => x.kind), ['shared', 'project', 'service', 'builder'])
})

t('filterCatalogue by status, manager, text', () => {
  const rows = [
    { package: 'ripgrep', manager: 'apt', status: 'pending', project_slug: 'alpha' },
    { package: 'requests', manager: 'pip', status: 'built', reason: 'http client' },
  ]
  assert.equal(filterCatalogue(rows, { status: 'pending' }).length, 1)
  assert.equal(filterCatalogue(rows, { manager: 'pip' })[0].package, 'requests')
  assert.equal(filterCatalogue(rows, { q: 'HTTP' })[0].package, 'requests')
  assert.equal(filterCatalogue(rows, { q: 'alpha' })[0].package, 'ripgrep')
})

t('variantUsers', () => {
  const images = { variants: [{ name: 'dev', used_by: ['a', 'b'] }] }
  assert.deepEqual(variantUsers(images, 'dev'), ['a', 'b'])
  assert.deepEqual(variantUsers(images, 'nope', ['x']), ['x'])
})

t('validPackage refuses urls, paths, flags, metacharacters', () => {
  assert.ok(validPackage('apt', 'ripgrep'))
  assert.ok(validPackage('pip', 'requests[socks]'))
  assert.ok(validPackage('npm', '@types/node'))
  for (const bad of ['http://x/y.deb', '../evil', '--index-url=x', 'a;rm -rf /', 'git+https://x', '']) {
    assert.ok(!validPackage('pip', bad), bad)
    assert.ok(!validPackage('apt', bad), bad)
  }
  assert.ok(!validPackage('brew', 'x'))
})

t('parseHosts dedupes and lowercases', () => {
  assert.deepEqual(parseHosts('A.com, b.org\n a.com  c.net'), ['a.com', 'b.org', 'c.net'])
})

t('validateProfile: a new profile is savable with defaults (shared box, per project)', () => {
  const p = { ...blankProfile(), name: 'Mine' }
  assert.equal(p.service_placement, 'per_project'); assert.equal(p.box_runtime, 'kvm')
  assert.equal(p.separate_box, false)
  let e = validateProfile(p)
  assert.deepEqual(e, {})
  e = validateProfile({ ...p, service_placement: '' })
  assert.ok(e.service_placement)
  e = validateProfile({ ...p, name: 'default' }, { names: ['Default'] })
  assert.equal(e.name, 'that name is taken')
  e = validateProfile({ ...p, allow_hosts: ['a.com'], deny_hosts: ['a.com'] })
  assert.match(e.hosts, /both/)
  e = validateProfile({ ...p, separate_box: true, box_mem_mb: 100 })
  assert.ok(e.box_mem_mb)
})

t('profilePayload carries placement and runtime, defaulting when empty', () => {
  const pl = profilePayload({ ...blankProfile(), name: ' x ', service_placement: 'per_service',
    box_runtime: 'docker', box_mem_mb: '512' })
  assert.equal(pl.name, 'x'); assert.equal(pl.service_placement, 'per_service')
  assert.equal(pl.box_runtime, 'docker'); assert.equal(pl.box_mem_mb, 512)
  assert.equal('projects' in pl, false)
  const d = profilePayload({ name: 'y', service_placement: '', box_runtime: '' })
  assert.equal(d.service_placement, 'per_project'); assert.equal(d.box_runtime, 'kvm')
})

t('network mode: one radio over network_off + default_verdict', () => {
  assert.equal(networkMode({ network_off: true, default_verdict: 'allow' }), 'off')
  assert.equal(networkMode({ network_off: false, default_verdict: 'deny' }), 'ask')
  assert.equal(networkMode({ default_verdict: 'allow' }), 'allow')
  assert.deepEqual(withNetworkMode('off'), { network_off: true })
  assert.deepEqual(withNetworkMode('ask'), { network_off: false, default_verdict: 'deny' })
  assert.deepEqual(withNetworkMode('allow'), { network_off: false, default_verdict: 'allow' })
  assert.equal(newSitesText({ default_verdict: 'deny' }), 'ask me')
  assert.equal(newSitesText({ network_off: 1 }), 'network off')
})

t('runs in: one radio over separate_box + box_runtime', () => {
  assert.equal(runsIn({ separate_box: false, box_runtime: 'docker' }), 'shared')
  assert.equal(runsIn({ separate_box: true, box_runtime: 'kvm' }), 'vm')
  assert.equal(runsIn({ separate_box: 1, box_runtime: 'docker' }), 'container')
  assert.deepEqual(withRunsIn('shared'), { separate_box: false })
  assert.deepEqual(withRunsIn('vm'), { separate_box: true, box_runtime: 'kvm' })
  assert.deepEqual(withRunsIn('container'), { separate_box: true, box_runtime: 'docker' })
  assert.equal(runsInText({ separate_box: 0 }), 'shared box')
  assert.equal(runsInText({ separate_box: 1, box_runtime: 'kvm', box_image: 'main', box_mem_mb: 512 }),
    'own VM (main, 512 MB)')
})

t('projectPolicy: one row per project, profile + project lists merged with sources', () => {
  const profiles = [
    { id: 1, name: 'Default', is_default: true, default_verdict: 'deny', network_off: false, projects: ['a', 'c'] },
    { id: 2, name: 'Scoped', is_default: false, default_verdict: 'allow', network_off: true, projects: ['b'] },
  ]
  const groups = [
    { project: 'a', kind: 'project', profile: { id: 1, name: 'Default', default: 'deny' },
      entries: [{ host: 'x.com', source: 'operator' }, { host: 'auto.io', source: 'auto', id: 9 }],
      deny: ['y.com'] },
    { project: 'b', kind: 'project', profile: { id: 2, name: 'Scoped', default: 'allow' },
      entries: [{ host: 'evil.com', source: 'operator' }], deny: ['z.com'] },
    { project: '__image_build__', kind: 'project', profile: { id: 0, name: 'Image build' }, entries: [], deny: [] },
    { project: '__general__', kind: 'profile', profile: { id: 1, name: 'Default', default: 'deny' },
      entries: [{ host: 'pypi.org', source: 'seed' }], deny: [], projects: ['a', 'c'] },
    { project: 'profile:2', kind: 'profile', profile: { id: 2, name: 'Scoped', default: 'allow' },
      entries: [], deny: ['evil.com'], projects: ['b'] },
  ]
  const projects = [{ slug: 'a', name: 'Alpha' }, { slug: 'b', name: 'Beta' }, { slug: 'c', name: 'C' }]
  let r = projectPolicy({ groups, profiles, projects })
  assert.deepEqual(r.map((x) => x.slug), ['a', 'b', 'c'])            // never __image_build__
  const [a, b, c] = r
  assert.equal(a.name, 'Alpha'); assert.equal(a.profile.name, 'Default')
  assert.equal(a.profile.isDefault, true); assert.equal(a.profile.key, '__general__')
  assert.deepEqual(a.allow.map((e) => [e.host, e.from]),
    [['x.com', 'project'], ['auto.io', 'auto'], ['pypi.org', 'general']])
  assert.deepEqual(a.block, [{ host: 'y.com', from: 'project' }])
  assert.equal(b.profile.network_off, true); assert.equal(b.profile.key, 'profile:2')
  assert.deepEqual(b.block.map((e) => [e.host, e.from]), [['z.com', 'project'], ['evil.com', 'profile']])
  assert.equal(b.allow[0].blocked, true)                                // block wins
  // a project with no list of its own still shows its profile's lists
  assert.equal(c.profile.name, 'Default')
  assert.deepEqual(c.allow.map((e) => e.from), ['general'])
  r = projectPolicy({ groups, profiles, projects, filter: 'b' })
  assert.deepEqual(r.map((x) => x.slug), ['b'])
  // the panel knows no project names: the filter alone is enough
  r = projectPolicy({ groups, profiles, projects: [], filter: 'c' })
  assert.equal(r.length, 1); assert.equal(r[0].profile.name, 'Default')
})

t('projectLabel and needsProject', () => {
  assert.equal(projectLabel('__image_build__'), 'image build')
  assert.equal(projectLabel('__general__'), 'Default profile')
  assert.equal(projectLabel('', {}), 'unattributed')
  assert.equal(projectLabel('a', { a: 'Alpha' }), 'Alpha')
  assert.equal(needsProject({ project_slug: null }), true)
  assert.equal(needsProject({ project: '__general__' }), true)
  assert.equal(needsProject({ project_slug: 'a' }), false)
})

t('diffLines classes', () => {
  const d = diffLines('--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n same')
  assert.deepEqual(d.map((x) => x.cls), ['file', 'file', 'hunk', 'del', 'add', 'ctx'])
})

t('persistDaysLeft and uptime', () => {
  const now = Date.parse('2026-09-26T00:00:00Z')
  assert.equal(persistDaysLeft({ delete_after: '2026-10-26 00:00:00' }, now), 30)
  assert.equal(persistDaysLeft({}, now), null)
  assert.equal(uptime(3725), '1h 2m')
  assert.equal(uptime(null), '–')
})

t('exposure: never wider than asked, lan only with a valid services address', () => {
  assert.deepEqual(exposeChoices('none', '10.0.0.9'), ['none'])
  assert.deepEqual(exposeChoices('host', '10.0.0.9'), ['none', 'loopback'])
  assert.deepEqual(exposeChoices('lan', ''), ['none', 'loopback'])
  assert.deepEqual(exposeChoices('lan', '10.0.0.9'), ['none', 'loopback', 'lan'])
  assert.equal(exposeDefault('lan', ''), 'loopback')
  assert.equal(exposeDefault('lan', '10.0.0.9'), 'lan')
  assert.equal(exposeDefault('host', ''), 'loopback')
  assert.equal(exposeDefault('none', 'x'), 'none')
})

t('exposePayload sends canonical binds only, drops closed and impossible ports', () => {
  const ports = [{ port: 8080, expose: 'host' }, { port: 9000, expose: 'lan' }, { port: 22, expose: 'none' }]
  assert.deepEqual(exposePayload({}, ports), [])
  assert.deepEqual(exposePayload({ 9000: 'lan', 8080: 'loopback', 22: 'none' }, ports, '10.0.0.9'),
    [{ port: 8080, bind: 'loopback' }, { port: 9000, bind: 'lan' }])
  // no LAN address any more: a stale lan choice is not sent
  assert.deepEqual(exposePayload({ 9000: 'lan' }, ports, ''), [])
  // legacy values never go out
  assert.deepEqual(exposePayload({ 8080: 'host' }, ports), [])
  assert.deepEqual(exposePayload({ 22: 'loopback' }, ports), [])
})

t('normRuntimes: docker is offered only when the server says available', () => {
  const none = normRuntimes(null)
  assert.equal(none.kvm.available, true)
  assert.equal(none.docker.available, false)
  assert.ok(none.docker.reason)
  const r = normRuntimes({ kvm: { available: true, reason: null },
    docker: { available: false, reason: 'docker not installed', weak: false, warnings: [] } })
  assert.equal(r.docker.available, false); assert.equal(r.docker.reason, 'docker not installed')
  const w = normRuntimes({ kvm: { available: false, reason: 'no /dev/kvm' },
    docker: { available: true, reason: null, rootless: false, userns: false, gvisor: false,
      seccomp: true, weak: true, warnings: ['no userns-remap: root in the container is root on the host'] } })
  assert.equal(w.kvm.available, false); assert.equal(w.kvm.reason, 'no /dev/kvm')
  assert.equal(w.docker.weak, true); assert.equal(w.docker.warnings.length, 1)
  assert.equal(w.docker.seccomp, true)
})

t('assignableProjects drops the reserved slugs', () => {
  assert.deepEqual(assignableProjects([{ slug: 'a' }, { slug: '__image_build__' }, { slug: '__general__' }, null])
    .map((p) => p.slug), ['a'])
})

t('packageReach: the server detail for the row target, used_by for any other variant', () => {
  const p = { target_variant: 'main', variant_used_by: ['a', 'b', 'c'],
    variant_used_by_detail: { all: ['a', 'b', 'c'], direct: ['a'], via: { dev: ['b', 'c'], x: [] } } }
  const r = packageReach(p, 'main', null)
  assert.deepEqual(r.all, ['a', 'b', 'c']); assert.deepEqual(r.direct, ['a'])
  assert.deepEqual(r.via, [['dev', ['b', 'c']]])
  const o = packageReach(p, 'desktop', { variants: [{ name: 'desktop', used_by: ['z'] }] })
  assert.deepEqual(o.all, ['z']); assert.deepEqual(o.via, [])
  assert.deepEqual(packageReach({ target_variant: 'main', variant_used_by: ['q'] }, 'main').all, ['q'])
})

t('build panel: events fold into phase, log and result; REST keeps the finished log', () => {
  let s = buildState({ running: false, log_tail: [] })
  assert.equal(applyBuildEvent(s, { type: 'other' }), s)
  s = applyBuildEvent(s, { type: 'image_build', phase: 'start', variant: 'dev', version: 3 })
  assert.equal(s.running, true); assert.equal(s.variant, 'dev')
  s = applyBuildEvent(s, { type: 'image_build', phase: 'boot', variant: 'dev', box: 'b-dev' })
  assert.equal(s.phase, 'boot'); assert.equal(s.box, 'b-dev')
  s = applyBuildEvent(s, { type: 'image_build', phase: 'log', variant: 'dev', line: 'apt-get install' })
  s = applyBuildEvent(s, { type: 'image_build', phase: 'log', variant: 'dev', line: '<b>x</b>' })
  assert.deepEqual(s.log, ['apt-get install', '<b>x</b>'])
  assert.equal(buildNeedsReload({ type: 'image_build', phase: 'log' }), false)
  assert.equal(buildNeedsReload({ type: 'image_build', phase: 'done' }), true)
  s = applyBuildEvent(s, { type: 'image_build', phase: 'done', variant: 'dev', version: 3, ok: false, error: 'exit 100' })
  assert.equal(s.running, false); assert.equal(s.last.ok, false); assert.equal(s.last.error, 'exit 100')
  // the reload after done: the host has no log_tail any more, this tab keeps its lines
  const m = mergeBuildRest(s, { running: false, variant: null, phase: null, log_tail: [] })
  assert.equal(m.log.length, 2); assert.equal(m.last.error, 'exit 100')
  // a new build seen over REST replaces the old log
  const n2 = mergeBuildRest(m, { running: true, variant: 'main', phase: 'boot', log_tail: ['l1'] })
  assert.deepEqual(n2.log, ['l1']); assert.equal(n2.variant, 'main')
  // the log is capped
  let big = buildState(null)
  for (let i = 0; i < 400; i += 1) big = applyBuildEvent(big, { type: 'image_build', phase: 'log', line: `l${i}` })
  assert.equal(big.log.length, 300); assert.equal(big.log[299], 'l399')
  const r = applyBuildEvent(big, { type: 'image_build', phase: 'resolved', count: 2, error: null })
  assert.deepEqual(r.resolved, { count: 2, error: null })
  assert.equal(verLabel(3), 'v3'); assert.equal(verLabel('v7'), 'v7'); assert.equal(verLabel(null), '?')
})

console.log(`${n} passed`)

t('deleteBlock: the default and in-use profiles cannot go, with a reason', () => {
  assert.match(deleteBlock({ is_default: true, projects: [] }), /make another profile the default/)
  assert.match(deleteBlock({ is_default: false, projects: ['a'] }), /used by 1 project: move it/)
  assert.match(deleteBlock({ projects: ['a', 'b'] }), /used by 2 projects: move them/)
  assert.equal(deleteBlock({ is_default: false, projects: [] }), null)
})
