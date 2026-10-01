import { Suspense, lazy, useEffect } from 'react'
import { Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom'
import Login from './pages/Login.jsx'
import Work from './work/Work.jsx'
import NotFound from './pages/NotFound.jsx'

// The routing table, in the order the nav lists it (see nav.jsx): the six
// primaries, then the ⋯ overflow, then the routes that are reachable but not
// advertised, then the catch-all. Fifteen routes used to sit in one
// undifferentiated block with no 404, so an unknown path rendered the chrome
// around nothing at all and looked precisely like a page that had failed to
// load.
//
// Login and Work are eager: Login is the first thing an unauthenticated visit
// needs, and Work (the chat plus its project's windows) is `/`, which is where
// almost every session starts. Everything else is a chunk.
//
// The chunks are then pulled in on idle, right after the first page settles.
// Without that, every first visit to a route pays a blank frame while its
// chunk arrives; with it, the fallback realistically never renders. It starts
// on idle rather than on mount so it cannot compete with the first paint, and
// a browser without requestIdleCallback just gets a short timer.
// first run only, so never prefetched
function OpenChat() {
  const { id } = useParams()
  const step = new URLSearchParams(useLocation().search).get('step')
  if (/^\d+$/.test(id || '')) {
    try { localStorage.setItem('jarvis.chat.last', id) } catch { /* private mode */ }
    // /c/<id>?step=<n>: a security card's "Open chat at step". Chat scrolls to it
    // once the transcript is loaded (pages/Chat.jsx), then forgets it.
    try {
      if (/^\d+$/.test(step || '')) sessionStorage.setItem('jarvis.chat.step', `${id}:${step}`)
    } catch { /* private mode */ }
  }
  return <Navigate to="/" replace />
}

const Setup = lazy(() => import('./pages/Setup.jsx'))
// Agents is a layout route like Review: the shell + tab strip is the default
// export, the definitions editor the index child, Skills and Outputs siblings.
const Agents = lazy(() => import('./pages/Agents.jsx'))
const AgentDefinitions = lazy(() => import('./pages/Agents.jsx')
  .then((m) => ({ default: m.AgentDefinitions })))
const SkillsPanel = lazy(() => import('./SkillsPanel.jsx'))
const AgentOutputs = lazy(() => import('./AgentOutputs.jsx'))
const Tools = lazy(() => import('./pages/Tools.jsx'))
// Settings is a layout route like Agents: the shell + tab strip is the default
// export, Models the index child, Alerts / Access / System siblings.
const Settings = lazy(() => import('./pages/Settings.jsx'))
const SettingsModels = lazy(() => import('./pages/Settings.jsx')
  .then((m) => ({ default: m.ModelsTab })))
const SettingsAlerts = lazy(() => import('./pages/Settings.jsx')
  .then((m) => ({ default: m.AlertsTab })))
const SettingsAccess = lazy(() => import('./pages/Settings.jsx')
  .then((m) => ({ default: m.AccessTab })))
const SettingsSystem = lazy(() => import('./pages/Settings.jsx')
  .then((m) => ({ default: m.SystemTab })))

// Review is a layout route: the shell (title + tab strip) is the default
// export and the queue is the index child, so the tabs are real URLs —
// linkable, and NavLink lights the current one for free.
const Review = lazy(() => import('./pages/Review.jsx'))
const ReviewHome = lazy(() => import('./pages/Review.jsx')
  .then((m) => ({ default: m.ReviewHome })))
const Network = lazy(() => import('./pages/Network.jsx'))
const Logs = lazy(() => import('./pages/Logs.jsx'))
const SecretsPanel = lazy(() => import('./SecretsPanel.jsx'))
const Persistent = lazy(() => import('./pages/Persistent.jsx'))
const Profiles = lazy(() => import('./pages/Profiles.jsx'))
// the VM manager (DESIGN-BOXES (e)): a layout route like Security, tabs
// Boxes · Images · Catalogue
const Vms = lazy(() => import('./pages/Vms.jsx'))
const VmBoxes = lazy(() => import('./pages/Vms.jsx').then((m) => ({ default: m.Boxes })))
const VmImages = lazy(() => import('./pages/Vms.jsx').then((m) => ({ default: m.Images })))
const Catalogue = lazy(() => import('./pages/Catalogue.jsx'))

const Memory = lazy(() => import('./pages/Memory.jsx'))
// the terminal-style shell, on trial beside the classic UI: /shell, a chat at
// /shell/c/:id, a project's home at /shell/p/:slug — one route, parsed inside,
// so moving between them never remounts it mid-stream
const Shell = lazy(() => import('./shell/Shell.jsx'))
const Schedules = lazy(() => import('./pages/Schedules.jsx'))
const Git = lazy(() => import('./pages/Git.jsx'))

// reachable, not advertised
const Voice = lazy(() => import('./pages/Voice.jsx'))
const Artifacts = lazy(() => import('./pages/Artifacts.jsx'))

const PREFETCH = [
  () => import('./work/windows.jsx'), () => import('./pages/Projects.jsx'),
  () => import('./pages/Agents.jsx'), () => import('./pages/Review.jsx'),
  () => import('./pages/Tools.jsx'), () => import('./pages/Settings.jsx'),
  () => import('./pages/Network.jsx'), () => import('./pages/Logs.jsx'),
  () => import('./SecretsPanel.jsx'), () => import('./pages/Memory.jsx'),
  () => import('./pages/Schedules.jsx'), () => import('./SkillsPanel.jsx'),
  () => import('./AgentOutputs.jsx'), () => import('./shell/Shell.jsx'),
  () => import('./pages/Voice.jsx'), () => import('./pages/Artifacts.jsx'),
  () => import('./pages/Vms.jsx'), () => import('./pages/Catalogue.jsx'),
  () => import('./pages/Persistent.jsx'), () => import('./pages/Profiles.jsx'),
  () => import('./pages/Git.jsx'),
]

function usePrefetchRoutes(enabled) {
  useEffect(() => {
    if (!enabled) return undefined
    let cancelled = false
    const run = () => { if (!cancelled) PREFETCH.forEach((load) => { load() }) }
    const idle = window.requestIdleCallback
    const id = idle ? idle(run, { timeout: 3000 }) : setTimeout(run, 1200)
    return () => {
      cancelled = true
      if (idle) window.cancelIdleCallback?.(id)
      else clearTimeout(id)
    }
  }, [enabled])
}

export default function AppRoutes({ onLogin, onSetup, authed }) {
  usePrefetchRoutes(authed)
  return (
    // The fallback paints nothing rather than a spinner: it is on screen for a
    // few tens of milliseconds at most, and a control that appears and vanishes
    // that fast reads as a flicker, not as progress.
    <Suspense fallback={<div className="route-pending" aria-busy="true" />}>
      <Routes>
        <Route path="/login" element={<Login onLogin={onLogin} />} />
        <Route path="/setup" element={<Setup onDone={onSetup} />} />

        {/* the bar */}
        {/* Work: the chat, with the chat's project's windows beside it. The
            old Chat and Projects destinations both land here: a project's
            board link puts that project on the chat (Work then replaces the
            address with /), and /projects opens the Projects sheet. Every
            one renders the same <Work />, so React keeps it mounted across
            the hop and a chat mid-stream is not torn down. */}
        <Route path="/" element={<Work />} />
        <Route path="/work" element={<Navigate to="/" replace />} />
        {/* /c/<id>: open one chat in Work (the terminal client's /web). Chat
            resumes the id stored under jarvis.chat.last on mount. */}
        <Route path="/c/:id" element={<OpenChat />} />
        <Route path="/projects" element={<Work openProjects />} />
        <Route path="/projects/:slug" element={<Work />} />
        <Route path="/agents" element={<Agents />}>
          <Route index element={<AgentDefinitions />} />
          <Route path="skills" element={<SkillsPanel />} />
          <Route path="outputs" element={<AgentOutputs />} />
          <Route path="outputs/:slug" element={<AgentOutputs />} />
        </Route>
        <Route path="/security" element={<Review />}>
          <Route index element={<ReviewHome />} />
          <Route path="persistent" element={<Persistent />} />
          <Route path="network" element={<Network />} />
          <Route path="profiles" element={<Profiles />} />
          <Route path="logs" element={<Logs />} />
          <Route path="secrets" element={<SecretsPanel />} />
        </Route>
        <Route path="/vms" element={<Vms />}>
          <Route index element={<VmBoxes />} />
          <Route path="images" element={<VmImages />} />
          <Route path="catalogue" element={<Catalogue />} />
        </Route>
        <Route path="/tools" element={<Tools />} />
        <Route path="/settings" element={<Settings />}>
          <Route index element={<SettingsModels />} />
          <Route path="alerts" element={<SettingsAlerts />} />
          <Route path="access" element={<SettingsAccess />} />
          <Route path="system" element={<SettingsSystem />} />
        </Route>

        {/* the ⋯ menu */}
        <Route path="/memory" element={<Memory />} />
        <Route path="/schedules" element={<Schedules />} />
        <Route path="/git" element={<Git />} />
        <Route path="/git/:slug" element={<Git />} />
        <Route path="/shell/*" element={<Shell />} />

        {/* the old addresses keep working: bookmarks, toasts, muscle memory */}
        <Route path="/review/*" element={<ReviewMoved />} />
        <Route path="/network" element={<Navigate to="/security/network" replace />} />
        <Route path="/logs" element={<Navigate to="/security/logs" replace />} />
        <Route path="/context" element={<Navigate to="/memory" replace />} />
        <Route path="/skills" element={<Navigate to="/agents/skills" replace />} />

        {/* reachable, not advertised */}
        <Route path="/voice" element={<Voice />} />
        <Route path="/artifacts" element={<Artifacts />} />

        <Route path="*" element={<NotFound />} />
      </Routes>
    </Suspense>
  )
}

// Review was renamed Security. /review and every /review/<tab> land on the
// same tab under /security, and the navigation state rides along — a toast's
// `openEvent` deep link still opens its evidence board after the redirect.
function ReviewMoved() {
  const loc = useLocation()
  const to = loc.pathname.replace(/^\/review/, '/security') + loc.search + loc.hash
  return <Navigate to={to} state={loc.state} replace />
}
