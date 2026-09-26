import {
  useCallback, useEffect, useLayoutEffect, useRef, useState,
} from 'react'
import { createPortal } from 'react-dom'
import { NavLink, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { api } from './api.js'
import { subscribe } from './events.js'
import { setMediaHosts } from './mediaHosts.js'
import Player from './Player.jsx'
import AppRoutes from './routes.jsx'
import {
  MoreIcon, NAV_ITEMS, NavIcon, NavItem, NavList, NavSlotContext, OVERFLOW_ITEMS,
  PRIMARY_ITEMS,
} from './nav.jsx'
import { AuthContext } from './auth.jsx'
import Menu from './components/Menu.jsx'
import Notices, { PendingCountContext, useNotices } from './Notices.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import { followBoxes, listBoxes } from './boxes/api/vms.js'
import { navState } from './boxes/logic.js'
import { AskProvider } from './ask.jsx'

// The destinations, their icons and their split between the bar and the ⋯
// menu all live in nav.jsx, which is the ONE source the bar, the rail, the
// overflow menu and the phone drawer are rendered from. They used to be three
// hand-maintained copies here. The routes live in routes.jsx.

// Light/dark switch. index.html stamps data-theme before first paint; this
// keeps it, localStorage and the browser-chrome colour in sync afterwards.
function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem('jarvis.theme', theme) } catch { /* private mode */ }
    // the browser chrome takes the page background — read from the token
    // once the theme attribute has switched it, so the two cannot drift
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
    if (bg) document.querySelector('meta[name="theme-color"]')?.setAttribute('content', bg)
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
    <button className="nav-chip theme-chip" onClick={onToggle}
            aria-label={light ? 'switch to dark theme' : 'switch to light theme'}
            title={light ? 'dark mode' : 'light mode'}>
      {light ? <MoonIcon /> : <SunIcon />}
    </button>
  )
}

// The VMs nav item's state dot (it replaced the old VM chip here, whose
// explainer, nuke and rebuild moved to the /vms page). One light poll of the
// boxes list; the word comes from boxes/logic.navState.
function useVmDot(enabled) {
  const [dot, setDot] = useState('off')
  useEffect(() => {
    if (!enabled) return undefined
    let live = true
    const load = () => listBoxes().then((r) => { if (live) setDot(navState(r)) }).catch(() => {})
    load()
    const t = setInterval(load, 15000)
    const stop = followBoxes(load)          // box_up / box_down on the shared stream
    return () => { live = false; clearInterval(t); stop() }
  }, [enabled])
  return dot
}

