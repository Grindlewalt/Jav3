import { useEffect, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api.js'
import Login from './pages/Login.jsx'
import Chat from './pages/Chat.jsx'
import Projects from './pages/Projects.jsx'
import ProjectDetail from './pages/ProjectDetail.jsx'

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
          <Link to="/">Chat</Link>
          <Link to="/projects">Projects</Link>
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
        <Route path="/projects/:slug" element={<ProjectDetail />} />
      </Routes>
    </div>
  )
}
