import {
  createContext, useCallback, useEffect, useLayoutEffect, useRef, useState,
} from 'react'
import { createPortal } from 'react-dom'
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
import ComputerUse from './pages/ComputerUse.jsx'
import Notices, { useNotices } from './Notices.jsx'

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
  { to: '/computer', label: 'Computer use' },
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

// One glyph per primary destination. The nav lives in two places — the top bar
// and the chat sidebar's collapsed rail — and these are the constant that flies
// between them, so every placement must draw the same mark.
const ICONS = {
  '/': <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4L3 21l1.2-3.6A8.4 8.4 0 1 1 21 11.5Z" />,
  '/projects': <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v7.5a1.5
                        1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 17.5Z" />,
  '/agents': <><circle cx="12" cy="8.6" r="3.4" /><path d="M5.5 19.4a6.5 6.5 0 0 1 13 0" /></>,
  '/review': <path d="M12 3.2 19.2 6v5.6c0 4-3 7.2-7.2 9.2-4.2-2-7.2-5.2-7.2-9.2V6Zm-2.6
                      8.6 2 2.1 4-4.2" />,
}
const NavIcon = ({ to, innerRef }) => (
  <span className="nav-ico" ref={innerRef} aria-hidden="true">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      {ICONS[to]}
    </svg>
  </span>
)

// The nav's two homes exchange it through this: the Chat page hands up the DOM
// node inside its collapsed sidebar, and App portals the links into it. A slot
// means "render as a rail" — no second source of truth to keep in sync.
export const NavSlotContext = createContext(() => {})

// Light/dark switch. index.html stamps data-theme before first paint; this
// keeps it, localStorage and the browser-chrome colour in sync afterwards.
function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem('jarvis.theme', theme) } catch { /* private mode */ }
    document.querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', theme === 'light' ? '#ece7da' : '#0a0a0b')
  }, [theme])
  const toggle = useCallback(
    () => setTheme((t) => (t === 'light' ? 'dark' : 'light')), [])
  return [theme, toggle]
}

const SunIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" aria-hidden="true">
    <circle cx="12" cy="12" r="4.4" />
    <path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.2 5.2l2.1 2.1M16.7
             16.7l2.1 2.1M18.8 5.2l-2.1 2.1M7.3 16.7l-2.1 2.1" />
  </svg>
)
const MoonIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M20.6 14.2A8.8 8.8 0 0 1 9.8 3.4a8.8 8.8 0 1 0 10.8 10.8Z" />
  </svg>
)

function ThemeToggle({ theme, onToggle }) {
  const light = theme === 'light'
  return (
    <button className="nav-chip" onClick={onToggle}
            aria-label={light ? 'switch to dark theme' : 'switch to light theme'}
            title={light ? 'dark mode' : 'light mode'}>
      {light ? <MoonIcon /> : <SunIcon />}
    </button>
  )
}

