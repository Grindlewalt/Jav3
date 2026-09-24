import { Suspense, lazy, useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import Login from './pages/Login.jsx'
import Chat from './pages/Chat.jsx'
import NotFound from './pages/NotFound.jsx'

// The routing table, in the order the nav lists it (see nav.jsx): the six
// primaries, then the ⋯ overflow, then the routes that are reachable but not
// advertised, then the catch-all. Fifteen routes used to sit in one
// undifferentiated block with no 404, so an unknown path rendered the chrome
// around nothing at all and looked precisely like a page that had failed to
// load.
//
// Login and Chat are eager: Login is the first thing an unauthenticated visit
// needs, and Chat is `/`, which is where almost every session starts.
// Everything else is a chunk — Workspace alone is 1,600 lines the chat page
// has no use for.
//
// The chunks are then pulled in on idle, right after the first page settles.
// Without that, every first visit to a route pays a blank frame while its
// chunk arrives; with it, the fallback realistically never renders. It starts
// on idle rather than on mount so it cannot compete with the first paint, and
// a browser without requestIdleCallback just gets a short timer.
const Projects = lazy(() => import('./pages/Projects.jsx'))
const Workspace = lazy(() => import('./pages/Workspace.jsx'))
const Agents = lazy(() => import('./pages/Agents.jsx'))
const Tools = lazy(() => import('./pages/Tools.jsx'))
const Settings = lazy(() => import('./pages/Settings.jsx'))

// Review is a layout route: the shell (title + tab strip) is the default
// export and the queue is the index child, so the tabs are real URLs —
// linkable, and NavLink lights the current one for free.
const Review = lazy(() => import('./pages/Review.jsx'))
const ReviewHome = lazy(() => import('./pages/Review.jsx')
  .then((m) => ({ default: m.ReviewHome })))
const Network = lazy(() => import('./pages/Network.jsx'))
const Logs = lazy(() => import('./pages/Logs.jsx'))
const SecretsPanel = lazy(() => import('./SecretsPanel.jsx'))

const Memory = lazy(() => import('./pages/Memory.jsx'))
const Schedules = lazy(() => import('./pages/Schedules.jsx'))

// reachable, not advertised
const Skills = lazy(() => import('./pages/Skills.jsx'))
const Voice = lazy(() => import('./pages/Voice.jsx'))
const Artifacts = lazy(() => import('./pages/Artifacts.jsx'))

const PREFETCH = [
  () => import('./pages/Projects.jsx'), () => import('./pages/Workspace.jsx'),
  () => import('./pages/Agents.jsx'), () => import('./pages/Review.jsx'),
  () => import('./pages/Tools.jsx'), () => import('./pages/Settings.jsx'),
  () => import('./pages/Network.jsx'), () => import('./pages/Logs.jsx'),
  () => import('./SecretsPanel.jsx'), () => import('./pages/Memory.jsx'),
  () => import('./pages/Schedules.jsx'), () => import('./pages/Skills.jsx'),
  () => import('./pages/Voice.jsx'), () => import('./pages/Artifacts.jsx'),
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

export default function AppRoutes({ onLogin, authed }) {
  usePrefetchRoutes(authed)
  return (
    // The fallback paints nothing rather than a spinner: it is on screen for a
    // few tens of milliseconds at most, and a control that appears and vanishes
    // that fast reads as a flicker, not as progress.
    <Suspense fallback={<div className="route-pending" aria-busy="true" />}>
      <Routes>
        <Route path="/login" element={<Login onLogin={onLogin} />} />

        {/* the bar */}
        <Route path="/" element={<Chat />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/projects/:slug" element={<Workspace />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/review" element={<Review />}>
          <Route index element={<ReviewHome />} />
          <Route path="network" element={<Network />} />
          <Route path="logs" element={<Logs />} />
          <Route path="secrets" element={<SecretsPanel />} />
        </Route>
        <Route path="/tools" element={<Tools />} />
        <Route path="/settings" element={<Settings />} />

        {/* the ⋯ menu */}
        <Route path="/memory" element={<Memory />} />
        <Route path="/schedules" element={<Schedules />} />

        {/* the old addresses keep working: bookmarks, toasts, muscle memory */}
        <Route path="/network" element={<Navigate to="/review/network" replace />} />
        <Route path="/logs" element={<Navigate to="/review/logs" replace />} />
        <Route path="/context" element={<Navigate to="/memory" replace />} />

        {/* reachable, not advertised */}
        <Route path="/skills" element={<Skills />} />
        <Route path="/voice" element={<Voice />} />
        <Route path="/artifacts" element={<Artifacts />} />

        <Route path="*" element={<NotFound />} />
      </Routes>
    </Suspense>
  )
}
