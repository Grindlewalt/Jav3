import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api.js'
import { setMediaHosts } from './mediaHosts.js'
import Login from './pages/Login.jsx'
import Chat from './pages/Chat.jsx'
import Projects from './pages/Projects.jsx'
import Artifacts from './pages/Artifacts.jsx'
import Workspace from './pages/Workspace.jsx'
import Context from './pages/Context.jsx'
import Skills from './pages/Skills.jsx'
import Tools from './pages/Tools.jsx'
import Agents from './pages/Agents.jsx'
import Schedules from './pages/Schedules.jsx'
import Logs from './pages/Logs.jsx'
import Network from './pages/Network.jsx'
import Review from './pages/Review.jsx'

function NotificationBell() {
  const [data, setData] = useState(null)
  const [secCount, setSecCount] = useState(0)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const load = () => {
      api('/api/notifications').then(setData).catch(() => {})
      // unacknowledged security alerts fold into the same badge
      api('/api/security/events?unacknowledged=true')
        .then((r) => setSecCount((r.events || []).length)).catch(() => {})
    }
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])
  const count = (data?.count || 0) + secCount
  const close = () => setOpen(false)
  return (
    <div className="notif-wrap">
      <button className="notif-bell" onClick={() => setOpen((o) => !o)}
              title="Pending approvals">
        🔔{count > 0 && <span className="notif-badge">{count}</span>}
      </button>
      {open && (
        <div className="notif-drop">
          {count === 0 && <div className="dim small notif-empty">Nothing waiting on you</div>}
          {secCount > 0 && (
            <NavLink to="/review" className="notif-item" onClick={close}>
              <span className="grow ellipsis">⚠ {secCount} security alert{secCount === 1 ? '' : 's'} — review</span>
            </NavLink>
          )}
          {(data?.git || []).map((g) => (
            <NavLink key={`g${g.id}`} to={`/projects/${g.project}`} className="notif-item" onClick={close}>
              <span className="grow ellipsis">git push · {g.project} · {g.message}</span>
            </NavLink>
          ))}
          {(data?.schedules || []).map((s) => (
            <NavLink key={`sc${s.id}`} to="/schedules" className="notif-item" onClick={close}>
              <span className="grow ellipsis">proposed schedule · {s.name} · {s.kind === 'agent' ? s.agent_slug : 'jarvis'}</span>
            </NavLink>
          ))}
        </div>
      )}
    </div>
  )
}

