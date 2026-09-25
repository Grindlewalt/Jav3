import { useEffect, useState } from 'react'
import { api } from '../api.js'
import EmptyState from '../components/EmptyState.jsx'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// The session spine: the goal, the plan (shared todos), and this project's
// live runs — one glance says what we're doing, where we are, what's moving.
// The goal persists with the board layout (.workspace.json panel state).
export default function TaskBoardPanel({ slug, state, setState }) {
  const [todos, setTodos] = useState([])
  const [text, setText] = useState('')
  const [jobs, setJobs] = useState([])

  const refreshTodos = () =>
    api(`/api/projects/${slug}/todos`).then((r) => setTodos(r.todos))
  const refreshJobs = () =>
    api('/api/jobs').then((r) =>
      setJobs((r.jobs || []).filter((j) => j.project === slug))).catch(() => {})
  useEffect(() => {
    refreshTodos(); refreshJobs()
    const t = setInterval(refreshJobs, 5000)
    const h = () => refreshTodos()
    window.addEventListener('jarvis-files-changed', h)
    return () => { clearInterval(t); window.removeEventListener('jarvis-files-changed', h) }
  }, [slug]) // eslint-disable-line

  async function act(body) {
    const r = await api(`/api/projects/${slug}/todos`, {
      method: 'POST', body: JSON.stringify(body) })
    setTodos(r.todos)
  }

  const running = jobs.filter((j) => !j.done)
  const recent = jobs.filter((j) => j.done).slice(0, 3)
  const doneCount = todos.filter((t) => t.done).length
  return (
    <div className="pane-col">
      <div className="dim small">goal</div>
      <textarea className="board-goal" rows={2} value={state.goal || ''}
                placeholder="what is this session trying to achieve?"
                onChange={(e) => setState({ goal: e.target.value })} />
      <div className="row">
        <span className="dim small grow">plan · {doneCount}/{todos.length} done</span>
      </div>
      <ul className="todo-list grow-scroll">
        {todos.map((t, i) => (
          <li key={i} className={t.done ? 'done' : ''}>
            <label>
              <input type="checkbox" checked={t.done}
                     onChange={() => act({ action: 'toggle', index: i })} />
              <span>{t.text}</span>
            </label>
            <button className="win-btn" onClick={() => act({ action: 'delete', index: i })}>×</button>
          </li>
        ))}
        {todos.length === 0 && <EmptyState as="li">no plan yet — Jav3 writes one with todo_update</EmptyState>}
      </ul>
      <form className="row" onSubmit={(e) => {
        e.preventDefault()
        if (text.trim()) { act({ action: 'add', text }); setText('') }
      }}>
        <input className="grow" placeholder="add a step…" value={text}
               onChange={(e) => setText(e.target.value)} />
        <button type="submit">Add</button>
      </form>
      <div className="dim small">runs</div>
      <ul className="board-runs">
        {running.length === 0 && recent.length === 0 &&
          <EmptyState as="li">nothing running</EmptyState>}
        {running.map((j) => (
          <li key={j.id}>
            <span className="run-dot running" />
            <span className="grow ellipsis" title={j.summary}>{j.summary || `#${j.id}`}</span>
            <span className="tag running">{j.kind}</span>
          </li>
        ))}
        {recent.map((j) => (
          <li key={j.id} className="dim">
            <span className="run-dot done" />
            <span className="grow ellipsis" title={j.summary}>{j.summary || `#${j.id}`}</span>
            <span className="tag done">{j.kind}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
