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
import Jobs from './pages/Jobs.jsx'
import Logs from './pages/Logs.jsx'

function NotificationBell() {
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const load = () => api('/api/notifications').then(setData).catch(() => {})
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])
  const count = data?.count || 0
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
          {(data?.staged || []).map((s) => (
            <NavLink key={`st${s.project}`} to={`/projects/${s.project}`} className="notif-item" onClick={close}>
              <span className="grow ellipsis">staged changes · {s.project} · {s.files} file{s.files === 1 ? '' : 's'}</span>
            </NavLink>
          ))}
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

export default function App() {
  const [user, setUser] = useState(undefined) // undefined = checking
  const [, setCfgReady] = useState(false) // bump once the media allowlist lands
  const location = useLocation()

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
        <nav>
          <span className="brand">Jarvis</span>
          <NavLink to="/" end>Chat</NavLink>
          <NavLink to="/projects">Projects</NavLink>
          <NavLink to="/artifacts">Artifacts</NavLink>
          <NavLink to="/context">Context</NavLink>
          <NavLink to="/agents">Agents</NavLink>
          <NavLink to="/logs">Logs</NavLink>
          <NavLink to="/jobs">Jobs</NavLink>
          <NavLink to="/schedules">Schedules</NavLink>
          <NavLink to="/skills">Skills</NavLink>
          <NavLink to="/tools">Tools</NavLink>
          <NotificationBell />
          <button
            className="link"
            onClick={async () => {
              await api('/api/auth/logout', { method: 'POST' })
              setUser(null)
            }}
          >
            Logout
          </button>
        </nav>
      )}
      <Routes>
        <Route path="/login" element={<Login onLogin={setUser} />} />
        <Route path="/" element={<Chat />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/projects/:slug" element={<Workspace />} />
        <Route path="/artifacts" element={<Artifacts />} />
        <Route path="/context" element={<Context />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/logs" element={<Logs />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/schedules" element={<Schedules />} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/tools" element={<Tools />} />
      </Routes>
    </div>
  )
}
