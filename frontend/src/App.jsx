import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api.js'
import Login from './pages/Login.jsx'
import Chat from './pages/Chat.jsx'
import Projects from './pages/Projects.jsx'
import Workspace from './pages/Workspace.jsx'
import Context from './pages/Context.jsx'
import Skills from './pages/Skills.jsx'
import Tools from './pages/Tools.jsx'
import Agents from './pages/Agents.jsx'
import Schedules from './pages/Schedules.jsx'
import Runs from './pages/Runs.jsx'
import Jobs from './pages/Jobs.jsx'

export default function App() {
  const [user, setUser] = useState(undefined) // undefined = checking
  const location = useLocation()

  useEffect(() => {
    api('/api/auth/me').then(setUser).catch(() => setUser(null))
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
          <NavLink to="/context">Context</NavLink>
          <NavLink to="/agents">Agents</NavLink>
          <NavLink to="/runs">Runs</NavLink>
          <NavLink to="/jobs">Jobs</NavLink>
          <NavLink to="/schedules">Schedules</NavLink>
          <NavLink to="/skills">Skills</NavLink>
          <NavLink to="/tools">Tools</NavLink>
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
        <Route path="/context" element={<Context />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/runs" element={<Runs />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/schedules" element={<Schedules />} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/tools" element={<Tools />} />
      </Routes>
    </div>
  )
}
