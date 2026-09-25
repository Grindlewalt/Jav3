import { useEffect, useState } from 'react'
import { api } from '../api.js'
import EmptyState from '../components/EmptyState.jsx'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

export default function TodoPanel({ slug }) {
  const [todos, setTodos] = useState([])
  const [text, setText] = useState('')

  useEffect(() => {
    api(`/api/projects/${slug}/todos`).then((r) => setTodos(r.todos))
  }, [slug])

  async function act(body) {
    const r = await api(`/api/projects/${slug}/todos`, {
      method: 'POST', body: JSON.stringify(body) })
    setTodos(r.todos)
  }

  return (
    <div className="pane-col">
      <form className="row" onSubmit={(e) => {
        e.preventDefault()
        if (text.trim()) { act({ action: 'add', text }); setText('') }
      }}>
        <input className="grow" placeholder="add a to-do…" value={text}
               onChange={(e) => setText(e.target.value)} />
        <button type="submit">Add</button>
      </form>
      <ul className="todo-list">
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
        {todos.length === 0 && <EmptyState as="li">nothing yet</EmptyState>}
      </ul>
    </div>
  )
}