// Jav3 -> browser bridge: the gui topic of the shared event stream.
// Tools push actions here: open a URL (popup-blocked -> clickable toast),
// play media in a floating dock, or nudge an open Workspace to reload its
// layout. Fire-and-forget — a missed event only matters on-screen.
function GuiBridge() {
  const [toasts, setToasts] = useState([])
  const [player, setPlayer] = useState(null)   // {kind, src, title}

  useEffect(() => {
    // the gui topic of the one shared per-browser connection (src/events.js);
    // an event the host addressed to ONE machine (by this tab's id, src/tab.js)
    // is only delivered in that tab
    const toast = (t) => {
      const id = Math.random().toString(36).slice(2)
      setToasts((ts) => [...ts, { id, ...t }])
      setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), 15000)
    }
    return subscribe('gui', (ev) => {
      if (!ev) return
      if (ev.type === 'open_url') {
        const w = window.open(ev.url, '_blank', 'noopener,noreferrer')
        if (!w) toast({ text: 'Jav3 wants to open', url: ev.url })
      } else if (ev.type === 'play_media') {
        setPlayer(ev)
      } else if (ev.type === 'player') {
        // the music player owns its own state machine — hand it the event
        // rather than threading a queue through here
        window.dispatchEvent(new CustomEvent('jarvis-player', { detail: ev }))
      } else if (ev.type === 'layout_changed') {
        window.dispatchEvent(new CustomEvent('jarvis-layout-changed', { detail: ev }))
      }
    })
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
  // first run: no login exists yet, so every route funnels to /setup
  const [setupNeeded, setSetupNeeded] = useState(undefined)
  const [, setCfgReady] = useState(false) // bump once the media allowlist lands
  const [menuOpen, setMenuOpen] = useState(false) // mobile nav drawer
  const [moreOpen, setMoreOpen] = useState(false) // desktop overflow menu
  const [theme, toggleTheme] = useTheme()
  const location = useLocation()
  const navigate = useNavigate()
  // the Chat page publishes a mount point when its sidebar is collapsed; while
  // one exists the nav renders into it as a rail instead of onto the top bar
  const [navSlot, setNavSlot] = useState(null)
  const railed = !!navSlot
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
  const vmDot = useVmDot(!!user)

  // close both menus whenever the route changes
  useEffect(() => { setMenuOpen(false); setMoreOpen(false) }, [location.pathname])

  const closeMore = useCallback(() => setMoreOpen(false), [])

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
    // an older server without the route (or any failure) means "not needed":
    // the worst case is the ordinary login page, never a trap on /setup
    api('/api/setup/status')
      .then((r) => setSetupNeeded(!!r.needed))
      .catch(() => setSetupNeeded(false))
    api('/api/config')
      .then((c) => {
        setMediaHosts(c.media_hosts)
        setCfgReady(true)
      })
      .catch(() => {})
  }, [])

  const logout = useCallback(async () => {
    await api('/api/auth/logout', { method: 'POST' })
    setUser(null)
  }, [])

  if (user === undefined || setupNeeded === undefined)
    return <div className="center">…</div>
  if (setupNeeded && location.pathname !== '/setup') return <Navigate to="/setup" replace />
  if (!setupNeeded && location.pathname === '/setup')
    return <Navigate to={user ? '/' : '/login'} replace />
  // The page being asked for rides along, so logging in lands back on it
  // instead of always on Chat.
  if (user === null && !setupNeeded && location.pathname !== '/login')
    return <Navigate to="/login" replace state={{ from: location.pathname }} />

  const counts = { review: notices.count }
  const dots = { vms: vmDot }

  // The phone drawer's way into chat history. On a phone the Chat sidebar is
  // an off-canvas sheet, and an edge swipe was the only way to open it — an
  // invisible gesture is not an affordance. The stored side state covers
  // arriving from another page (Chat mounts reading it); the event covers
  // already being on Chat.
  const openChats = () => {
    try { localStorage.setItem('jarvis.chat.side', 'open') } catch { /* private mode */ }
    setMenuOpen(false)
    if (location.pathname !== '/') navigate('/')
    window.dispatchEvent(new Event('jarvis-open-chats'))
  }
  const chatItem = NAV_ITEMS.find((i) => i.to === '/')

  // The bar and the rail render the SAME markup — only the container's class
  // and the label's visibility differ, which is what lets the icons fly
  // between the two placements. The ⋯ menu is the glass Menu primitive with
  // the same NavItem rows, so an overflow destination looks like a bar one.
  const navLinks = (
    <>
      {PRIMARY_ITEMS.map((item) => (
        <NavItem key={item.to} item={item} count={counts[item.count] || 0}
                 dot={dots[item.dot]}
                 iconRef={(el) => {
                   if (el) icoRefs.current.set(item.to, el)
                   else icoRefs.current.delete(item.to)
                 }} />
      ))}
      <Menu open={moreOpen} onClose={closeMore} label="More" width={200}
            className="nav-menu" wrapClassName="more-wrap"
            trigger={(
              <button className="nav-more" aria-expanded={moreOpen} aria-haspopup="menu"
                      aria-label="More" title="More" onClick={() => setMoreOpen((o) => !o)}>
                <MoreIcon />
              </button>
            )}>
        <NavList items={OVERFLOW_ITEMS} itemClassName="menu-item" itemRole="menuitem"
                 counts={counts} dots={dots} onNavigate={closeMore} />
      </Menu>
    </>
  )

  return (
    <AskProvider>
    <AuthContext.Provider value={{ user, logout }}>
    <PendingCountContext.Provider value={notices.count}>
    <div className={railed ? 'app railed' : 'app'}>
      {user && (
        <>
          {/* Railed: the bar is gone and the links live in the chat sidebar,
              portaled into the slot it published. Otherwise the usual top bar.
              Security wears the pending count — the bell's old job. */}
          {/* the bar always exists and folds to zero height instead of being
              torn out — otherwise the page below jumped 56px the instant the
              rail handed the nav back, which read as a jolt under the icons'
              flight. Empty while folded; the links are in the rail. */}
          <nav className={railed ? 'nav folded' : 'nav'} aria-hidden={railed}>
            {!railed && <>
              <span className="brand">Jav3</span>
              <div className="nav-links">{navLinks}</div>
              <div className="nav-status">
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
              shoves the page down. It is the ONLY navigation a phone has, so
              it shows the whole map — primaries and overflow alike — where it
              used to be a separately-written flat list that had already
              drifted (its own logout, no icons on any row). */}
          {menuOpen && (
            <div className="nav-scrim" onClick={() => setMenuOpen(false)} />
          )}
          <div className={menuOpen ? 'nav-drawer open' : 'nav-drawer'}
               aria-hidden={!menuOpen}>
            <NavList items={[chatItem]} counts={counts} dots={dots} tabIndex={menuOpen ? 0 : -1} />
            <button type="button" className="drawer-row" tabIndex={menuOpen ? 0 : -1}
                    onClick={openChats}>
              <NavIcon name="history" />
              <span className="nav-label">Chat history</span>
            </button>
            <NavList items={NAV_ITEMS.filter((i) => i !== chatItem)} counts={counts} dots={dots}
                     tabIndex={menuOpen ? 0 : -1} />
            {/* the drawer's one theme control — a row like the rest, not a
                centred white slab; the bar's own toggle hides while the
                drawer is the navigation */}
            <div className="drawer-foot">
              <button type="button" className="drawer-row" tabIndex={menuOpen ? 0 : -1}
                      onClick={toggleTheme}>
                <span className="nav-ico" aria-hidden="true">
                  {theme === 'light' ? <MoonIcon /> : <SunIcon />}</span>
                <span className="nav-label">
                  {theme === 'light' ? 'Dark mode' : 'Light mode'}</span>
              </button>
            </div>
          </div>
        </>
      )}
      {user && <GuiBridge />}
      {/* the music player renders nothing until something is queued into it */}
      {user && <Player />}
      {user && <Notices toasts={notices.toasts} dismiss={notices.dismiss}
                        clear={notices.clear} count={notices.count} />}
      <NavSlotContext.Provider value={setNavSlot}>
      {/* Inside the provider and around the routes only: the nav, the player
          and the toasts stay mounted through a page's failure, so there is
          always a way out of a broken page. */}
      <ErrorBoundary resetKey={location.pathname}>
      <AppRoutes onLogin={setUser} authed={!!user}
                 onSetup={(u) => { setSetupNeeded(false); setUser(u) }} />
      </ErrorBoundary>
      </NavSlotContext.Provider>
    </div>
    </PendingCountContext.Provider>
    </AuthContext.Provider>
    </AskProvider>
  )
}
