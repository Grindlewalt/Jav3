import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Tools() {
  const [tools, setTools] = useState([])
  useEffect(() => { api('/api/tools').then((r) => setTools(r.tools)) }, [])

  return (
    <div className="page">
      <h2>Tools</h2>
      <p className="dim">what Jarvis is allowed to do. Everything goes through the
        registry + one calling convention; granting = flipping
        <code> enabled</code> in the def once a handler exists.</p>

      <div className="tool-grid">
        {tools.map((t) => (
          <div key={t.name} className="tool-card">
            <div className="row">
              <code className="grow">{t.name}</code>
              {t.enabled
                ? <span className="badge">granted</span>
                : <span className="tag">not granted</span>}
            </div>
            <p>{t.desc || t.description}</p>
            {t.when_to_use && <p className="dim small">use when: {t.when_to_use}</p>}
          </div>
        ))}
        {tools.length === 0 && <p className="dim">registry is empty</p>}
      </div>
    </div>
  )
}