// Guest-VM status (GET /api/vm/status) plus the one operator control: nuke —
// discard the overlay and reboot fresh from the golden image. Nuke is
// double-confirmed and refuses while a turn is in flight; boot/teardown stay
// elsewhere. The status read itself never mutates.
// (The runtime model switch lives in the chat composer now — ComposerModel in
// Chat.jsx; the bar copy was redundant and the operator asked for its removal.)
function VmStatus({ inBar = false }) {
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
    <div className={`notif-wrap vm-wrap${inBar ? '' : ' vm-corner'}`} ref={wrapRef}>
      <button className="nav-chip" onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              aria-label={`guest VM — ${s.running ? 'running' : 'off'}`}
              title="guest VM status">
        <span className={`run-dot ${s.running ? 'running' : ''}`} />
        <span className="vm-word">VM</span>
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
  const [theme, toggleTheme] = useTheme()
  const location = useLocation()
  // the Chat page publishes a mount point when its sidebar is collapsed; while
  // one exists the nav renders into it as a rail instead of onto the top bar
  const [navSlot, setNavSlot] = useState(null)
  const railed = !!navSlot
  // phone: the VM chip rides the top bar (operator's call) instead of holding
  // the bottom-left corner. One instance either way — it polls.
  const [phone, setPhone] = useState(
    () => window.matchMedia('(max-width: 768px)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 768px)')
    const h = (e) => setPhone(e.matches)
    mq.addEventListener('change', h)
    return () => mq.removeEventListener('change', h)
  }, [])
  const icoRefs = useRef(new Map())     // route -> icon element, for the FLIP
  const lastRects = useRef(null)

  // FLIP: the icons visibly travel between the rail and the bar rather than
  // vanishing from one and appearing in the other.
  //
  // The capture runs after EVERY render, not just when the placement changes.
  // Keyed on [railed] it also fired during App's pre-auth render, where there
  // is no nav at all — that cached an empty map, and the first bar -> rail move
  // had nothing to fly from (only rail -> bar animated). Re-measuring each pass
  // also keeps the rects honest when the Review count resizes the bar.
  //
  // Detached rects are ignored, and an empty pass never overwrites a good one.
  // Navigating off Chat leaves one commit where the portal still targets the
  // slot node the unmounting page just took with it: the icons measure (0,0)
  // there, and caching that made the return flight start from the top-left
  // corner instead of the rail — the icons snapped to the corner and flew in
  // from there, which is what read as jumpy. Only that direction was affected,
  // which is why toggling the sidebar always looked fine.
  const prevRailed = useRef(railed)
  useLayoutEffect(() => {
    const now = new Map()
    icoRefs.current.forEach((el, key) => {
      if (!el || !el.isConnected) return
      const r = el.getBoundingClientRect()
      if (!r.width && !r.height) return
      now.set(key, r)
    })
    const before = lastRects.current
    const moved = prevRailed.current !== railed
    prevRailed.current = railed
    if (now.size) lastRects.current = now
    if (!moved || !before || !before.size || !now.size) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    now.forEach((to, key) => {
      const from = before.get(key)
      const el = icoRefs.current.get(key)
      if (!from || !el) return
      const dx = from.left - to.left
      const dy = from.top - to.top
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return
      el.animate(
        [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }],
        // matches the bar's fold: an ease-in-out, not the front-loaded curve
        // that made both read as a snap followed by a crawl
        { duration: 420, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' },
      )
    })
  })

  // toasts + the pending count that lives on the Review nav link
  const notices = useNotices(!!user)

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

  // the same links either way — only the container and the label's visibility
  // differ, which is what lets the icons fly between the two
  const navLinks = (
    <>
      {PRIMARY_LINKS.map((l) => (
        <NavLink key={l.to} to={l.to} end={l.end} title={l.label}>
          <NavIcon to={l.to} innerRef={(el) => {
            if (el) icoRefs.current.set(l.to, el)
            else icoRefs.current.delete(l.to)
          }} />
          <span className="nav-label">{l.label}</span>
          {l.to === '/review' && notices.count > 0 && (
            <span className="nav-count">{badge(notices.count)}</span>)}
        </NavLink>
      ))}
      <div className="notif-wrap more-wrap" ref={moreRef}>
        <button className="nav-more" aria-expanded={moreOpen} aria-haspopup="menu"
                title="More" onClick={() => setMoreOpen((o) => !o)}>
          <span className="nav-ico" aria-hidden="true">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="currentColor">
              <circle cx="5.5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" />
              <circle cx="18.5" cy="12" r="1.6" />
            </svg>
          </span>
          <span className="nav-label">More</span>
          <span className={moreOpen ? 'chev open' : 'chev'} aria-hidden="true">›</span>
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
    </>
  )

  return (
    <div className={railed ? 'app railed' : 'app'}>
      {user && (
        <>
          {/* Railed: the bar is gone and the links live in the chat sidebar,
              portaled into the slot it published. Otherwise the usual top bar.
              Review wears the pending count — the bell's old job. */}
          {/* the bar always exists and folds to zero height instead of being
              torn out — otherwise the page below jumped 56px the instant the
              rail handed the nav back, which read as a jolt under the icons'
              flight. Empty while folded; the links are in the rail. */}
          <nav className={railed ? 'nav folded' : 'nav'} aria-hidden={railed}>
            {!railed && <>
              <span className="brand">Jarvis</span>
              <div className="nav-links">{navLinks}</div>
              <div className="nav-status">
                {phone && <VmStatus inBar />}
                <ThemeToggle theme={theme} onToggle={toggleTheme} />
              </div>
              <button className="nav-toggle"
                      aria-label={menuOpen ? 'close menu' : 'menu'}
                      aria-expanded={menuOpen}
                      onClick={() => setMenuOpen((o) => !o)}>
                {menuOpen ? '✕' : '☰'}
              </button>
            </>}
          </nav>
          {railed && createPortal(
            <>
              <div className="rail-links">{navLinks}</div>
              <span className="grow" />
              <ThemeToggle theme={theme} onToggle={toggleTheme} />
            </>, navSlot)}

          {/* phone: a fixed drawer over a scrim, never an in-flow block that
              shoves the page down */}
          {menuOpen && (
            <div className="nav-scrim" onClick={() => setMenuOpen(false)} />
          )}
          <div className={menuOpen ? 'nav-drawer open' : 'nav-drawer'}
               aria-hidden={!menuOpen}>
            {[...PRIMARY_LINKS, ...MORE_LINKS].map((l) => (
              <NavLink key={l.to} to={l.to} end={l.end}
                       tabIndex={menuOpen ? 0 : -1}>
                {l.label}
                {l.to === '/review' && notices.count > 0 && (
                  <span className="nav-count">{badge(notices.count)}</span>)}
              </NavLink>
            ))}
            <div className="drawer-foot">
              <button className="ghost" tabIndex={menuOpen ? 0 : -1}
                      onClick={toggleTheme}>
                {theme === 'light' ? 'Dark mode' : 'Light mode'}</button>
              <button className="ghost" tabIndex={menuOpen ? 0 : -1}
                      onClick={logout}>Log out</button>
            </div>
          </div>
        </>
      )}
      {user && <GuiBridge />}
      {user && <Notices toasts={notices.toasts} dismiss={notices.dismiss}
                        clear={notices.clear} />}
      {/* the guest VM sits on its own in the bottom-left corner, off away from
          the destinations and the page's own controls — on a phone it moved
          into the top bar above */}
      {user && !phone && <VmStatus />}
      <NavSlotContext.Provider value={setNavSlot}>
      <Routes>
        <Route path="/login" element={<Login onLogin={setUser} />} />
        <Route path="/" element={<Chat />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/projects/:slug" element={<Workspace />} />
        <Route path="/artifacts" element={<Artifacts />} />
        <Route path="/review" element={<Review />} />
        <Route path="/network" element={<Network />} />
        <Route path="/computer" element={<ComputerUse />} />
        <Route path="/context" element={<Context />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/logs" element={<Logs />} />
        <Route path="/schedules" element={<Schedules />} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/tools" element={<Tools />} />
      </Routes>
      </NavSlotContext.Provider>
    </div>
  )
}
