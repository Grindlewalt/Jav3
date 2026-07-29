import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api.js'
import { useDismiss } from './useDismiss.js'
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
import TriagePanel from './TriagePanel.jsx'

// Primary destinations stay on the bar; everything else lives behind "More".
// Eleven top-level links used to wrap the bar into two or three ragged rows
// between 769px and ~1250px, stranding Logout in the middle of the header.
const PRIMARY_LINKS = [
  { to: '/', label: 'Chat', end: true },
  { to: '/projects', label: 'Projects' },
  { to: '/agents', label: 'Agents' },
  { to: '/review', label: 'Review' },
]
const MORE_LINKS = [
  { to: '/artifacts', label: 'Artifacts' },
  { to: '/network', label: 'Network' },
  { to: '/context', label: 'Context' },
  { to: '/logs', label: 'Logs' },
  { to: '/schedules', label: 'Schedules' },
  { to: '/skills', label: 'Skills' },
  { to: '/tools', label: 'Tools' },
]

// Counts come from live queues and reached 294 in practice, which overflowed
// the badge and smeared across the icon.
const badge = (n) => (n > 99 ? '99+' : String(n))

function NotificationBell() {
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const load = () => api('/api/notifications').then(setData).catch(() => {})
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])
  // the backend count already folds in security alerts + egress approvals
  const count = data?.count || 0
  const secCount = data?.alerts || 0
  const egressCount = data?.egress_pending || 0
  const close = useCallback(() => setOpen(false), [])
  const ref = useDismiss(open, close)
  return (
    <div className="notif-wrap" ref={ref}>
      <button className="notif-bell" onClick={() => setOpen((o) => !o)}
              aria-label={`Pending approvals${count ? ` (${count})` : ''}`}
              aria-expanded={open} title="Pending approvals">
        <span aria-hidden="true">🔔</span>
        {count > 0 && <span className="notif-badge">{badge(count)}</span>}
      </button>
      {open && (
        <div className="notif-drop">
          {count === 0 && <div className="dim small notif-empty">Nothing waiting on you</div>}
          {secCount > 0 && (
            <NavLink to="/review" className="notif-item" onClick={close}>
              <span className="grow ellipsis">⚠ {secCount} security alert{secCount === 1 ? '' : 's'} — review</span>
            </NavLink>
          )}
          {egressCount > 0 && (
            <NavLink to="/network" className="notif-item" onClick={close}>
              <span className="grow ellipsis">🌐 {egressCount} host approval{egressCount === 1 ? '' : 's'} waiting — network</span>
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
// Runtime model switch: flash for cheap, pro for smart. Applies to the next
// model call everywhere (chat/agents/guest); agents with an explicit model
// pin keep it. Server-side allowlist; persisted across restarts.
// State lives in App so the bar copy and the mobile-drawer copy stay in sync.
function useModel() {
  const [m, setM] = useState(null)
  useEffect(() => { api('/api/model').then(setM).catch(() => {}) }, [])
  const change = useCallback(async (model) => {
    try {
      setM(await api('/api/model', {
        method: 'PUT', body: JSON.stringify({ model }) }))
    } catch (err) { window.alert(err.detail || String(err)) }
  }, [])
  return [m, change]
}

function ModelSwitch({ m, onChange, className = '' }) {
  if (!m) return null
  const short = (id) => id.replace(/^deepseek-v4-/, '')
  return (
    <select className={`model-switch ${className}`} value={m.active}
            aria-label="model for new turns"
            title="model for new turns — pro is ~3x the price of flash"
            onChange={(e) => onChange(e.target.value)}>
      {m.choices.map((c) => <option key={c} value={c}>{short(c)}</option>)}
    </select>
  )
}

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
  const closeDrop = useCallback(() => setOpen(false), [])
  const wrapRef = useDismiss(open, closeDrop)

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
    <div className="notif-wrap vm-wrap" ref={wrapRef}>
      <button className="notif-bell" onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              aria-label={`guest VM — ${s.running ? 'running' : 'off'}`}
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

// Jarvis -> browser bridge: one SSE subscription per tab (/api/gui/stream).
// Tools push actions here: open a URL (popup-blocked -> clickable toast),
// play media in a floating dock, or nudge an open Workspace to reload its
// layout. Fire-and-forget — a missed event only matters on-screen.
function GuiBridge() {
  const [toasts, setToasts] = useState([])
  const [player, setPlayer] = useState(null)   // {kind, src, title}

  useEffect(() => {
    const es = new EventSource('/api/gui/stream')
    const toast = (t) => {
      const id = Math.random().toString(36).slice(2)
      setToasts((ts) => [...ts, { id, ...t }])
      setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), 15000)
    }
    es.onmessage = (m) => {
      let ev
      try { ev = JSON.parse(m.data) } catch { return }
      if (ev.type === 'open_url') {
        const w = window.open(ev.url, '_blank', 'noopener,noreferrer')
        if (!w) toast({ text: 'Jarvis wants to open', url: ev.url })
      } else if (ev.type === 'play_media') {
        setPlayer(ev)
      } else if (ev.type === 'layout_changed') {
        window.dispatchEvent(new CustomEvent('jarvis-layout-changed', { detail: ev }))
      }
    }
    return () => es.close()
  }, [])

  return (
    <>
      {player && (
        <div className="media-dock">
          <div className="row">
            <span className="grow ellipsis" title={player.title}>{player.title}</span>
            <button className="ghost" onClick={() => setPlayer(null)}>✕</button>
          </div>
          {player.kind === 'video'
            ? <video key={player.src} src={player.src} controls autoPlay />
            : <audio key={player.src} src={player.src} controls autoPlay />}
        </div>
      )}
      {toasts.length > 0 && (
        <div className={player ? 'gui-toasts raised' : 'gui-toasts'}>
          {toasts.map((t) => (
            <div key={t.id} className="gui-toast">
              {t.text}{' '}
              {t.url && <a href={t.url} target="_blank" rel="noopener noreferrer">{t.url}</a>}
            </div>
          ))}
        </div>
      )}
    </>
  )
}

export default function App() {
  const [user, setUser] = useState(undefined) // undefined = checking
  const [, setCfgReady] = useState(false) // bump once the media allowlist lands
  const [menuOpen, setMenuOpen] = useState(false) // mobile nav drawer
  const [moreOpen, setMoreOpen] = useState(false) // desktop overflow menu
  const [model, setModel] = useModel()
  const location = useLocation()

  // close both menus whenever the route changes
  useEffect(() => { setMenuOpen(false); setMoreOpen(false) }, [location.pathname])

  const closeMore = useCallback(() => setMoreOpen(false), [])
  const moreRef = useDismiss(moreOpen, closeMore)

  // the drawer is a fixed overlay; stop the page behind it from scrolling,
  // and let Escape dismiss it like every other popover in the bar
  useEffect(() => {
    document.body.classList.toggle('nav-locked', menuOpen)
    if (!menuOpen) return () => document.body.classList.remove('nav-locked')
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.classList.remove('nav-locked')
    }
  }, [menuOpen])

  useEffect(() => {
    api('/api/auth/me').then(setUser).catch(() => setUser(null))
    api('/api/config')
      .then((c) => { setMediaHosts(c.media_hosts); setCfgReady(true) })
      .catch(() => {})
  }, [])

  const logout = async () => {
    await api('/api/auth/logout', { method: 'POST' })
    setUser(null)
  }

  if (user === undefined) return <div className="center">…</div>
  if (user === null && location.pathname !== '/login')
    return <Navigate to="/login" replace />

  return (
    <div className="app">
      {user && (
        <>
          <nav className="nav">
            <span className="brand">Jarvis</span>

            {/* desktop: primary destinations + an overflow menu */}
            <div className="nav-links">
              {PRIMARY_LINKS.map((l) => (
                <NavLink key={l.to} to={l.to} end={l.end}>{l.label}</NavLink>
              ))}
              <div className="notif-wrap more-wrap" ref={moreRef}>
                <button className="nav-more" aria-expanded={moreOpen}
                        aria-haspopup="menu"
                        onClick={() => setMoreOpen((o) => !o)}>
                  More <span className={moreOpen ? 'chev open' : 'chev'}
                             aria-hidden="true">›</span>
                </button>
                {moreOpen && (
                  <div className="notif-drop more-drop" role="menu">
                    {MORE_LINKS.map((l) => (
                      <NavLink key={l.to} to={l.to} role="menuitem"
                               className="notif-item more-item"
                               onClick={closeMore}>{l.label}</NavLink>
                    ))}
                    <button className="notif-item more-item more-logout"
                            role="menuitem" onClick={logout}>Log out</button>
                  </div>
                )}
              </div>
            </div>

            <div className="nav-status">
              <ModelSwitch m={model} onChange={setModel} className="bar-only" />
              <NotificationBell />
              <TriagePanel />
              <VmStatus />
            </div>

            <button className="nav-toggle" aria-label={menuOpen ? 'close menu' : 'menu'}
                    aria-expanded={menuOpen}
                    onClick={() => setMenuOpen((o) => !o)}>
              {menuOpen ? '✕' : '☰'}
            </button>
          </nav>

          {/* phone: a fixed drawer over a scrim, never an in-flow block that
              shoves the page down */}
          {menuOpen && (
            <div className="nav-scrim" onClick={() => setMenuOpen(false)} />
          )}
          <div className={menuOpen ? 'nav-drawer open' : 'nav-drawer'}
               aria-hidden={!menuOpen}>
            {[...PRIMARY_LINKS, ...MORE_LINKS].map((l) => (
              <NavLink key={l.to} to={l.to} end={l.end}
                       tabIndex={menuOpen ? 0 : -1}>{l.label}</NavLink>
            ))}
            <div className="drawer-foot">
              <label className="drawer-model">
                <span className="dim small">Model for new turns</span>
                <ModelSwitch m={model} onChange={setModel} className="drawer-only" />
              </label>
              <button className="ghost" tabIndex={menuOpen ? 0 : -1}
                      onClick={logout}>Log out</button>
            </div>
          </div>
        </>
      )}
      {user && <GuiBridge />}
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