// Guest-VM status (GET /api/vm/status) plus the one operator control: nuke —
// discard the overlay and reboot fresh from the golden image. Nuke is
// double-confirmed and refuses while a turn is in flight; boot/teardown stay
// elsewhere. The status read itself never mutates.
function VmStatus() {
  const [s, setS] = useState(null)
  const [open, setOpen] = useState(false)
  const [nuking, setNuking] = useState(false)
  const [rebuilding, setRebuilding] = useState(false)
  const [toast, setToast] = useState('')
  const load = () => api('/api/vm/status').then(setS).catch(() => setS(null))
  useEffect(() => {
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [])

  async function nuke() {
    if (s?.inflight > 0) {
      window.alert(`${s.inflight} turn(s) in flight — wait for them to finish before nuking.`)
      return
    }
    if (!window.confirm('Nuke the guest VM? Its overlay disk is discarded and it '
      + 'reboots fresh from the golden image. In-flight work is lost.')) return
    setNuking(true)
    try {
      const r = await api('/api/vm/nuke', {
        method: 'POST', body: JSON.stringify({ confirm: true }) })
      setS(r)
    } catch (err) { window.alert(err.detail || String(err)) }
    setNuking(false)
  }

  // Rebuild the golden image from scratch — heavy, so double-confirmed.
  async function rebuild() {
    if (!window.confirm('Rebuild the guest image from scratch? This can take a while.')) return
    if (!window.confirm('Are you sure? The current image is replaced once the build finishes.')) return
    setRebuilding(true)
    setToast('rebuild started…')
    try {
      await api('/api/vm/rebuild', { method: 'POST', body: JSON.stringify({ confirm: true }) })
      setToast('image rebuild kicked off')
      load()
    } catch (err) { setToast(err.detail || String(err)) }
    setRebuilding(false)
    setTimeout(() => setToast(''), 4000)
  }

  if (!s) return null
  const age = s.age_seconds != null
    ? (s.age_seconds < 90 ? `${s.age_seconds}s` : `${Math.round(s.age_seconds / 60)}m`)
    : null
  // newer backends carry image freshness metadata; older ones omit it entirely
  const hasImageMeta = s.image_stale !== undefined || s.image_built_at !== undefined
    || s.image_age_days !== undefined
  const imageAge = s.image_age_days != null
    ? `${s.image_age_days}d old` : (s.image_built_at ? String(s.image_built_at).slice(0, 10) : null)
  return (
    <div className="notif-wrap vm-wrap">
      <button className="notif-bell" onClick={() => setOpen((o) => !o)}
              title="guest VM status">
        <span className={`run-dot ${s.running ? 'running' : ''}`} /> VM
        {s.image_stale && <span className="notif-badge vm-stale-badge" title="image is stale">!</span>}
      </button>
      {open && (
        <div className="notif-drop vm-drop">
          <div className="notif-item"><span className="grow">state</span>
            <span className={s.running ? '' : 'dim'}>
              {s.running ? 'running' : (s.base_built ? 'off' : 'no image')}</span></div>
          {s.running && age && (
            <div className="notif-item"><span className="grow">age</span><span>{age}</span></div>)}
          {s.running && (
            <div className="notif-item"><span className="grow">in-flight turns</span>
              <span>{s.inflight}</span></div>)}
          <div className="notif-item"><span className="grow">gateway</span>
            <span className={s.gateway ? '' : 'dim'}>{s.gateway ? 'on' : 'off'}</span></div>
          <div className="notif-item"><span className="grow">image</span>
            <span className={s.image_stale ? 'warn' : 'dim'}
                  title={s.image_built_at ? `built ${s.image_built_at}` : undefined}>
              {s.image_version}{s.image_stale && imageAge ? ` · ${imageAge}` : ''}</span></div>
          {s.image_stale && (
            <div className="notif-item"><span className="grow warn">stale image</span>
              <span className="warn small">{imageAge || 'rebuild suggested'}</span></div>)}
          {s.idle_scrub_seconds > 0 && (
            <div className="notif-item"><span className="grow">idle scrub</span>
              <span className="dim">{s.idle_scrub_seconds}s</span></div>)}
          {toast && <div className="notif-item"><span className="grow small dim">{toast}</span></div>}
          {hasImageMeta && (
            <div className="vm-nuke-row">
              <button className="ghost" disabled={rebuilding}
                      title="rebuild the golden image from scratch"
                      onClick={rebuild}>{rebuilding ? 'rebuilding…' : '⟳ rebuild image'}</button>
            </div>
          )}
          <div className="vm-nuke-row">
            <button className="ghost danger" disabled={nuking || !s.running}
                    title={s.running ? 'discard the overlay, reboot fresh'
                                     : 'nothing to nuke — guest is off'}
                    onClick={nuke}>{nuking ? 'nuking…' : '☢ nuke guest'}</button>
          </div>
        </div>
      )}
    </div>
  )
}

export default function App() {
  const [user, setUser] = useState(undefined) // undefined = checking
  const [, setCfgReady] = useState(false) // bump once the media allowlist lands
  const [menuOpen, setMenuOpen] = useState(false) // mobile nav drawer
  const location = useLocation()

  // close the mobile menu whenever the route changes
  useEffect(() => { setMenuOpen(false) }, [location.pathname])

  useEffect(() => {
    api('/api/auth/me').then(setUser).catch(() => setUser(null))
    api('/api/config')
      .then((c) => { setMediaHosts(c.media_hosts); setCfgReady(true) })
      .catch(() => {})
  }, [])

  if (user === undefined) return <div className="center">…</div>
  if (user === null && location.pathname !== '/login')
    return <Navigate to="/login" replace />

  return (
    <div className="app">
      {user && (
        <nav className={menuOpen ? 'nav open' : 'nav'}>
          <span className="brand">Jarvis</span>
          <button className="nav-toggle" aria-label="menu"
                  aria-expanded={menuOpen}
                  onClick={() => setMenuOpen((o) => !o)}>
            {menuOpen ? '✕' : '☰'}
          </button>
          <div className="nav-links">
            <NavLink to="/" end>Chat</NavLink>
            <NavLink to="/projects">Projects</NavLink>
            <NavLink to="/artifacts">Artifacts</NavLink>
            <NavLink to="/review">Review</NavLink>
            <NavLink to="/network">Network</NavLink>
            <NavLink to="/context">Context</NavLink>
            <NavLink to="/agents">Agents</NavLink>
            <NavLink to="/logs">Logs</NavLink>
            <NavLink to="/schedules">Schedules</NavLink>
            <NavLink to="/skills">Skills</NavLink>
            <NavLink to="/tools">Tools</NavLink>
            <button
              className="link nav-logout"
              onClick={async () => {
                await api('/api/auth/logout', { method: 'POST' })
                setUser(null)
              }}
            >
              Logout
            </button>
          </div>
          <NotificationBell />
          <VmStatus />
        </nav>
      )}
      <Routes>
        <Route path="/login" element={<Login onLogin={setUser} />} />
        <Route path="/" element={<Chat />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/projects/:slug" element={<Workspace />} />
        <Route path="/artifacts" element={<Artifacts />} />
        <Route path="/review" element={<Review />} />
        <Route path="/network" element={<Network />} />
        <Route path="/context" element={<Context />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/logs" element={<Logs />} />
        <Route path="/schedules" element={<Schedules />} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/tools" element={<Tools />} />
      </Routes>
    </div>
  )
}
