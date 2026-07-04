import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const navigate = useNavigate()

  async function submit(e) {
    e.preventDefault()
    setError(null)
    try {
      const res = await api('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      })
      onLogin({ username: res.username })
      navigate('/')
    } catch (err) {
      setError(err.detail || 'login failed')
    }
  }

  return (
    <div className="center">
      <form className="login" onSubmit={submit}>
        <h1>Jarvis</h1>
        <input placeholder="username" value={username}
               onChange={(e) => setUsername(e.target.value)} autoFocus />
        <input type="password" placeholder="password" value={password}
               onChange={(e) => setPassword(e.target.value)} />
        {error && <div className="error">{error}</div>}
        <button type="submit">Log in</button>
      </form>
    </div>
  )
}
